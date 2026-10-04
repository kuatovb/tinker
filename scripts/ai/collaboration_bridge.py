#!/usr/bin/env python3
"""
tinker Collaboration Bridge CLI & MCP Entry Point (Feature 007).
Usage: python scripts/ai/collaboration_bridge.py <command> [options]
"""

import os
import sys
import json
import argparse
from pathlib import Path
from typing import Dict, Any

# Ensure project root is in sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.ai.collaboration.state import (
    WorkStore,
    CollaborationError,
    is_valid_uuid,
)
from scripts.ai.collaboration.environment import (
    find_git_root,
    discover_executable,
    doctor as run_doctor,
    QuotaAdapter,
)

EXIT_OK = 0
EXIT_INVALID_INPUT = 2
EXIT_BLOCK = 3
EXIT_ERROR = 4


def format_output(data: Dict[str, Any], as_json: bool, text_summary: str):
    if as_json:
        sys.stdout.write(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    else:
        sys.stdout.write(text_summary + "\n")
    sys.stdout.flush()


def cmd_doctor(args) -> int:
    try:
        diag = run_doctor(
            project_root=args.project_root,
            agy_exe=args.agy_exe,
            codex_exe=args.codex_exe,
            python_exe=args.python_exe,
            check_android=args.check_android
        )
        as_json = getattr(args, "json", False)

        lines = ["=== Отчёт диагностики среды (Doctor) ==="]
        for k, v in diag["diagnostics"].items():
            lines.append(f"- {k}: {v}")

        if diag["errors"]:
            lines.append("\nОбнаруженные проблемы:")
            for err in diag["errors"]:
                lines.append(f"  * {err}")

        format_output(diag, as_json, "\n".join(lines))
        return EXIT_OK if diag["ok"] else EXIT_INVALID_INPUT
    except Exception as e:
        sys.stderr.write(f"Ошибка выполнения doctor: {e}\n")
        return EXIT_ERROR


def cmd_status(args) -> int:
    as_json = getattr(args, "json", False)
    work_id = args.work_id
    if not work_id or not is_valid_uuid(work_id):
        format_output(
            {"ok": False, "error": "Некорректный или отсутствующий UUID work_id"},
            as_json,
            "Ошибка: параметр --work-id должен быть валидным UUID"
        )
        return EXIT_INVALID_INPUT

    try:
        root = find_git_root(override=args.project_root)
        base_dir = root / "logs" / "ai" / "collaboration"
        store = WorkStore(base_dir=base_dir, work_id=work_id, checkout_root=root)

        work = store.load_snapshot()
        latest_seq = store.get_latest_event_seq()

        data = {
            "ok": True,
            "work_id": work.work_id,
            "revision": work.revision,
            "state": work.state,
            "goal": work.goal,
            "stop_requested": work.stop_requested,
            "handoff_requested": work.handoff_requested,
            "operation_boundary": work.operation_boundary,
            "last_event_seq": latest_seq,
            "updated_at": work.updated_at,
        }

        text = (
            f"Работа: {work.work_id}\n"
            f"Состояние: {work.state} (ревизия {work.revision})\n"
            f"Цель: {work.goal}\n"
            f"Граница операции: {work.operation_boundary or 'отсутствует'}\n"
            f"Флаг остановки: {'запрошена' if work.stop_requested else 'нет'}\n"
            f"Последнее событие: seq={latest_seq}"
        )
        format_output(data, as_json, text)
        return EXIT_OK
    except CollaborationError as e:
        format_output(
            {"ok": False, "error_code": e.code, "message": e.message},
            as_json,
            f"Ошибка [{e.code}]: {e.message}"
        )
        if e.code == "corruption_detected":
            return EXIT_ERROR
        return EXIT_INVALID_INPUT
    except Exception as e:
        sys.stderr.write(f"Непредвиденная ошибка: {e}\n")
        return EXIT_ERROR


def cmd_stop(args) -> int:
    as_json = getattr(args, "json", False)
    work_id = args.work_id
    if not work_id or not is_valid_uuid(work_id):
        format_output(
            {"ok": False, "error": "Некорректный или отсутствующий UUID work_id"},
            as_json,
            "Ошибка: параметр --work-id должен быть валидным UUID"
        )
        return EXIT_INVALID_INPUT

    try:
        root = find_git_root(override=args.project_root)
        base_dir = root / "logs" / "ai" / "collaboration"
        store = WorkStore(base_dir=base_dir, work_id=work_id, checkout_root=root)

        work = store.load_snapshot()
        work.stop_requested = True
        work.revision += 1
        seq = store.commit_mutation(
            work=work,
            event_type="stop_requested",
            payload={"work_id": work_id, "revision": work.revision},
            request_id=f"stop_{work_id}_{work.revision}"
        )

        data = {
            "ok": True,
            "work_id": work_id,
            "revision": work.revision,
            "state": work.state,
            "stop_requested": True,
            "event_seq": seq,
            "message": "Stop flag set successfully. Execution will stop at turn boundary."
        }
        text = (
            f"Остановка запрошена для работы {work_id}.\n"
            f"Текущее состояние: {work.state}. Остановка произойдёт на границе текущего хода."
        )
        format_output(data, as_json, text)
        return EXIT_OK
    except CollaborationError as e:
        format_output(
            {"ok": False, "error_code": e.code, "message": e.message},
            as_json,
            f"Ошибка [{e.code}]: {e.message}"
        )
        return EXIT_BLOCK if e.code == "checkout_busy" else EXIT_ERROR
    except Exception as e:
        sys.stderr.write(f"Непредвиденная ошибка: {e}\n")
        return EXIT_ERROR


def cmd_quota(args) -> int:
    as_json = getattr(args, "json", False)
    try:
        root = find_git_root(override=args.project_root)
        agy_path = discover_executable(
            "agy",
            arg_override=args.agy_exe,
            env_override_name="INVT_AI_AGY_EXE",
            cwd=root
        )

        adapter = QuotaAdapter(agy_exe=agy_path, threshold=args.threshold)
        snapshot = adapter.check_quota(model_override=args.model)

        data = snapshot.to_dict()
        data["ok"] = snapshot.availability != "unknown"

        text_lines = [
            f"Статус квоты: {snapshot.availability.upper()}",
            f"Модель: {snapshot.model_id} (группа: {snapshot.group_name})",
            f"Источник: {snapshot.source}, Проверено: {snapshot.checked_at}",
        ]
        if snapshot.uncertainty_reason:
            text_lines.append(f"Причина неопределённости: {snapshot.uncertainty_reason}")
        if snapshot.buckets:
            text_lines.append("Окна лимитов:")
            for b in snapshot.buckets:
                rf = b['remaining_fraction']
                rf_str = f"{rf * 100:.1f}%" if rf is not None else "N/A"
                text_lines.append(f"  * {b['name']} ({b['window']}): остаток {rf_str}, сброс: {b.get('reset_time')}")

        format_output(data, as_json, "\n".join(text_lines))
        return EXIT_OK if snapshot.availability != "unknown" else EXIT_BLOCK
    except CollaborationError as e:
        format_output(
            {"ok": False, "error_code": e.code, "message": e.message},
            as_json,
            f"Ошибка квоты [{e.code}]: {e.message}"
        )
        return EXIT_INVALID_INPUT
    except Exception as e:
        sys.stderr.write(f"Непредвиденная ошибка при запросе квоты: {e}\n")
        return EXIT_ERROR


def cmd_stub(name: str, phase_task: str) -> int:
    msg = f"Команда '{name}' запланирована на последующий этап ({phase_task}) и ожидает ревью Codex."
    sys.stderr.write(msg + "\n")
    return EXIT_INVALID_INPUT


def main():
    parser = argparse.ArgumentParser(
        description="tinker Collaboration Bridge (Feature 007)"
    )
    parser.add_argument("--project-root", help="Явное переопределение пути к корню проекта")
    parser.add_argument("--python-exe", help="Явное переопределение пути к python.exe")
    parser.add_argument("--agy-exe", help="Явное переопределение пути к agy.exe")
    parser.add_argument("--codex-exe", help="Явное переопределение пути к codex.exe")
    parser.add_argument("--json", action="store_true", help="Вывод в формате JSON")

    subparsers = parser.add_subparsers(dest="command", help="Команды моста")

    # doctor
    p_doctor = subparsers.add_parser("doctor", help="Диагностика окружения и зависимостей")
    p_doctor.add_argument("--check-android", action="store_true", help="Проверить Android toolchain (JDK, SDK, Wrapper)")

    # status
    p_status = subparsers.add_parser("status", help="Чтение текущего состояния работы")
    p_status.add_argument("--work-id", required=True, help="UUID работы")

    # stop
    p_stop = subparsers.add_parser("stop", help="Запрос остановки на границе хода")
    p_stop.add_argument("--work-id", required=True, help="UUID работы")

    # quota
    p_quota = subparsers.add_parser("quota", help="Запрос read-only снимка лимитов Antigravity")
    p_quota.add_argument("--model", help="Переопределение идентификатора модели")
    p_quota.add_argument("--threshold", type=float, default=0.20, help="Порог малого остатка (по умолчанию 0.20)")

    # Stubs for subsequent tasks per contract
    subparsers.add_parser("bootstrap", help="Настройка окружения моста (T031/Phase 6)")
    subparsers.add_parser("serve", help="Запуск MCP stdio сервера (T016/Phase 3)")
    subparsers.add_parser("handoff", help="Передача управления пользователю (T038/Phase 7)")
    subparsers.add_parser("export", help="Экспорт контекста работы (T037/Phase 7)")
    subparsers.add_parser("reconcile", help="Сверка состояния Git и задач (T039/Phase 7)")
    subparsers.add_parser("recover", help="Восстановление после сбоя (T040/Phase 7)")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(EXIT_INVALID_INPUT)

    if args.command == "doctor":
        sys.exit(cmd_doctor(args))
    elif args.command == "status":
        sys.exit(cmd_status(args))
    elif args.command == "stop":
        sys.exit(cmd_stop(args))
    elif args.command == "quota":
        sys.exit(cmd_quota(args))
    elif args.command == "bootstrap":
        sys.exit(cmd_stub("bootstrap", "Phase 6 / T031"))
    elif args.command == "serve":
        sys.exit(cmd_stub("serve", "Phase 3 / T016"))
    elif args.command == "handoff":
        sys.exit(cmd_stub("handoff", "Phase 7 / T038"))
    elif args.command == "export":
        sys.exit(cmd_stub("export", "Phase 7 / T037"))
    elif args.command == "reconcile":
        sys.exit(cmd_stub("reconcile", "Phase 7 / T039"))
    elif args.command == "recover":
        sys.exit(cmd_stub("recover", "Phase 7 / T040"))
    else:
        sys.stderr.write(f"Неизвестная команда: {args.command}\n")
        sys.exit(EXIT_INVALID_INPUT)


if __name__ == "__main__":
    main()
