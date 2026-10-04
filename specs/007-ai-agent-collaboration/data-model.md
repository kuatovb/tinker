# Модель данных и переходов

**Статус:** проектирование [функции 007](spec.md). JSON/Markdown — локальные файлы, миграций PostgreSQL нет.

## Сущности

| Сущность | Поля и ограничения |
|---|---|
| Work | UUID work_id; schema_version=1; revision; goal; artifact_refs; allowed_files; allowed_actions; acceptance; task_ids; state; timeout_seconds; created_at/updated_at; approval_id; baseline; current_turn; last_result; last_review; stop_requested; handoff |
| Approval | UUID; work_id; scope_digest; источник и текст фактического решения пользователя; время; одобренные действия. Новый scope требует нового решения, модель сама approval не создаёт |
| Message | UUID message_id; work_id; turn_id; seq; sender/recipient; kind=assignment/question/answer/result/review; reply_to; safe content; время. answer/review адресуют конкретное ожидаемое сообщение |
| Turn | UUID; work_id; локальный conversation_id; worker token; PID/start_time; начальный revision; deadline; outcome; terminal_received; process_exited; pending_tools. Один активный ход на работу |
| ExecutorResult | kind=question/implementation_result/blocked; work_id/turn_id; summary; claimed_files; checks; remaining_actions; question; block_reason. Все результаты — заявления исполнителя |
| CheckEvidence | ID; criterion/task_id; status=passed/failed/not_run; команда или описание; exit_code, если выполнялась; безопасное краткое evidence; относительный путь отчёта при наличии. not_run содержит причину, не превращается в passed |
| Review | ID; work_id; проверенный baseline/current fingerprint; criteria results; scope violations; remarks; verdict=accepted/changes_requested/blocked; reviewer=Codex. accepted относится к точной текущей ревизии |
| LocalEnvironment | device_id; канонический root; native CLI/Python пути и capabilities; bootstrap entry name; локальный session ID. Не входит в portable export |
| CheckoutOwnership | checkout_key; work_id; reservation; execution_lock; worker token; PID/start_time; execution_known; release evidence. Блокирует других workers этой связки |
| Handoff | revision; goal; relative artifacts; выполненные и оставшиеся задачи; safe check summaries; boundary; stop/release evidence; requires_reconcile. Переносимый экспорт исключает LocalEnvironment и session ID |
| QuotaSnapshot | ID; checked_at/source/version; выбранный model_id/group; bucket id/window/remaining_fraction/reset_at; availability=available/low/exhausted/unknown; threshold; uncertainty reason. Окна не объединяются с session usage, auth/account identity не сохраняются |
| QuotaNotice | ID; work/task refs; snapshot_id; kind=quota_low/quota_exhausted/quota_unknown; checkpoint revision; последний подтверждённый этап; pending operation boundary; delivered event seq. Повтор одного события не дублируется |

`remaining_fraction` — число 0..1 либо null при неизвестности; недопустимое число даёт unknown, без clamping к придуманному остатку. Threshold по умолчанию 0.20, допустим 0..1; interval по умолчанию 60 секунд, положительный. Свежесть перед новым ходом — не старше interval, с новой проверкой при достижении срока reset; timestamp источника не выдумывается из отсутствующего поля. В state хранятся полученные значения и время наблюдения с явными ограничениями достоверности.

quota_low/exhausted/unknown блокируют новый ход и создают safe checkpoint/event. Для активной работы сохраняются implementing+stop_requested либо error+quota reason/execution_unknown до действительного окончания. Для неактивной — stopped/error с quota reason. Отдельное новое состояние complete не появляется; уведомление о квоте не освобождает ownership. Resume требует новой доступной quota, reconciliation и действующего approval.

Baseline хранит HEAD/ветку и пути staged/unstaged/untracked, отдельные index blob IDs и worktree fingerprints несекретных файлов в scope и всех исходно изменённых посторонних файлов, включая untracked. Fingerprint учитывает существование/удаление и хеш: изменение уже dirty файла не теряется при прежнем наборе путей. Для секретных файлов доступны только Git/файловые metadata без чтения содержимого; подозрительное изменение блокирует принятие до безопасной проверки. Полный diff и содержимое секретных файлов автоматически не сохраняются. Scope digest включает goal, artifact revisions, file/action bounds и acceptance. Артефакты читаются только по проверенным относительным ссылкам внутри root.

## Проверка входов

- ID проверяются как UUID, revision — целое положительное число. Дубликат request_id возвращает прежний исход и не запускает второй ход.
- Цель, критерии и summary непустые. Timeout положительный; неизвестные enums и отсутствующие поля — protocol_error.
- Artifact/file paths относительные; абсолютные пути, выход через `..`, symlink/junction за root и секретные auth/env/key файлы отклоняются.
- Структурированная claimed_files не заменяет фактический Git diff. Неизвестный task_id либо вопрос другой работы отклоняется.
- Технический ответ не расширяет scope. Новые требования/доступ ожидают Approval с новым digest.
- Секреты не читаются для контекста. Raw stdout/stderr/tool_info не сохраняются: в журнал попадают только проверенные допустимые поля и безопасные категории ошибок. Подозрительное содержимое не возвращается в MCP, фиксируется sensitive_output_blocked.

## Состояния

| Состояние | Смысл и допустимый следующий шаг |
|---|---|
| awaiting_approval | Задание сохранено, approved digest отсутствует; после настоящего согласования можно start |
| implementing | Один активный ход; чтение status/stop/handoff request разрешено, второй prompt запрещён |
| needs_answer | Исполнитель завершил ход с вопросом; answer продолжает ту же session либо ожидается пользователь |
| in_review | Ход завершён; Codex проверяет diff/checks; fixes → implementing, accepted → complete |
| complete | Проверена актуальная ревизия, критерии выполнены; reservation освобождена |
| error | Ошибка с code/detail; при execution_unknown новый writer запрещён до подтверждённого завершения и сверки |
| stopped | Текущий ход закончился, новые ходы запрещены; resume сохраняет work_id и требует проверки текущих файлов |
| handed_off_to_user | Исполнение закончено, checkpoint доступен, reservation освобождена; самостоятельный Antigravity работает по своим правилам |

stop_requested/handoff_requested — флаги, а не преждевременная смена implementing. Пока worker пишет, статус остаётся implementing либо error/execution_unknown. После request stop успешный результат сохраняется, но не запускает следующий ход; итоговое состояние stopped, исход результата остаётся доступен для resume/review. Handoff request после подтверждённого завершения переводит в handed_off_to_user.

Reconcile из handed_off_to_user/stopped/error/complete не запускает модель или запись: сравнивает факты и оставляет работу in_review либо awaiting_approval. После native изменений review повторяется; resume требует принятого решения о выполненном объёме, действующего scope approval и безопасного ownership. Потерянную session нельзя заменить «последней»: создаётся явный новый локальный conversation с portable context после согласования восстановления.

## Ошибки

Коды: approval_required, checkout_busy, revision_conflict, scope_violation, permission_denied, provider_error, process_error, protocol_error, empty_result, session_unavailable, timeout, execution_unknown, tool_unavailable, sensitive_output_blocked, reconciliation_required. Denied tool либо отсутствие обязательной проверки не дают complete. Provider ERROR имеет приоритет над текстом ответа и exit 0.

При deadline фиксируется timeout и unknown boundary; worker продолжает наблюдение текущей операции, reservation остаётся. Если позже получен однозначный result+exit и нет pending tools, результат сохраняется как поздний; автоматического продолжения нет, необходим reconcile/review. При crash одного worker PID/lock недостаточно: проверяется реальный исполнитель и фиксируется recovery evidence.

## Надёжность хранения

Папка работы — `logs/ai/collaboration/works/<work_id>/`. State mutation идёт под file lock, snapshot записывается во временный файл в той же папке, flush/fsync, затем atomic replace. Event содержит seq/revision и принадлежность работе; частичная последняя запись явно обнаруживается при восстановлении. Snapshot — авторитетное состояние, журнал сверяется с ним; повреждение не подменяется пустой «новой работой».

До start snapshot и handoff уже существуют. После каждого достоверного исхода обновляются оба. Работа detached worker не зависит от активного вызова Codex; offline status/stop/handoff не обращаются к провайдерам. Persistent reservation не снимается по времени. Release требует завершённого execution и сохранённого checkpoint либо задокументированного ручного восстановления.
