# Контракт моста v1

**Статус:** проект интерфейса, команды ещё не реализованы. См. [plan](../plan.md) и [data-model](../data-model.md).

## CLI

Предполагаемая точка входа: `python scripts/ai/collaboration_bridge.py <command>`. Python означает проверенный локальный интерпретатор; bootstrap устанавливает отдельное окружение и выдаёт его launcher.

| Команда | Поведение |
|---|---|
| doctor | Read-only проверка root, native executables, capabilities и зависимостей; без моделей/секретов |
| bootstrap --dry-run | Список предлагаемых локальных изменений, регистраций и коллизий; ничего не записывает |
| bootstrap | Создаёт окружение моста и собственную MCP запись Codex; compare/no-op, чужие config сохраняет |
| serve | MCP stdio; stdout только протокол, диагностические сообщения только безопасный stderr |
| status --work-id ID | Локальный snapshot, состояние worker, граница операции и последнее безопасное событие |
| stop --work-id ID | Durable stop_requested; не запускает модель и не удаляет изменения |
| handoff --work-id ID | Запрашивает передачу, обновляет контекст без модели; releasing только после safe completion |
| export --work-id ID --output PATH | Явно выбранный безопасный переносимый контекст; без raw logs/session/auth/персональных путей и Git push |
| reconcile --work-id ID | Read-only Git metadata и сравнение выбранных файлов/tasks; результат требует решения Codex |
| recover --work-id ID --evidence PATH | Фиксирует ручную проверку неизвестного исполнения; не снимает lock без доказательства и сверки |
| quota --work-id ID | Read-only квота текущей модели/группы, safe windows/source/checked_at и доступность; без модельного хода |

Overrides: `--project-root`, `--python-exe`, `--agy-exe`, `--codex-exe` либо локальные INVT_AI_* env-переменные. Одинаковые ключи: аргумент → локальная настройка → PATH/discovery. Нет implicit fallback на прежнее устройство. Секреты в config не читаются для reports.

CLI возвращает JSON при `--json`, иначе краткий русский текст. Exit: 0 — операция выполнена/статус прочитан, 2 — некорректный ввод/недоступная возможность, 3 — approval/ownership/revision block, 4 — ошибка процесса/протокола/состояния. Status может успешно прочитать work.state=error: exit команды не означает успешность работы.

## MCP tools только для Codex

Общий ответ: schema_version, ok, work_id, revision, state, execution_known, last_event_seq, next_action; при ошибке code и безопасное detail. State-changing запросы имеют request_id, expected_revision. Ревизионный конфликт и повторный request не создают дополнительный worker. Чтение возвращает только данные этой работы, cursor=event seq.

Дополнение 2026-10-04: read/status содержат quota summary и quota_low/quota_exhausted/quota_unknown notices со ссылкой на checkpoint. Перед start/reply/review changes_requested/resume требуется актуальная доступная квота применимой группы; <=20% любого применимого окна блокирует новый ход. Неизвестные/reset-expired/отсутствующие данные не дают разрешение и не подставляют 0/100%. Порог и положительный interval настраиваются локально, bootstrap не редактирует native Antigravity settings.

| Tool | Вход | Результат/ограничение |
|---|---|---|
| collaboration_create | request_id, задание: goal/artifacts/files/actions/criteria/task_ids/timeout | work_id и checkpoint; без запуска, awaiting_approval при отсутствии решения |
| collaboration_approve | work_id, expected_revision, request_id, реальное решение пользователя + scope_digest | Запись согласования. Tool description запрещает Codex имитировать решение пользователя |
| collaboration_start | work_id, expected_revision, request_id | Сохраняет assignment/checkpoint, захватывает checkout, запускает worker, быстро возвращает implementing |
| collaboration_read | work_id, after_seq, wait_seconds=0..30 | Snapshot и новые нормализованные сообщения; polling не создаёт prompt |
| collaboration_reply | work_id, expected_revision, request_id, reply_to, content | Ответ на вопрос именно этой работы; новый ход exact session, approval проверяется заново |
| collaboration_review | work_id, expected_revision, request_id, Review | changes_requested отправляет замечание той же session; accepted даёт complete только для актуального fingerprint; blocked сохраняет причину |
| collaboration_stop | work_id, expected_revision, request_id | Stop request; подтверждение запроса не равно остановке worker |
| collaboration_resume | work_id, expected_revision, request_id, reconciliation/approval refs | Продолжает только после сверки; work_id сохранён, ready result сначала идёт в review |
| collaboration_handoff | work_id, expected_revision, request_id | Сохранённый context и pending/safe статус передачи; код Codex не нужен для построения файла |
| collaboration_reconcile | work_id | Фактические Git metadata + tasks/current fingerprints; ничего автоматически не перезаписывает |

No model sampling и обратного обращения к OpenAI API внутри сервера. Двусторонний цикл: read(question) → Codex reasoning → reply → read(result). Он работает, пока Codex доступен; при лимитах последующий ответ заменяется самостоятельным native workflow пользователя, без обещания работающей модели Codex.

Квотный источник — отдельный print invocation `/model` и `/usage`, а не stream user message. Нормализовать только `command.data.id/label` модели и применимые `command.data.groups[].buckets[]` с remaining_fraction/reset_time/window/id/name. Status=ERROR, повреждённый payload, неподтверждённая группа либо bounded timeout дают quota_unknown. Ошибка настоящей генерации о квоте имеет приоритет над предыдущим available snapshot; 429 без подтверждения исчерпания не выдаётся за достоверный остаток.

После quota event checkpoint пишется без нового Antigravity prompt, затем Codex независимо проверяет фактические пути/результаты и сообщает пользователю. Уведомление содержит работу/задачу, остаток и reset лишь при доступности, последнюю подтверждённую границу, непроверенные действия и checkpoint path. Не создавать автосообщения вне текущего диалога, не менять модель/аккаунт/paid reserve. Одно событие доставляется по seq, repeated polling не расходует модельную квоту и не дублирует уведомление.

## Протокол worker → Antigravity

Выбранный запуск: проверенный `agy.exe` с `--input-format stream-json --output-format stream-json --json-schema <object-schema-path> --mode accept-edits`. cwd — текущий root. Восстановление добавляет только точный `--conversation UUID`. --continue и --dangerously-skip-permissions запрещены. Внешний deadline обязателен; семантика print-timeout в stream-input проверяется отдельной контрактной проверкой.

Задание передаётся одним UTF-8 NDJSON user сообщением; затем stdin закрывается. stdout и stderr читаются одновременно, ограниченными буферами, пока процесс работает. Нет ожидания process exit перед чтением потока. Непонятный event, поздняя ошибка, неожиданный session ID или отсутствие terminal result не считаются успехом. Документированный wire format — [Antigravity Headless](https://www.antigravity.google/docs/cli/headless/).

Object schema исполнителя, принадлежащая нашему протоколу:

| Поле | Тип/правило |
|---|---|
| schema_version | integer=1 |
| work_id, turn_id | UUID, должны совпасть с назначением |
| kind | question / implementation_result / blocked |
| summary | непустой safe string |
| claimed_files | массив относительных путей; для вопроса может быть пустым |
| checks | массив {criterion_id, status, command_or_description, exit_code, evidence, limitation} |
| remaining_actions | массив безопасных кратких строк |
| question | для kind=question: {question_id, text, decision_kind=technical/scope/permission} |
| block_reason | для blocked: {code, needed_action}; для остальных null |

Schema требует эти поля, запрещает неизвестные поля; question/block_reason допускают null только у соответствующего другого kind. Check passed/failed требует фактический запуск/evidence; not_run требует limitation. Conditional проверки реализуются также в parser, не полагаясь только на генератор модели. Free-text response не заменяет отсутствующий structured_output.

Первый init/result фиксирует локальную session. При продолжении UUID должен совпасть; невозможность восстановления — session_unavailable, без подстановки последней session. Проверка на новом устройстве сначала показывает отсутствие локальной session, затем явно создаёт новую из переносимого контекста.

Для перехода в review нужны: SUCCESS, валидный непустой structured_output, отсутствие denied/error/pending write, завершившийся CLI и scope check. Error status и tool failures имеют приоритет над ответом. Счётчики usage/num_turns/duration не используются как таймер текущего запуска. Записывается только нормализованный outcome, raw tool parameters/output не журналируются.

## Разрешения и передача

Не разрешены прямые изменения Antigravity settings/rules/skills/workflow, маскировка отказов и auto-proceed. Action approval задания — граница workflow, не замена OS sandbox. Нужные дополнительные права показываются пользователю; bootstrap их не выдаёт.

Safe handoff требует terminal current turn, вышедшего worker, отсутствия известных активных операций и свежего Git checkpoint. При timeout/unknown возвращается pending, checkout остаётся закреплённым. После safe transfer handed_off_to_user не предъявляет native Antigravity дополнительных требований.

## Bootstrap и сохранность

1. Найти текущую копию и необходимые EXE, проверить необходимые flags без model calls.
2. Создать отдельное окружение моста с фиксированными dependencies; не заменять системный Python/Java/Gradle.
3. Прочитать только нужную MCP-запись Codex и сравнить. Совпадение — no-op; коллизия — явная ошибка. Обновление своей записи сохраняет rollback-данные локально без публикации секретов.
4. Зарегистрировать собственное имя через официальную Codex CLI. Проверить запись и handshake. При сбое восстановить только свою запись; чужие записи/модели не менять.
5. Проверить неизменность исходных Antigravity rules/skills/workflow, Spec Kit scripts/templates/manifests. Antigravity MCP/config не редактировать.
