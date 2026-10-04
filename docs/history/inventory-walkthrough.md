# Историческая приёмка связки в Inventory

Сохранено 2026-10-05. Проверки ниже относятся к старому checkout; результаты переноса записаны отдельно.

# Отчёт о реализации этапа фонового запуска и квотного мониторинга связки агентов (Feature 007)

> **Статус проверок**: `NOT_RUN` со стороны Antigravity.
> Общий регламент разработки не запрещает Antigravity запуск проверок; в текущей headless-сессии запуск команд заблокирован системными ограничениями среды выполнения. Поэтому тесты имеют статус `NOT_RUN` со стороны Antigravity, а их запуск и независимая верификация выполнены Codex в изолированном виртуальном окружении моста.
>
> **Статус контроля версий (Git)**: текущий пакет изменений будет зафиксирован и отправлен Codex (`git push origin HEAD`) после завершения проверки документов. Исторический коммит основы: `45018d1` (`feat(ai): add verified collaboration foundation`).

---

## 1. Первопричина и точечное исправление фикстуры (Surgical Fix)

В ходе независимого полного прогона тестов Codex (144 теста за 226.056 с) ранее был зафиксирован единственный сбой:
- Тест: `test_worker.TestWorkerAndQuota.test_canary_token_sanitization_in_tool_error`
- Ожидалось: `permission_denied`
- Фактически: `fatal_step_error`

**Первопричина**: расхождение текста ошибки в тестовой фикстуре [`fake_cli.py`](../../tests/ai/collaboration/fake_cli.py). Ранее строковый литерал `"Permission denied for CANARY_TOOL_TOKEN_54321"` был заменён на `"Fatal step error for CANARY_TOOL_TOKEN_54321"`, что нарушило классификацию ошибки в существующем тесте.

**Решение**:
1. В [`fake_cli.py`](../../tests/ai/collaboration/fake_cli.py) точечно восстановлен исходный литерал `"Permission denied for CANARY_TOOL_TOKEN_54321"`.
2. Архитектурная безопасность подтверждена: метод `normalize_stream_event` в [`worker_main.py`](../../scripts/ai/collaboration/worker_main.py) выполняет рекурсивную проверку `_contains_sensitive_content(raw_event)` по всем полям сырого события до применения allowlist, поэтому событие блокируется (`sensitive_output_blocked`) независимо от нативной классификации ошибки.
3. Сразу после восстановления фикстуры сформирован маркер `logs/ai/collaboration/implementation-007/source-ready.md` (`"Fixture restored. Source/tests frozen for independent checks."`), а весь исходный код и тесты были заморожены для независимой верификации Codex.

---

## 2. Реализованная функциональность этапа

### А. Устойчивый запуск (`CollaborationCoordinator.start`)
- **Быстрый целевой отклик**: метод `start()` возвращает `work_id` и `turn_id` программы-исполнителя без ожидания генерации модельного ответа (отклик < 2.0 с подтверждён в контролируемом тесте с fake CLI; для нативных синхронных запросов квоты действует ограниченный IO таймаут без безусловной гарантии моментального ответа).
- **Fail-closed резервирование и чекпоинт**: предварительная фиксация намерения (`status="in_progress"`), CAS-резервирование исполнителя (`worker_token`) и создание структуры хода (`Turn с UUID turn_id`) производятся под файловой блокировкой `msvcrt.locking` до вызова `subprocess.Popen`.
- **Идемпотентность и защита от коллизий**: повторный вызов `start()` для активного хода возвращает детерминированный ответ без повторного запуска процесса. Повтор при невалидном или неизвестном резервировании возвращает `execution_unknown` с нулевым числом новых генераций.

### Б. Изолированный наблюдатель (`worker_main.py`)
- **Отсоединённый процесс**: наблюдатель запускается через локально определённый Python-интерпретатор (с возможностью переопределения и fallback на `sys.executable`) и явный список аргументов `argv` с флагами `subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW` (без использования оболочки `shell=True`).
- **Windows Virtualenv Redirector Lineage**: корректно разрешено различие PID внешнего лаунчера-редиректора (`outer PID`) и внутреннего интерпретатора Python (`inner observer PID`). Проверка `verify_process_lineage` валидирует direct PID либо `os.getppid` непосредственного родителя-лаунчера и точное время создания (`creation_time` через `GetProcessTimes`), токены задания и хода без обхода окружения. Окружение `child_env` локально настраивает корень `PYTHONPATH`, сохраняя глобальные системные настройки.
- **Архитектура блокировок**:
  - `execution.lock` — эксклюзивная файловая блокировка, удерживаемая процессом наблюдателя на всё время хода.
  - `ownership.lock` — краткосрочная файловая блокировка для атомарных CAS-операций резервирования в `reservation.json` (снимки состояния и события защищаются `store.lock` отдельно).
- **Строгая валидация CLI-префикса**: аргумент `--cli-args-prefix` проверяется на соответствие списку строк; некорректный JSON или нестроковые элементы отклоняются до старта исполнения.

### В. Жизненный цикл и переходы состояний
- **Строгий `SUCCESS`**: статус успешного завершения хода фиксируется только при одновременном соблюдении условий: терминальный результат с валидированным `structured_output`, нормальный код возврата процесса (0), получение сигналов EOF от обоих потоков чтения (stdout/stderr), отсутствие ошибок доступа (`permission_denied`), фатальных ошибок и незавершённых действий (pending tools).
- **Состояние `in_review`**: по завершении хода задание переводится в статус `in_review` (а не `complete`), резервирование удерживается до проведения независимого ревью и согласования.
- **Обработка тайм-аута**: фиксируется статус `execution_unknown`, воркер продолжает наблюдение исходного CLI-процесса до его фактического выхода И закрытия обоих потоков чтения (readers EOF), без принудительного `kill` или автоматических повторов. Незавершённые действия сохраняют статус `execution_unknown` и требуют ручного восстановления; запоздалый результат (`late result`) помечается флагом `requires_reconcile=True`, а резервирование не сбрасывается автоматически (no auto-release).
- **Остановка по сигналу `stop`**: остановка выполняется на границе хода (а не отдельных шагов); активный процесс не объявляется остановленным до завершения, промежуточные данные и артефакты сохраняются.

### Г. Квотный мониторинг хода (US7 / T049, T051 частично)
- **Периодический монитор**: фоновый поток проверяет остаток квот с интервалом 60 с и порогом малого остатка 20% (0.20).
- **Блокировка генерации**: состояния `quota_low`, `quota_exhausted` или `quota_unknown` блокируют отправку следующего промпта.
- **Безопасные уведомления**: формируются дедуплицированные записи `quota_notice`, исключающие дублирование сообщений.
- **Отказоустойчивость**: ошибки ввода-вывода при сохранении снимка квоты (`save_quota_snapshot`) переводят операцию в `checkpoint_error` и не подменяются успешным статусом.

---

## 3. Подтверждённые результаты независимой верификации Codex (Evidence)

Все проверки исходного кода и окружения успешно выполнены Codex в изолированном виртуальном окружении (`logs/ai/collaboration/final-verification.json`):

1. **Полный набор тестов связки (Full Suite PASS)**:
   - Команда: `python -m unittest discover -s tests/ai/collaboration -q`
   - Результат: **144 tests PASS** (время выполнения: 213.907 с, 0 ошибок, 0 сбоев, exit code 0).
2. **Адресные проверки канареек**:
   - `canary_worker`: **3 tests PASS** (0.647 с, exit code 0).
   - `canary_observer`: **1 test PASS** (4.139 с, exit code 0).
3. **Набор тестов квот**:
   - `quota_suite`: **9 tests PASS** (32.611 с, exit code 0).
4. **Адресные тесты запуска**:
   - `corrected_start_regressions`: **2 tests PASS** (10.005 с, exit code 0).
5. **Диагностика окружения (`doctor`)**:
   - Команда: `python scripts/ai/collaboration_bridge.py --json doctor` (глобальный флаг `--json`).
   - Результат: **PASS** (Python 3.12.10, MCP SDK 2.3.0).
6. **Граф кодовой базы (Graphify)**:
   - Обновление после финальной фикстуры выполнено успешно (EXIT 0): размер `graph.json` 16 605 113 байт, **11 595 узлов, 24 101 связь, 688 сообществ** (151 метка хабов переименована).
   - Предупреждения парсера относятся к предсуществующим файлам (`EquipmentPhotoEditorActivity.kt:71`, `EquipmentPhotoEditorModel.kt:25`, `scripts/android/native-artifacts.init.gradle:17`) и не затрагивают мост. Семантическая разметка не запускалась (`not_run`).
7. **Целостность защищённых файлов**:
   - Проверено 261 отслеживаемый файл: хэши строго совпадают с HEAD (`changed: []`).
   - Код приложений (backend, frontend, Android), схема базы данных и Docker-конфигурации не модифицировались.
8. **Статус задач реализации**:
   - Codex принимает задачи **T010**, **T011** и **T015** (в дополнение к подтверждённым **T001**, **T002**, **T003**, **T014**, **T048**). Задачи **T049** и **T051** реализованы частично. Задача **T035** остаётся открытой. Полный bridge и MCP-сервер ещё не готовы.

---

## 4. Список файлов

### Исходный код и фикстуры (заморожены)
- [`scripts/ai/collaboration/coordinator.py`](../../scripts/ai/collaboration/coordinator.py) — реализация `CollaborationCoordinator.start`, fail-closed проверка резервирования, валидация replay `in_progress`, целевой отклик.
- [`scripts/ai/collaboration/worker_main.py`](../../scripts/ai/collaboration/worker_main.py) — автономный процесс наблюдателя, `execution.lock`, `ownership.lock`, рекурсивная валидация чувствительных данных, валидация `--cli-args-prefix`.
- [`scripts/ai/collaboration/worker.py`](../../scripts/ai/collaboration/worker.py) — потоковая обработка NDJSON, запуск дочерних процессов, обработка кодов возврата и состояний.
- [`scripts/ai/collaboration/state.py`](../../scripts/ai/collaboration/state.py) — модель состояния, валидация процессов Windows redirector lineage по PID и времени создания, блокировки `msvcrt`.
- [`scripts/ai/collaboration/environment.py`](../../scripts/ai/collaboration/environment.py) — диагностика окружения `doctor`, фоновый квотный монитор.
- [`tests/ai/collaboration/fake_cli.py`](../../tests/ai/collaboration/fake_cli.py) — тестовый эмулятор CLI; восстановлен литерал ошибки canary-токена `"Permission denied for CANARY_TOOL_TOKEN_54321"`.
- [`tests/ai/collaboration/test_start.py`](../../tests/ai/collaboration/test_start.py) — тесты старта, проверки резервирования, разделения проверок stop и replay.
- [`tests/ai/collaboration/test_worker.py`](../../tests/ai/collaboration/test_worker.py) — тесты жизненного цикла воркера, санитизации канареек и валидации префикса аргументов.
- [`tests/ai/collaboration/test_quota.py`](../../tests/ai/collaboration/test_quota.py) — тесты квотного мониторинга, ошибок сохранения и блокировки генерации.
- `logs/ai/collaboration/implementation-007/source-ready.md` — маркер готовности и заморозки исходного кода/тестов.

### Документация проекта
- [`docs/DEVELOPMENT_ENVIRONMENT.md`](https://github.com/kuatovb/inventory_system/blob/7c308bedb99df3e246b901d986bee367b6528fec/docs/DEVELOPMENT_ENVIRONMENT.md) — обновлены данные графа кодовой базы, синтаксис команды `doctor` и описание этапа 007.
- [`docs/INDEX.md`](https://github.com/kuatovb/inventory_system/blob/7c308bedb99df3e246b901d986bee367b6528fec/docs/INDEX.md) — актуализированы ссылки и аннотации к плану и задачам функции 007.
- [`CHANGELOG.md`](https://github.com/kuatovb/inventory_system/blob/7c308bedb99df3e246b901d986bee367b6528fec/CHANGELOG.md) — добавлена запись о фоновом запуске, наблюдателе и квотном контроле в секции `[Не выпущено]`.
- [`walkthrough.md`](https://github.com/kuatovb/inventory_system/blob/7c308bedb99df3e246b901d986bee367b6528fec/walkthrough.md) — настоящий итоговый отчёт о реализации и состоянии этапа.

---

## 5. Границы реализации и оставшиеся ограничения

1. **Программный API координатора**: метод `start()` доступен программатически; команды `start` в CLI-парсере `collaboration_bridge.py` и инструментах MCP-сервера ещё не подключены.
2. **Команды-заглушки CLI**: команды `bootstrap`, `serve`, `handoff`, `export`, `reconcile`, `recover` возвращают явные заглушки с описанием ожидающих этапов.
3. **Многоходовое ревью и handoff**: многоходовой цикл передачи замечаний и исправлений (US2), жизненный цикл передачи контекста (handoff) и перенос на второе Windows-устройство остаются в очереди разработки.
4. **Контроль квот до MCP**: до полной интеграции MCP-сервера Codex выполняет ручную проверку квот перед отправкой пакетов команд. Используется модель per-run `gemini-3.8-flash-high` без изменения глобальных настроек и платных опций.
5. **Автономность Antigravity**: нативный CLI и расширения Antigravity полностью независимы и функционируют штатно без запущенного моста. Пользователь может продолжать разработку непосредственно в Antigravity.
6. **Фиксация в Git**: коммит и пуш текущего этапа выполняет Codex после завершения проверки документов.

---

## 6. Команды для независимой проверки Codex

```powershell
# 1. Диагностика окружения (с глобальным флагом --json)
python scripts/ai/collaboration_bridge.py --json doctor

# 2. Модульные тесты квот
python -m unittest tests/ai/collaboration/test_quota.py -q

# 3. Модульные тесты запуска
python -m unittest tests/ai/collaboration/test_start.py -q

# 4. Модульные тесты воркера
python -m unittest tests/ai/collaboration/test_worker.py -q

# 5. Полный набор тестов связки
python -m unittest discover -s tests/ai/collaboration -q
```
