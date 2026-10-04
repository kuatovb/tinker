# Проверка реализации связки

**Статус:** инструкция приёмки частично реализованного проекта. CLI doctor/status/stop/quota и тестовый набор доступны; bootstrap, serve и полный MCP/E2E цикл ещё не готовы. Ниже отдельно описаны предполагаемые команды и критерии будущей сквозной приёмки.

## Подготовка

Использовать отдельный Windows checkout без секретных копий/env/ключей, Python 3.12+, native Antigravity/Codex CLI и их собственную локальную авторизацию. Для negative tests использовать fake CLI, не провайдера. Реальные модельные E2E выполнять на небольших согласованных fixtures, не на бизнес-коде.

Предполагаемые команды после реализации:

```powershell
python scripts/ai/collaboration_bridge.py doctor
python scripts/ai/collaboration_bridge.py bootstrap --dry-run
python scripts/ai/collaboration_bridge.py bootstrap
python -m unittest discover -s tests/ai/collaboration -v
```

Первый bootstrap регистрирует только свой Codex MCP server, повторный ничего не дублирует. Проверить настоящее MCP initialize/list_tools и доступность инструментов в новом диалоге Codex; простой CLI list не доказывает работоспособность сервера.

## Сценарии и ожидаемые доказательства

| Критерий | Сценарий | Доказательство |
|---|---|---|
| SC-001 | Согласовать изменение одного fixture; Codex create/approve/start/read; Antigravity пишет его; Codex review | assignment/result связаны одним work_id, реальный diff и независимая проверка; complete после review |
| SC-002 | Fixture явно требует технического уточнения; исполнитель возвращает question; Codex reply | same conversation UUID, reply_to правильный, результат учитывает ответ; текст пользователь не копирует |
| SC-003 | В fixture заложен воспроизводимый дефект; Codex возвращает замечание | failing evidence → same session fixes → passing independent evidence; tasks подтверждены после review |
| SC-004 | Fake CLI выдаёт ERROR+exit0, SUCCESS+empty, denied tool+exit0, malformed/partial JSON, wrong UUID, late error, background unfinished, timeout | Каждый исход явно error/blocked, complete отсутствует; timeout не разрешает второго writer |
| SC-005 | Две работы: read параллельно, start второго в том же checkout; затем отдельный worktree | same checkout отклонён checkout_busy; сообщения изолированы; разные checkout могут выполнять свои работы |
| SC-006 | Stop во время хода, дождаться результата/exit, resume | Следующий prompt не отправлен; work_id, сообщения и файлы сохранены; resume после сверки |
| SC-007 | Два каталога на текущем Windows, один с пробелами/кириллицей; затем другой Windows-компьютер с иными tool paths | doctor/повторный bootstrap/E2E в каждой копии; отсутствие старых путей; второе устройство имеет собственный evidence |
| SC-008 | Safe handoff, закрыть Codex; native Antigravity завершает fixture без моста; затем вернуть Codex | Native не требует Codex; reconcile находит изменения и готовые задачи, не повторяет готовые действия |
| SC-009 | Сравнить before/after SHA-256 защищённых правил/skills/workflow; отключить/удалить регистрацию моста | Исходные файлы совпадают; standalone Antigravity работает без регистрации/ответа Codex |
| SC-010 | Codex недоступен; прочитать checkpoint и выполнить offline handoff; повторить при незавершённом ходе | Нет новых запросов к модели Codex; сохранена граница операции; ownership передано лишь после safe completion |
| SC-011 | Fake quota даёт применимый остаток <=20%, затем exhaustion при активном ходе | Новый prompt не отправлен, notice и checkpoint с task/work IDs видны, partial result не complete; active writer не считается остановленным по одному notice |
| SC-012 | Источник timeout/ERROR, malformed schema, expired reset или неизвестная/чужая модельная группа | quota_unknown, причины явные; нет подстановки 0/100% или session tokens, новая генерация запрещена |
| SC-013 | Исчерпание не позволяет модели отвечать; создать checkpoint без prompt, затем восстановить quota и reconcile | Уведомление содержит реальные файлы/проверки/границу; выполненное не повторяется, writer один, требуется пользовательское решение о продолжении |

Дополнительные negative checks: нет настоящего approval; stale revision/request duplicate; чужая MCP запись; symlink за root; bat/cmd override; недоступный native EXE/session; user files changed; scope violation; secret-looking tool output; повреждённый snapshot/event tail; worker crash/PID reuse. Отдельный fixture: уже dirty посторонний файл изменяется повторно, staged blob меняется при прежнем наборе status paths; scope check должен обнаружить оба нарушения по раздельным fingerprints. Ожидаемый исход — конкретная ошибка и сохранность файлов, не автоматический fallback.

Квотные tests не расходуют реальную квоту до нуля: использовать fake windows/errors и реальный read-only `/model`/`/usage` для проверки schema/no-turn. Проверить before-start, between-turn и периодическое наблюдение, two-window minimum, notice dedup и отсутствие auto account/model/paid fallback. Код fixtures/tests и технические документы пишет Antigravity, Codex независимо запускает проверки и фиксирует подтверждённые результаты в задачах.

## Самостоятельное продолжение

Предполагаемый offline CLI, без модели Codex:

```powershell
python scripts/ai/collaboration_bridge.py status --work-id <UUID>
python scripts/ai/collaboration_bridge.py stop --work-id <UUID>
python scripts/ai/collaboration_bridge.py handoff --work-id <UUID>
```

До запуска следующего writer status должен подтвердить safe handoff, а не только получение запроса. При pending/execution_unknown сначала проверить активного исполнителя и сохранить evidence recovery. После передачи открыть обычный Antigravity в проекте и дать ему прочитать spec/plan/tasks и сохранённый handoff.md; решения о продолжении принимает пользователь по native workflow.

Если мост не установлен, читать ранее созданный handoff.md обычным редактором. Отсутствие моста само по себе не доказывает остановку прежнего writer: если safe transfer не зафиксирован, проверить исполнителя и незавершённые операции вручную, сохранить recovery evidence и сверить Git до native записи. На другом устройстве использовать согласованные Git-артефакты и отдельно выбранный safe export; не копировать auth, config, raw sessions и локальную reservation. Новая машина создаёт новую session после сверки кода. После возврата Codex:

```powershell
python scripts/ai/collaboration_bridge.py reconcile --work-id <UUID>
```

Codex сравнивает актуальное состояние, подтверждает готовые задачи и согласует остаток; reconcile сам не запускает исполнителя.

## Оформление проверки

Unit/contract tests и fake-CLI scenarios отделены от реальных E2E. Не выдавать фикстуры за работу провайдера, вторую папку за второе устройство или exit0 за завершённое задание. Evidence: work/turn IDs локально, обезличенные outcomes и SHA-256 fixtures/защищённых файлов; без секретов/полного raw stream.

Для Android-задания проверять Gradle через Wrapper 9.7.1 и JAVA_HOME этого устройства; запускать относящиеся тесты/сборку и копирование свежего APK по правилам ветки. Backend/frontend/Docker/migrations проверять при их реальном изменении. Само добавление tooling не требует пересборки незатронутых приложений.

Пока второе устройство недоступно, оставить соответствующий acceptance check незавершённым с точной причиной. Итоговый walkthrough перечисляет фактически выполненные сценарии и ограничения; Conventional Commit/push включает только исходники/документацию, а не state/logs/auth/APK.
