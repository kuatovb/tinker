# tinker

Проект для совместной работы нескольких ИИ: подготовка технического задания, исполнение задач, вопросы, независимая проверка результата и сохранение контекста между сессиями.

## Статус

Проект находится на раннем этапе разработки. Реализованная пара — Codex и Antigravity; другие исполнители потребуют отдельных адаптеров. Разработка автоматической связки сейчас отложена, исходники и задачи сохранены для будущего продолжения.

Подтверждены **8 из 57 задач**. Есть координатор create/approve, Python API устойчивого запуска, фоновый наблюдатель, хранилище состояния, защита от повторного исполнения и часть контроля квот. CLI/MCP start, bootstrap, обмен вопросами и ответами, review/resume, handoff/reconcile/recover и сквозная приёмка ещё не готовы. Наличие зависимости MCP не означает готовый или зарегистрированный сервер.

## Документация

- [Спецификация](specs/007-ai-agent-collaboration/spec.md)
- [План разработки](specs/007-ai-agent-collaboration/plan.md)
- [Задачи](specs/007-ai-agent-collaboration/tasks.md)
- [Контракт](specs/007-ai-agent-collaboration/contracts/bridge.md)
- [Текущее состояние и проверка](docs/STATUS.md)

## Проверка (Windows, Python 3.12+)

```powershell
python -m venv .venv
./.venv/Scripts/python.exe -m pip install -r scripts/ai/collaboration-requirements.txt
./.venv/Scripts/python.exe -m unittest discover -s tests/ai/collaboration -q
./.venv/Scripts/python.exe scripts/ai/collaboration_bridge.py --help
```

Тесты используют fake CLI и временные Git-копии. Для них не нужны модельные генерации и платные E2E. Нативная диагностика доступна отдельно: python scripts/ai/collaboration_bridge.py --json doctor; она требует собственных локальных установок CLI.

Закреплённая зависимость — mcp==2.3.0. Журналы, учётные данные, пользовательская конфигурация ИИ и виртуальные окружения не публикуются. Пути инструментов определяются на текущем устройстве.
