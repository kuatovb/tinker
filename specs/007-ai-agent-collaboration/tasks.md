# Задачи: совместная работа Codex и Antigravity

**Перенос в tinker, 2026-10-05:** самостоятельный проект связки нескольких ИИ. Требования, T001–T057 и 8 подтверждённых задач сохранены из Inventory. Разработка моста остаётся отложенной; перенос не является её возобновлением. Реализованная пара — Codex и Antigravity; другие ИИ потребуют отдельного проектирования.

**Ветка:** `dev`. **Вход:** [spec](spec.md), [согласованный plan](plan.md), [research](research.md), [data-model](data-model.md), [контракт](contracts/bridge.md), [quickstart](quickstart.md).
**Статус:** реализация отложена пользователем 2026-10-05. Подтверждены **8 из 57 задач**; остальные задачи, исходники и результаты проверок сохранены на будущее. Checkbox не изменены решением об отсрочке.

**Текущий порядок:** Codex готовит ТЗ, план и задачи; пользователь сам передаёт поручение Antigravity. Автоматический запуск исполнителя и контроль лимитов не используются. Возобновление 007 возможно только по явному запросу пользователя после сверки фактических файлов, Git и задач. Согласование реализации от 2026-10-04 («да можно начать») и точки остановки ниже сохранены как история.

**Исторический порядок реализации 007 до отсрочки:** Codex готовил планы и задачи, отвечал на вопросы, контролировал квоту/выполнение и независимо проверял результат. Antigravity писал код, скрипты, тесты и техническую документацию, запускал проверки и возвращал результат. До появления моста Codex использовал проверенный CLI-механизм с точным conversation ID и read-only `/usage` перед заданиями/между этапами. Подтверждённые checkbox, checkpoint и evidence проверки фиксировал Codex. Этот механизм сейчас не применяется.

## Формат и границы

- Формат: `- [ ] Tnnn [P?] [USn?] действие с точным путём`.
- `[P]` разрешает параллельность только после общих prerequisites, при разных файлах. Два writers одного checkout запрещены; параллельная реализация требует отдельных согласованных копий. Чтение/ревью допускаются параллельно.
- P1: US1, US2, US3, US5, US6 и новое US7 (квота); P2: US4. Нумерация US соответствует spec. US7 адаптер может быть выполнен после основы до следующих live E2E; до его готовности Codex применяет ту же квотную политику вручную.
- Tests включены для явных сценариев приёмки SC-001–SC-013 и ошибок из spec/quickstart. Fake CLI/quota тесты отделены от живых E2E; они не требуют провайдерских запросов.
- На каждой реализации проверяются Git status, scope и реальные результаты. Protected Antigravity rules/skills/workflow и Spec Kit scripts/templates/manifests не редактируются. Секреты, state, logs и auth не коммитируются.
- Gradle 9.7.1 сохраняется в Wrapper; JDK/SDK — текущего устройства. PostgreSQL уже настроена предыдущим этапом. Здесь нет изменений схемы/runtime и миграций; AGP/Kotlin/KSP не обновляются в рамках моста.

## Фаза 1 — подготовка

**Цель:** создать только минимальную структуру tooling и среду проверок, сохранив стек приложения.

- [X] T001 Создать пакет и точки входа по plan в `scripts/ai/collaboration/__init__.py` и `scripts/ai/collaboration_bridge.py`, а также discovery для `tests/ai/collaboration/`; не подключать tooling к runtime приложения.
- [X] T002 Зафиксировать SDK в `scripts/ai/collaboration-requirements.txt` и способ отдельного Python 3.12+ окружения в `docs/DEVELOPMENT_ENVIRONMENT.md`; перед фиксацией сверить стабильную версию с официальным PyPI/docs, исходная согласованная версия `mcp==2.3.0`; не менять системный Python/Java/Gradle и чужие venv.
- [X] T003 [P] Подготовить управляемый fake CLI и несекретные fixtures в `tests/ai/collaboration/fake_cli.py` и `tests/ai/collaboration/fixtures/`, включая configurable UUID/result/exit/error/late events/долгий ход; без настоящей авторизации и model calls.

**Зависимости:** T001 → T002; T003 независима от T002 после T001. **Checkpoint:** структура есть, приложение не зависит от неё, никаких заявлений о работающем мосте.

**Проверка Codex 2026-10-04:** T001 подтверждён чтением точек входа и обнаружением 25 тестов через unittest discovery; исходники приложения не изменены. T002: PyPI подтвердил стабильную 2.3.0, установка в игнорируемый `logs/ai/collaboration/venv/` успешна, `importlib.metadata.version('mcp')` вернул 2.3.0 и импорт `MCPServer` проходит. Первые 25 тестов прошли, но независимое ревью выявило дефекты хранения, ownership, протокола и квоты: T003–T011/T048 не приняты, исправления переданы Antigravity в ту же сессию.

**Повторная проверка Codex 2026-10-04:** обычный запуск `unittest` подтвердил 72 теста (15.664 с), read-only `doctor` подтвердил Python 3.12.10 отдельного окружения и MCP 2.3.0, реальный `quota` вернул применимые Gemini weekly/5h buckets. T003 подтверждена: fake CLI и fixtures работают без модели и авторизации. Это не приёмка полного моста: T006/T007 сохраняют два выявленных P1 (исчезновение исходно dirty пути из status и recovery по одной текстовой пометке). `serve/bootstrap/handoff/export/reconcile/recover` ещё не реализованы. Пользователь выбрал малые исправления с отдельной проверкой вместо пакетных переписываний.

## Фаза 2 — общая основа

**Цель:** durable state, защита checkout и обработка CLI до реализации историй. Фаза блокирует все US.

- [ ] T004 Реализовать Work/Approval/Message/Turn/Result/Review/Environment/Ownership/Handoff и разрешённые переходы в `scripts/ai/collaboration/state.py` по data-model: «ID проверяются как UUID, revision — целое положительное число», «Цель, критерии и summary непустые», «Timeout положительный», `schema_version=1`; неизвестные enums/поля возвращают protocol_error. Реальные Approval хранят источник решения и scope_digest; модель не имитирует пользователя.
- [ ] T005 Реализовать atomic snapshot, seq/revision event store, нормализованный safe output и checkpoint до start/после исходов в `scripts/ai/collaboration/state.py`: flush/fsync + replace в той же папке, явное обнаружение повреждений, без raw stdout/stderr/tool_info, diff/config/auth; suspicious output → sensitive_output_blocked. Локальные файлы — только `logs/ai/collaboration/`, игнорируются Git.
- [ ] T006 Реализовать канонический checkout key, file/execution lock, persistent reservation и baseline в `scripts/ai/collaboration/state.py`: HEAD/ветка/paths, отдельные index/worktree fingerprints несекретных allowed и исходно dirty/untracked посторонних файлов, создание/удаление; секретам — только metadata. Проверять PID/start_time/token, не снимать reservation по возрасту или timeout; checkpoint содержит границу текущей операции.
- [ ] T007 Проверить invariants и восстановление в `tests/ai/collaboration/test_state.py`: revision/request duplicate, crash/partial event, owner collision, PID reuse, повторная правка уже dirty вне scope и staged blob при прежних status paths; содержимое пользователя сохраняется, secret/raw output не попадает в state.
- [ ] T008 Реализовать read-only discovery/doctor в `scripts/ai/collaboration/environment.py`: Git root сверяется со скриптом, canonical paths и overrides аргумент → локальная настройка → PATH; native EXE и capabilities, без старых C/D путей, моделей и секретов; bat/cmd override и unavailable tool дают явный отказ. JDK/SDK проверять лишь для Android-заданий.
- [ ] T009 Реализовать object schema и NDJSON decoder в `scripts/ai/collaboration/worker.py`: `kind=question/implementation_result/blocked`, `work_id/turn_id` совпадают; «Schema требует эти поля, запрещает неизвестные поля», question/block_reason nullable только для другого kind; checks passed/failed требуют фактическое evidence, not_run — limitation. Отсутствующий structured_output не заменять free text.
- [X] T010 Реализовать detached worker одного хода в `scripts/ai/collaboration/worker.py`: native EXE+argv/UTF-8 stdin, один user message+EOF, параллельное ограниченное чтение stdout/stderr, сохранение UUID, внешний monotonic deadline 600 секунд по умолчанию. SUCCESS принимается лишь после terminal+CLI exit и отсутствия denied/error/pending write; timeout/crash → execution_unknown, без retry/release/forcedkill-обещаний.
- [X] T011 Проверить protocol/process negative cases в `tests/ai/collaboration/test_worker.py`: ERROR+exit0, SUCCESS+empty, denied+exit0, malformed/partial JSON, wrong IDs, отсутствие result, late error, pending background, накопительные counters, timeout и безопасное позднее завершение. Каждый исход явный, новый writer при unknown запрещён.

**Зависимости:** T004 → T005 → T006 → T007; T008 после фазы 1; T009 после T004; T010 после T005/T006/T008/T009/T003; T011 после T010. **Checkpoint:** тестируемая основа, неизвестное исполнение не превращается в успех. Автоматический handoff ещё не заявляется готовым.

## Фаза 3 — US1: передача согласованной задачи (P1, первый MVP)

**Цель:** согласованная задача → Antigravity → отчёт → базовая независимая приёмка Codex.
**Независимая приёмка:** SC-001 — один небольшой fixture изменён только в scope, assignment/result одной работы, Codex подтверждает diff и критерий; пользователь не копирует сообщения.

- [ ] T012 [P] [US1] Добавить проверки create/approve/start и базового accepted review в `tests/ai/collaboration/test_assignment.py`: без approval запуск запрещён, изменённый scope требует нового решения, SUCCESS исполнителя остаётся in_review, accepted относится к текущему fingerprint.
- [ ] T013 [P] [US1] Добавить контрактные проверки MCP request/response в `tests/ai/collaboration/test_mcp.py`: общие поля, request_id/expected_revision, cursor, bounded wait `0..30`, отсутствие дублей worker и чужих сообщений; stdout содержит только протокол.
- [X] T014 [US1] Реализовать create/approve и сборку assignment в `scripts/ai/collaboration/state.py`: уникальный work_id, goal/artifacts/files/actions/acceptance/task_ids/timeout и actual approval digest; относительные пути остаются внутри root, `..`/symlink escape/auth/env/key отклоняются, unknown task_id запрещён.
- [X] T015 [US1] Соединить start с worker в `scripts/ai/collaboration/worker.py`: durable checkpoint до исполнения, согласованный scope и решение пользователя в prompt, per-run accept-edits, исходные shell/MCP permissions без расширения; blocked native workflow не имитирует Proceed.
- [ ] T016 [US1] Экспонировать create/approve/start/read и базовый review accepted/blocked в `scripts/ai/collaboration_bridge.py`: start быстро возвращает work_id, read не запускает модель, declared checks остаются заявлениями до независимого evidence и актуального fingerprint; backend MCP не вызывает Codex/OpenAI sampling/API.
- [ ] T017 [US1] Добавить только Codex-порядок передачи и проверки согласованного задания в `AGENTS.md`; до готовности остальных US явно описать доступный объём и независимость native Antigravity, не менять `.agents/rules/workflow.md` или skills Antigravity.
- [ ] T018 [US1] Проверить initialize/list_tools/call_tool через настоящий stdio MCP client и fake executor в `tests/ai/collaboration/test_mcp_integration.py`; проверить изоляцию двух работ и checkout_busy, отдельные worktree допускают независимую запись (SC-005).
- [ ] T019 [US1] Выполнить живой SC-001 из Codex с временной stdio-конфигурацией только этой сессии и маленьким fixture; сохранить независимый diff/evidence в `walkthrough.md`, безопасный локальный результат — в `logs/ai/collaboration/`; bootstrap постоянной регистрации пока не требуется. Код исполнения пишет Antigravity, не модель Codex.

**Зависимости:** T012/T013 после фазы 2, могут разрабатываться независимо; T014 → T015 → T016 → T017/T018 → T019. **Checkpoint:** первый MVP, не полная приёмка всех пользовательских требований.

## Фаза 4 — US2: вопросы и ответы (P1)

**Цель:** Antigravity спрашивает Codex и получает ответ в своей конкретной session.
**Независимая приёмка:** SC-002 — ответ учтён, work/message/session IDs совпадают; две работы не смешиваются.

- [ ] T020 [US2] Добавить маршрутизационные negative/positive checks в `tests/ai/collaboration/test_questions.py`: technical/scope/permission questions, wrong reply_to/work_id, duplicate answer, lost local session; forbidden --continue отсутствует.
- [ ] T021 [US2] Реализовать needs_answer и assignment/question/answer связи в `scripts/ai/collaboration/state.py`: Message содержит sender/recipient/work_id/turn_id/seq/reply_to; «Технический ответ не расширяет scope», новый scope/доступ ждёт настоящий Approval.
- [ ] T022 [US2] Реализовать следующий ход exact --conversation в `scripts/ai/collaboration/worker.py`, только после достоверного окончания предыдущего; unavailable/wrong UUID → session_unavailable, никогда не выбирать глобальную последнюю session и не создавать скрытый retry.
- [ ] T023 [US2] Экспонировать collaboration_reply и сообщения через read в `scripts/ai/collaboration_bridge.py`: ответ лишь на ожидаемый message_id этой работы и revision; вопрос пользователю при изменении требований/доступа, технические уточнения решает Codex в согласованном scope.
- [ ] T024 [US2] Выполнить живой SC-002 с вопросом и ответом в одном conversation; записать evidence в `walkthrough.md`, local trace IDs — в `logs/ai/collaboration/`; выполнить fake двухработный сценарий wrong recipient без смешивания сообщений.

**Зависимости:** US1 → T020 → T021 → T022 → T023 → T024. **Checkpoint:** двустороннее общение работает, model outage остаётся явной ошибкой.

## Фаза 5 — US3: замечания и независимое ревью (P1)

**Цель:** обнаруженный Codex дефект возвращается исполнителю, завершение основано на фактах.
**Независимая приёмка:** SC-003 — failing check → same-session fixes → passing independent check; непроведённая проверка не считается passed.

- [ ] T025 [US3] Добавить проверки ревью в `tests/ai/collaboration/test_review.py`: stale fingerprint, scope violation, missing/not_run check, incorrect task claim, accepted без criteria; отметка Antigravity не даёт complete.
- [ ] T026 [US3] Расширить Review/CheckEvidence и apply review в `scripts/ai/collaboration/state.py`: reviewer=Codex, verdict=accepted/changes_requested/blocked, checked fingerprint и criterion/task refs; «not_run содержит причину, не превращается в passed», accepted только на текущую ревизию с выполненными критериями.
- [ ] T027 [US3] Реализовать review changes_requested в `scripts/ai/collaboration_bridge.py` и передачу замечания через `scripts/ai/collaboration/worker.py` в прежнюю session с новой turn_id, без расширения согласованного scope; при конфликте пользовательских файлов блокировать перезапись.
- [ ] T028 [US3] Описать и подключить подтверждение task_ids после независимой проверки в `AGENTS.md` и `scripts/ai/collaboration/state.py`: claimed completion отдельно от verified evidence; записи в tasks.md делает Codex лишь для проверенного объёма, мост автоматически не делает commit/push.
- [ ] T029 [US3] Выполнить живой SC-003 на fixture с воспроизводимым дефектом; Codex самостоятельно запускает проверку до/после исправления, фиксирует ограничения недоступных checks и итог в `walkthrough.md`.

**Зависимости:** US1+US2 → T025 → T026 → T027 → T028 → T029. **Checkpoint:** цикл исправлений доказан; user/auth/runtime файлы не затронуты.

## Фаза 6 — US5: разные пути и Windows-устройства (P1)

**Цель:** локальная настройка повторяется без персональных путей и потери чужих настроек.
**Независимая приёмка:** SC-007 — два пути, включая пробелы/кириллицу, и отдельное физическое Windows-устройство с собственной установкой/авторизацией.

- [ ] T030 [US5] Добавить переносимость/bootstrap checks в `tests/ai/collaboration/test_environment.py`: новый root, executable overrides, кириллица/пробелы, отсутствующий EXE, bat/cmd отказ, existing own entry no-op, foreign collision и rollback только своей записи.
- [ ] T031 [US5] Реализовать bootstrap dry-run/apply в `scripts/ai/collaboration/environment.py`: отдельный venv, фиксированные dependencies, native capability checks, own Codex MCP registration через add/get/remove, canonical-copy entry name, сохранение чужих profiles/models/MCP и локальный rollback; никаких изменений Antigravity config/permissions.
- [ ] T032 [US5] Подключить doctor/bootstrap и локальные overrides к `scripts/ai/collaboration_bridge.py`, проверить повторную настройку и настоящий handshake; абсолютные пути генерируются только локально, перенос root требует нового bootstrap.
- [ ] T033 [US5] Реализовать device/session availability и явное восстановление из relative context в `scripts/ai/collaboration/state.py`: session старого устройства не импортируется как рабочая, unavailable требует явного нового локального conversation после решения о восстановлении; новый work/session не подменяют чужую работу.
- [ ] T034 [US5] Выполнить doctor/bootstrap/живую малую задачу из двух каталогов текущего Windows, включая пробелы/кириллицу и overrides; подтвердить отсутствие старых путей в Git-артефактах, записать evidence в `walkthrough.md` и обновить `docs/DEVELOPMENT_ENVIRONMENT.md`.
- [ ] T035 [US5] Выполнить doctor/bootstrap/живую малую задачу на втором физическом Windows-устройстве с иными root/tool paths; результаты добавить в `walkthrough.md`. Если устройство недоступно, оставить T035 незавершённой с причиной, не считать T034 заменой SC-007.

**Зависимости:** фаза 2+US1 → T030 → T031 → T032 → T033 → T034 → T035. Этот блок может быть подготовлен после US1, но writers общих файлов с US2/US3 исполняются последовательно. **Checkpoint:** переносимость подтверждается только фактически доступными устройствами.

## Фаза 7 — US6: самостоятельный Antigravity без Codex (P1)

**Цель:** контекст сохраняется без модели, право записи передаётся безопасно, возвращение не повторяет выполненную работу.
**Независимая приёмка:** SC-008/009/010 — закрытый Codex/отключённый мост, native выполнение, unchanged Antigravity rules, затем актуальное reconcile.

- [ ] T036 [US6] Добавить проверки handoff/export/reconcile в `tests/ai/collaboration/test_handoff.py`: checkpoint до первого запуска, offline вызовы без провайдера, pending при active/unknown, release только safe, экспорт без local paths/session/auth/raw logs; stale task completion не запускает writer.
- [ ] T037 [US6] Реализовать полный handoff.md и portable export в `scripts/ai/collaboration/state.py`: только относительные artifacts, safe results/checks, ready/remaining actions и текущая boundary; строить из имеющихся данных до запуска и после достоверных исходов, без вызова Codex и без auto Git publication.
- [ ] T038 [US6] Реализовать durable handoff request и safe ownership release в `scripts/ai/collaboration/state.py` и `scripts/ai/collaboration/worker.py`: запрет следующих ходов сразу, ожидание current terminal+worker exit+нет известных active operations+fresh checkpoint; timeout/unknown не снимают reservation. Offline handoff не требует реализованного US4 CLI stop.
- [ ] T039 [US6] Реализовать read-only reconcile и recovery evidence в `scripts/ai/collaboration/state.py`: текущие HEAD/branch/metadata, раздельные безопасные fingerprints, tasks и результаты native работы; сохранить user changes, stale complete/approval не применяются автоматически, ready work направить на ревью. Recover требует реальной проверки исполнителя, не одного PID/возраста lock.
- [ ] T040 [US6] Экспонировать offline CLI handoff/export/reconcile/recover и MCP handoff/reconcile в `scripts/ai/collaboration_bridge.py`; локальные команды работают без Codex CLI/модели и без авторизации OpenAI; pending ответ не равен подтверждённой передаче.
- [ ] T041 [US6] Документировать native продолжение с мостом и без него в `docs/DEVELOPMENT_ENVIRONMENT.md` и только Codex порядок возвращения в `AGENTS.md`: Markdown читаем без tooling, отсутствие моста не доказывает stop, при неизвестном writer сначала ручной recovery; исходные Antigravity rules/skills/workflow остаются неизменными.
- [ ] T042 [US6] Выполнить живые SC-008/009/010: safe handoff, отключить Codex/мост, пользователь продолжает native Antigravity, Codex возвращается и сверяет; сравнить SHA-256 защищённых файлов before/after, подтвердить отсутствие нового Codex model call для checkpoint/handoff и не более одного writer; evidence записать в `walkthrough.md`.

**Зависимости:** US1+US3+T033 → T036 → T037 → T038 → T039 → T040 → T041 → T042. US4 не prerequisite: общий stop_requested guard уже T010/T038. **Checkpoint:** standalone Antigravity не зависит от моста или доступности Codex.

## Фаза 8 — US4: статус, остановка, продолжение (P2)

**Цель:** управлять работой по ID, сохраняя результат и незавершённую границу.
**Независимая приёмка:** SC-006 — stop во время хода не отправляет следующий prompt, resume после сверки сохраняет work_id и файлы.

- [ ] T043 [US4] Добавить lifecycle checks в `tests/ai/collaboration/test_lifecycle.py`: offline status/stop, активный ход, late result, stopped resume, changed files/reconciliation_required, неизвестное исполнение и request duplicate; отсутствие новых prompts после stop.
- [ ] T044 [US4] Реализовать остановку на границе хода и resume в `scripts/ai/collaboration/state.py`/`scripts/ai/collaboration/worker.py`: stop/handoff flags не преждевременная смена implementing, сохранять last_result; resume сохраняет work_id, требует актуальное reconciliation/approval/ownership, готовый result сначала идёт в review.
- [ ] T045 [US4] Экспонировать offline status/stop и MCP stop/resume в `scripts/ai/collaboration_bridge.py`: вывод agent/stage/last meaningful event и operation boundary, read wait `0..30`, отсутствие sampling/API/model calls; exit status-команды не трактовать как успешность самой работы.
- [ ] T046 [US4] Проверить локальный status с целью отклика до 2 секунд и read wait до 30 секунд, затем stop/resume длительного fake worker в `tests/ai/collaboration/test_lifecycle.py`; сохранить измеренные пределы без приписывания latency провайдера.
- [ ] T047 [US4] Выполнить живой SC-006 на маленьком fixture; проверить сохранность IDs/messages/files после stop и возврата, запрет следующего хода до resume; записать результат в `walkthrough.md` и инструкции в `docs/DEVELOPMENT_ENVIRONMENT.md`.

**Зависимости:** US6 → T043 → T044 → T045 → T046 → T047. **Checkpoint:** управление подтверждено на fake и живом исполнителе; неизвестное исполнение требует recovery.

## Фаза 9 — US7: квота исполнителя и точка остановки (P1)

**Цель:** Codex контролирует применимую квоту Antigravity, фиксирует реальные результаты и уведомляет пользователя без нового запроса исполнителю.
**Независимая приёмка:** SC-011/012/013 — low/exhausted/unknown блокируют следующий ход, checkpoint и notice доступны, восстановление идёт после сверки; fake-тест не исчерпывает квоту настоящего аккаунта.

- [X] T048 [US7] Реализовать нормализованный read-only quota adapter в `scripts/ai/collaboration/environment.py`: отдельные `/model` и `/usage` print JSON, bounded timeout, выбранная модель и применимые groups/buckets, safe remaining_fraction/reset_time/window/source/checked_at; counters usage не подменяют account quota, unknown/expired/wrong group не дают разрешение. Никаких auth-файлов/private API или изменения native config.
- [ ] T049 [US7] Реализовать QuotaSnapshot/QuotaNotice и guard в `scripts/ai/collaboration/state.py`/`scripts/ai/collaboration/worker.py`: remaining_fraction 0..1 либо null, threshold default 0.20 и допустимый 0..1, interval default 60s положительный; check до start/reply/review fixes/resume и во время долгого хода. <=threshold, exhaustion или unknown блокируют следующий prompt, checkpoint записывается из событий/фактических metadata, active writer не объявляется остановленным; без retry/model/account/paid switch.
- [ ] T050 [US7] Экспонировать offline quota и квотные notice/status через `scripts/ai/collaboration_bridge.py`: dedup по событию/revision, work/task ID, последний подтверждённый этап, изменённые пути, checks/pending actions и reset только при доступности; Codex сверяет evidence и сообщает пользователю в текущем диалоге, snapshot создаётся без нового модельного запроса Antigravity.
- [ ] T051 [US7] Написать и выполнить `tests/ai/collaboration/test_quota.py` с fake quotas/errors: два окна/нужная группа, <=20%, zero/provider exhaustion, timeout/stale reset/malformed payload, failed mapping, stop request при writer, late result, no-next-prompt, notice dedup и resume только после fresh check+reconcile. Тесты пишет Antigravity, Codex независимо проверяет результат.
- [ ] T052 [US7] Выполнить реальный read-only `/model`/`/usage` для schema/no-turn и controlled fake-exhaustion end-to-end; проверить checkpoint/уведомление/возврат без расхода реальной квоты до нуля. Antigravity записывает техническое evidence в `walkthrough.md`, Codex проверяет фактическую точку остановки и подтверждает задачи в `specs/007-ai-agent-collaboration/tasks.md`.
- [ ] T053 [US7] Antigravity обновляет `docs/DEVELOPMENT_ENVIRONMENT.md`, `docs/INDEX.md`, `CHANGELOG.md` и `walkthrough.md`: описать роли, quota defaults/unknown, manual bootstrap monitoring, actual remaining как временный снимок, безопасный checkpoint и уведомление; Codex проверяет документы. Исторический список 51 задачи заменить актуальным 57, не менять исходные правила Antigravity.

**Зависимости:** фаза 2 → T048 → T049 → T050 → T051 → T052 → T053. Для проверки resume T051/T052 нужен US4; базовый guard и статус не зависят от полного lifecycle. Автоматический guard T048–T050 требуется до объявления MVP готовым. До него Codex проверяет квоту вручную для каждой передачи и сохраняет ту же границу остановки.

## Фаза 10 — общая приёмка и документация

- [ ] T054 Antigravity запускает полный `unittest` и применимые MCP integration/negative scenarios из `tests/ai/collaboration/`; выполнить оставшиеся проверки `specs/007-ai-agent-collaboration/quickstart.md`, разделить fake/live и отметить недоступные сценарии, включая второе устройство. Codex независимо проверяет результаты и повторяет нужные проверки, не скрывая ERROR/empty/denied/quota.
- [ ] T055 Проверить отсутствие секретов/local paths/state в Git scope и неизменность protected Antigravity/Spec Kit файлов; Antigravity уточняет operational инструкции в `docs/DEVELOPMENT_ENVIRONMENT.md` и актуальный индекс `docs/INDEX.md`, Codex проверяет, сохраняя Gradle Wrapper 9.7.1 и самостоятельность native workflow.
- [ ] T056 Codex подтверждает выполненные задачи в `specs/007-ai-agent-collaboration/tasks.md`, Antigravity обновляет `CHANGELOG.md` и `walkthrough.md` фактическими evidence: checked только подтверждённые задачи, T035 не закрывать без второго устройства, unavailable checks не passed; отразить состояние моста и оставшиеся ограничения.
- [ ] T057 После согласованного реализованного объёма обновить Graphify по текущему root, проверить `graphify-out/graph.json`, выполнить scoped Conventional Commit и `git push origin HEAD`; исключить secret/state/logs/APK и чужие изменения, обновлённые Graphify warnings и ошибки push Codex сообщает пользователю, Antigravity отражает в `walkthrough.md` без сокрытия.

**Checkpoint:** приёмка не считается полной до фактического выполнения всех SC. Незатронутые backend/frontend/Android/Docker сервисы не пересобираются; при реальном изменении приложения выполнить соответствующие правила AGENTS, включая миграции до тестов и свежий APK после сборки.

## Граф зависимостей и порядок

```text
Подготовка → Общая основа → US1 → US2 → US3
                              └→ US5 (до T034; T035 — второе устройство)
US1 + US3 + T033 → US6 → US4
Общая основа → US7 guard T048–T050 → требуется для готового MVP
US7 guard + US4 → US7 acceptance T051–T053
Все реализованные истории, включая US7 → Общая приёмка
Полная приёмка → требует также T035
```

Внутри истории checks задают contract, затем модели/обработка, фасад, integration и live acceptance. Тесты должны воспроизводить meaningful failure до исправления; не создавать тесты, лишь повторяющие код. Задачи одного `state.py`/`worker.py`/bridge файла исполняются последовательно независимо от независимости историй. T035 может ждать устройство; это не мешает независимым US6/US4, но блокирует полную приёмку SC-007.

## Параллельные возможности по историям

| История | Допустимый пример после prerequisites |
|---|---|
| US1 | T012 и T013 помечены [P]: разные test-файлы; writers — в разных согласованных checkout, либо один пишет, другой проводит read-only review |
| US2 | Во время последовательного T021/T022 независимый reviewer читает tests/questions и контракт; два writers этой копии запрещены |
| US3 | После реализации T026/T027 независимое ревью criteria/evidence и чтение test_review могут идти одновременно; запускаемые проверки не меняют исходники |
| US5 | После T034 проверка T035 на втором физическом устройстве не блокирует чтение/ревью текущего checkout и независимые US6/US4 |
| US6 | После T040 read-only аудит защищённых rules и portable export может идти параллельно; native запись начинается только после safe release |
| US4 | После T045 отдельные read-only status запросы разных работ допускаются параллельно с наблюдением одного writer |
| US7 | Read-only quota polling во время одного активного worker не запускает другую модельную генерацию или writer |

Отдельные implementation tasks US2–US6 не имеют [P], поскольку изменяют общие файлы либо зависят от результата предыдущего шага. Маркер не означает разрешение конкурирующих процессов Antigravity.

## Покрытие требований

| FR | Задачи |
|---|---|
| FR-001 | T014–T019, T028, T051–T053, T055–T056 |
| FR-002 | T004, T014–T015 |
| FR-003 | T021–T024, T027 |
| FR-004 | T022, T033 |
| FR-005 | T004, T021, T026, T038–T044 |
| FR-006 | T009, T011, T026 |
| FR-007 | T012, T016, T025–T029 |
| FR-008 | T027, T029 |
| FR-009 | T010–T011, T020, T046 |
| FR-010 | T006–T007, T018, T038, T042 |
| FR-011 | T014–T015, T021, T023 |
| FR-012 | T005, T037, T045 |
| FR-013 | T005, T007, T037, T049 |
| FR-014 | T043–T047 |
| FR-015 | T001, T002, T049 |
| FR-016 | T008, T030–T035 |
| FR-017 | T030–T032, T034–T035 |
| FR-018 | T033, T035–T037 |
| FR-019 | T017, T031, T041–T042 |
| FR-020 | T037, T040–T042 |
| FR-021 | T005, T010, T036–T038, T042 |
| FR-022 | T025–T028, T039, T042, T044 |
| FR-023 | T048–T051 |
| FR-024 | T049–T052 |
| FR-025 | T050–T053 |

| SC | Задачи доказательства |
|---|---|
| SC-001 | T018–T019 |
| SC-002 | T020, T024 |
| SC-003 | T025, T029 |
| SC-004 | T007, T011, T020, T043, T054 |
| SC-005 | T018, T020 |
| SC-006 | T043, T046–T047 |
| SC-007 | T030, T034–T035 |
| SC-008 | T036, T039, T042 |
| SC-009 | T041–T042, T055 |
| SC-010 | T036, T038, T042 |
| SC-011 | T051–T052 |
| SC-012 | T051–T052 |
| SC-013 | T051–T052 |

## Стратегия реализации

Базовый прототип — фазы 1–3 (T001–T019): передача согласованной задачи и базовое ревью. Готовый MVP включает также quota guard T048–T050; до него Codex проверяет лимиты вручную и не объявляет автоматический контроль реализованным. Затем доказать общение/исправления, переносимость и независимое продолжение, завершить lifecycle, квотную приёмку и общие проверки. MVP не отменяет оставшиеся P1 пользователя: готовой связка считается по всему согласованному объёму, с точными непроверенными ограничениями.

Всего **57 задач**: подготовка 3, общая основа 8, US1 8, US2 5, US3 5, US5 6, US6 7, US4 5, US7 6, общая приёмка 4. Три задачи отмечены [P]: T003/T012/T013. T001–T047 сохранены, новый US7 — T048–T053, прежние завершающие задачи сдвинуты на T054–T057. Отдельная задача второго устройства — T035; при её недоступности нельзя отмечать полный успех переносимости.

Решение пользователя о реализации получено 2026-10-04. После обсуждения трёх неудачных исправлений пользователь выбрал продолжение малыми исправлениями с сохранением JSON/Markdown; каждое проверяется отдельно. Первое назначение: T001–T011 и read-only quota adapter T048; до независимой проверки Codex эти задачи не считаются завершёнными. Установка MCP и живые E2E выполняются на следующих зависимых шагах.


**Проверка Codex 2026-10-04, контрольная точка основы:** полный unittest discovery — 109 тестов, PASS (41.139s). Doctor PASS: отдельное Python 3.12.10 окружение и MCP SDK 2.3.0, native CLI обнаружены. T014 подтверждён offline create/approve, строгими task definitions/human decision, content scope digest, повторными запросами, fault injection event/snapshot/handoff и сохранением единственного work/approval. T048 подтверждён fake negative tests и настоящими read-only /model и /usage; transient 503 дал unknown и блокировал новый ход до fresh SUCCESS. T012 реализован частично, start/review пока отсутствуют. T004–T011 остаются неполностью принятыми; detached worker, MCP, lifecycle и автоматический guard ещё не готовы. Код и тесты написал Antigravity, готовые проверки независимо запускал Codex. На этой точке подтверждены только T001/T002/T003/T014/T048; полный объём 57 задач не завершён.


**Завершение текущего пакета при ограничении Codex, 2026-10-04:** Antigravity исправил классификацию PermissionError/OSError в compute_file_fingerprint: отказ доступа и ошибка ввода-вывода возвращают явный process_error вместо missing; целевой файл после ошибки метаданных не открывается. Независимый targeted regression PASS, полный набор 110 тестов PASS (43.511s). После изменения исходников Graphify обновлён без LLM: 11500 узлов, 23699 связей, 685 сообществ; те же три предупреждения парсера существующих Android/Gradle-файлов. Walkthrough фиксирует предыдущую точку основы 45018d1/109 тестов; обновление технического отчёта Antigravity относится к следующему пакету. Новые checkbox не закрывались, T006 и остальные компоненты основы ещё требуют общей приёмки. При расходе 95% пятичасового лимита Codex новые модельные задания остановлены по stay-within-limits; уже переданный пакет завершён и проверен. Следующий пакет — detached start/observer (T010/T015 с quota guard), затем MCP/остальные истории. Перед продолжением проверить свежие лимиты, Git и наличие изменений самостоятельного Antigravity; не создавать конкурирующего writer. Согласование полного объёма пользователя сохраняется.

**Возобновление и независимая приёмка пакета фонового запуска, 2026-10-04:** Antigravity реализовал программный `CollaborationCoordinator.start`, отдельный `worker_main.py` и базовый guard квоты до генерации и во время текущего/позднего хода. Codex подтвердил T010/T011/T015: настоящий detached Python-процесс в Windows, выход родителя без потери результата, различие launcher/observer PID в venv, точное время создания и token, предварительный checkpoint, повтор запроса без второй генерации, отказ при unknown ownership, stop до/после начала генерации, поздний результат и сохранение резервирования. Исправлены выявленные инверсии locks, ошибочная регистрация redirector PID, подавление checkpoint failures и регрессия fake-фикстуры. Terminal Antigravity в headless получил native permission denial; настройки/права не расширялись, его проверки остаются NOT_RUN. Код, тесты и технические документы написал Antigravity, независимую проверку выполнил Codex.

- Полный `python -m unittest discover -s tests/ai/collaboration -q`: **144 теста PASS, 213.907 с, exit 0**. Отдельно: quota — 9 PASS/32.611 с; canary worker — 3 PASS/0.647 с; canary observer — 1 PASS/4.139 с; две исправленные проверки start — 2 PASS/10.005 с. Это fake/integration проверки без генераций настоящей модели, не живые SC-001–SC-003.
- `python scripts/ai/collaboration_bridge.py --json doctor`: PASS, Python 3.12.10, MCP 2.3.0, native capabilities доступны. 261 защищённый tracked-файл сверены с HEAD без изменений; исходники приложения, миграции и Android не затронуты, их сборки/перезапуски не выполнялись. После финального прогона SHA-256 всех восьми изменённых файлов кода/тестов совпали; оставшихся observer/fake CLI процессов нет.
- Graphify после финального изменения кода: EXIT 0, `graph.json` 16 605 113 байт, 11 595 узлов, 24 101 связь, 688 сообществ. Первый запуск не смог атомарно заменить файл в sandbox; разрешённый повтор успешен. Сохранились предупреждения парсера `EquipmentPhotoEditorActivity.kt:71`, `EquipmentPhotoEditorModel.kt:25`, `native-artifacts.init.gradle:17`; семантическая LLM-разметка не запускалась, устаревшие названия сообществ не считаются обновлёнными. Датированная резервная копия не входит в коммит.

Теперь подтверждены **8 из 57 задач**: T001/T002/T003/T010/T011/T014/T015/T048. T049/T051 остаются частичными: guard ещё не подключён к reply/review fixes/resume, квотные MCP notice и сверка после восстановления не реализованы. T004–T009 требуют отдельной общей приёмки основы; затем T012/T013/T016/T018 — MCP/базовое ревью и настоящий stdio handshake, T019 — живой SC-001. `bootstrap`, вопросы/ответы, автоматический handoff/reconcile/recover и общая приёмка ещё впереди; T035 без второго устройства не закрывается. Обновлены `walkthrough.md`, `CHANGELOG.md`, `docs/DEVELOPMENT_ENVIRONMENT.md` и `docs/INDEX.md`. Полная связка 007 не объявляется готовой; перед следующим пакетом проверить свежую квоту и фактический checkout.

**Квотная точка остановки, 2026-10-04 18:48:26 UTC:** после завершения исполнителя read-only `/model` превысил таймаут 15 с; снимок `agy_cli` вернул `availability=unknown`, buckets пусты. Это не 0% и не 100%, новые генерирующие задания не передаются до свежей успешной проверки применимых окон. Последний успешный снимок до финальной правки документов был 18:38:12 UTC (weekly 62.489%, five-hour 22.114%); его нельзя считать текущим разрешением. Исходники проверенного пакета сохранены, процесс исполнителя завершён, далее выполняется только независимая Git-фиксация результата.
