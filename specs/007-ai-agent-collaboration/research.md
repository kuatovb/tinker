# Исследование: локальная связка агентов

**Дата:** 2026-10-03. **Объём:** решения для [согласованной спецификации](spec.md); код моста ещё не написан.

## 1. Интерфейс Codex

**Решение:** локальный stdio MCP server только для Codex, быстрый dispatch и отдельный bounded status. Codex остаётся контроллером в текущем диалоге, второй модельный процесс Codex не запускается.

**Основание:** официальная документация поддерживает локальные MCP-записи и CLI add/get/remove; длительную генерацию отделяем от MCP-вызова с конечным сроком ожидания. Проверено локальным `codex mcp add --help`. [MCP](https://learn.chatgpt.com/docs/extend/mcp), [CLI](https://learn.chatgpt.com/docs/developer-commands).

**Альтернативы:** Agents SDK/API key и вложенный Codex добавляют ненужную авторизацию и расход лимитов. Cookbook с codex mcp-server не подходит: в установленной CLI 0.160.0 такой команды нет; используем Codex как MCP client.

## 2. Обмен с Antigravity

**Решение:** один detached Python worker на ход, stream-json stdin/stdout, root-object JSON schema; EOF после одного user-сообщения. Следующий ответ/замечание запускает новый процесс с точным conversation ID.

**Основание:** documented EOF завершает текущий ход, stream отдаёт result; --conversation продолжает выбранную сессию; structured_output доступен при schema. Session counters накопительные, поэтому сроки контролируем отдельно. SUCCESS требует дальнейшего ревью, ERROR/empty/denied не завершают задачу. [Headless](https://www.antigravity.google/docs/cli/headless/).

**Альтернативы:** глобальный --continue смешивает работы; постоянно открытый stdin требует лишнего daemon lifecycle; парсинг свободного текста ненадёжен.

## 3. Режим, разрешения и остановка

**Решение:** accept-edits только для запуска уже согласованного задания. Глобальные agentMode, permissions, rules и skills Antigravity не меняются. Запрос дополнительного согласования возвращается в Codex, фактическое Proceed не имитируется. [Modes](https://www.antigravity.google/docs/cli/modes/), [Permissions](https://www.antigravity.google/docs/permissions?tab=cli).

**Основание:** режим редактирования не отменяет permissions shell. Отказ headless-инструмента должен быть явной блокировкой. Отсутствие документации о гарантированном прекращении всех инструментов при forcedkill означает execution_unknown до проверки; остановка v1 — на границе текущего хода.

**Установленные расхождения:** local agy 1.2.16 help показывает timeout 0, а headless docs — 5m; современная schema требует root object. Изменения background completion описаны в [официальном changelog](https://github.com/google-antigravity/antigravity-cli/blob/main/CHANGELOG.md). Выбираем внешний deadline; поддержку timeout в stream-input проверяем при реализации. Возможности CLI доказаны help, реальное stream/schema поведение пока не запускалось.

## 4. Зависимости и переносимость

**Решение:** отдельное Python 3.12+ окружение, фиксированный mcp 2.3.0; без зависимости от личного Graphify venv. PyPI на дату проверки публикует 2.3.0 как latest stable; docs SDK описывают v2 и MCPServer. [PyPI](https://pypi.org/project/mcp/), [SDK](https://py.sdk.modelcontextprotocol.io/).

**Основание:** для процессов/JSON/CLI/хранилища хватает stdlib. Bootstrap использует официальную CLI для собственной MCP-записи, compare/no-op и отказ при коллизии, не переписывает весь TOML. Абсолютные пути генерируются локально, проектные ссылки относительные.

Native EXE + argv + UTF-8 stdin поддерживают пробелы/кириллицу без сборки shell-строки. Windows может интерпретировать bat/cmd через shell даже при shell=False: такие overrides v1 отклоняет с инструкцией выбора EXE. [Python subprocess](https://docs.python.org/3.12/library/subprocess.html). Для канонических путей и file lock используем [pathlib](https://docs.python.org/3.12/library/pathlib.html) и [msvcrt](https://docs.python.org/3.12/library/msvcrt.html).

**Альтернативы:** персональные C/D пути ломают перенос; глобальное tool environment связывает разные инструменты; автоматический upgrade «latest» в bootstrap делает поведение неповторяемым. Перед реализацией версию SDK снова проверяем, обновление фиксируем вместе с контрактными проверками.

## 5. Состояние, секреты и независимость

**Решение:** atomic JSON + нормализованные события + handoff.md в игнорируемом каталоге. Reservation принадлежит работе, execution lock — живому worker; возраст файла не позволяет снять блокировку. Snapshot формируется до исполнения и после результата без модели Codex.

**Основание:** пользователь должен продолжить native Antigravity при недоступном Codex. Markdown и relative artifact links достаточны; auth/session IDs остаются устройству. Reconcile опирается на реальные Git metadata, хеши несекретных файлов и выборочное ревью. Рабочие ограничения моста не внедряются в native Antigravity.

**Альтернативы:** БД/очередь/облачный scheduler избыточны; копирование raw streams/diff/config опасно для секретов; автоудаление lock после timeout может создать двух writers. Job Objects управляют назначенными процессами, но не дают доказательства остановки внешнего исполнителя; forcedkill не выбран для v1. [Microsoft Job Objects](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects).

## 6. Состояние исследования

Неопределённостей, требующих нового пользовательского требования, нет. Реализация должна проверить wire schema/EOF, background completion, поздние ошибки и наличие native EXE на втором устройстве. Это acceptance risks с явным отрицательным исходом, а не допущения об успешной проверке. Gradle 9.7.1 уже закреплён Wrapper; AGP/Kotlin/KSP migration остаётся отдельным долгом, см. [исследование Android](https://github.com/kuatovb/inventory_system/blob/7c308bedb99df3e246b901d986bee367b6528fec/docs/ANDROID_BUILD_TOOLCHAIN_RESEARCH.md).

## 7. Контроль лимитов исполнителя — дополнение 2026-10-04

**Решение:** read-only print `/model` и `/usage` отдельно от генерации. По официальному changelog они отвечают без agent turn и расхода квоты; проверка установленной 1.2.16 подтвердила SUCCESS, num_turns=0 и total_tokens=0. [Changelog](https://github.com/google-antigravity/antigravity-cli/blob/main/CHANGELOG.md), [CLI reference](https://www.antigravity.google/docs/cli/reference/).

Фактический `/usage` JSON имеет `command.data.groups[].buckets[]`: группировку, remaining_fraction, reset_time и window; `/model` — id/label/effort/is_default. Нормализуем только нужную группу и окна, без identity/auth. На момент проверки Gemini weekly remaining — около 91.5%, five-hour — около 96.6%; Claude/GPT окна — 100%. Это снимок текущего аккаунта, не постоянная конфигурация или гарантия последующего остатка.

Первый запуск в ограниченной среде вернул ERROR без надёжной quota. Повтор с доступом к локальной CLI среде успешен; внешнее ожидание ограничено. Это подтверждает необходимость unknown outcome и внешнего timeout, а не fallback на session tokens. Не читать keyring/auth-файлы или использовать private provider API.

**Основание:** квота по группе модели/нескольким окнам отличается от накопительных usage counters сессии. Проверка перед каждым генерирующим ходом, interval 60s и default threshold 20% позволяют уведомить и не отправлять следующий ход. Эти defaults — решение моста, не правило провайдера; availability одного окна не заменяет другое. Если группа/reset/source сомнительны, новые ходы ожидают достоверной проверки.

**Альтернативы:** вопрос модели «сколько осталось» или «где остановилась» расходует дефицитную квоту и не подтверждает факты; анализ накопленных токенов не даёт account remaining; автоматический paid reserve/model switch выходит за запрос. Точка остановки строится из сохранённых событий и независимой сверки файлов, активная операция остаётся под прежними правилами safe handoff.
