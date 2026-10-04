#!/usr/bin/env python3
"""
Configurable Fake CLI simulating Antigravity `agy.exe` for tests (Tasks T003, T011, T048).
Emits strictly nested official envelopes:
- event 'init': {'event': 'init', 'conversation_id': '...'}
- event 'step_update': {'event': 'step_update', 'step_update': {'step_index': 1, 'step_type': 'tool', 'state': 'ACTIVE', ...}}
- event 'result': {'event': 'result', 'result': {'status': 'SUCCESS' | 'ERROR', 'structured_output': {...}}}
Does not silently fabricate defaults on corrupt/missing fixtures.
"""

import sys
import os
import re
import json
import time
import argparse
import subprocess
from datetime import datetime, timezone, timedelta
from pathlib import Path

DEFAULT_WORK_ID = "00000000-0000-0000-0000-000000000001"
DEFAULT_TURN_ID = "00000000-0000-0000-0000-000000000002"
DEFAULT_CONVERSATION_ID = "11111111-1111-1111-1111-111111111111"


def get_fixtures_dir() -> Path:
    return Path(__file__).resolve().parent / "fixtures"


def handle_usage_command(mode: str):
    state_file = os.environ.get("FAKE_AGY_STATE_FILE")
    if state_file and Path(state_file).exists():
        try:
            with open(state_file, "r", encoding="utf-8") as sf:
                sdata = json.load(sf)
                if isinstance(sdata, dict) and "mode" in sdata:
                    mode = sdata["mode"]
        except Exception:
            pass

    fixtures_dir = get_fixtures_dir()
    usage_path = fixtures_dir / "usage_sample.json"

    if mode == "usage_error":
        sys.stderr.write("Error: Failed to fetch usage from provider\n")
        sys.exit(1)
    elif mode == "usage_timeout":
        time.sleep(10)
        sys.exit(0)
    elif mode == "usage_canary_stderr":
        sys.stderr.write("Error: CANARY_SECRET_USAGE_998877 failed\n")
        sys.exit(1)

    if not usage_path.exists():
        sys.stderr.write(f"Error: Fixture file not found: {usage_path}\n")
        sys.exit(1)

    try:
        with open(usage_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        sys.stderr.write(f"Error: Corrupted usage fixture: {e}\n")
        sys.exit(1)

    # Dynamic future reset timestamps to prevent test rot
    future_reset = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
    for grp in data.get("command", {}).get("data", {}).get("groups", []):
        for b in grp.get("buckets", []):
            b["reset_time"] = future_reset

    if mode == "usage_low":
        for grp in data.get("command", {}).get("data", {}).get("groups", []):
            if grp.get("name") == "Gemini Models":
                for b in grp.get("buckets", []):
                    b["remaining_fraction"] = 0.15
    elif mode == "usage_exhausted":
        for grp in data.get("command", {}).get("data", {}).get("groups", []):
            if grp.get("name") == "Gemini Models":
                for b in grp.get("buckets", []):
                    b["remaining_fraction"] = 0.0
    elif mode == "usage_wrong_group":
        data["command"]["data"]["groups"] = [
            {
                "name": "Unsupported Custom Group",
                "buckets": [
                    {
                        "id": "custom_daily",
                        "name": "Daily limit",
                        "window": "24h",
                        "remaining_fraction": 1.0,
                        "reset_time": future_reset
                    }
                ]
            }
        ]
    elif mode == "usage_expired_reset":
        past_reset = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        for grp in data.get("command", {}).get("data", {}).get("groups", []):
            for b in grp.get("buckets", []):
                b["reset_time"] = past_reset
    elif mode == "usage_naive_reset":
        # Missing timezone info (naive)
        for grp in data.get("command", {}).get("data", {}).get("groups", []):
            for b in grp.get("buckets", []):
                b["reset_time"] = "2029-01-01T12:00:00"
    elif mode == "usage_missing_window":
        for grp in data.get("command", {}).get("data", {}).get("groups", []):
            for b in grp.get("buckets", []):
                b["window"] = None
    elif mode == "usage_missing_status":
        data.pop("status", None)
    elif mode == "usage_boolean_fraction":
        for grp in data.get("command", {}).get("data", {}).get("groups", []):
            for b in grp.get("buckets", []):
                b["remaining_fraction"] = True
    elif mode == "usage_nan_fraction":
        for grp in data.get("command", {}).get("data", {}).get("groups", []):
            for b in grp.get("buckets", []):
                b["remaining_fraction"] = "NaN"
    elif mode == "usage_missing_fraction":
        for grp in data.get("command", {}).get("data", {}).get("groups", []):
            for b in grp.get("buckets", []):
                b["remaining_fraction"] = None
    elif mode == "usage_duplicate_group":
        orig = data["command"]["data"]["groups"]
        data["command"]["data"]["groups"] = orig + [orig[0]]
    elif mode == "usage_wrong_command_name":
        data["command"]["name"] = "wrong_usage"

    sys.stdout.write(json.dumps(data) + "\n")
    sys.stdout.flush()
    sys.exit(0)


def handle_model_command(mode: str):
    fixtures_dir = get_fixtures_dir()
    model_path = fixtures_dir / "model_sample.json"

    if mode == "model_error":
        sys.stderr.write("Error: Failed to fetch model info\n")
        sys.exit(1)
    elif mode == "model_canary_stderr":
        sys.stderr.write("Error: CANARY_SECRET_MODEL_112233 failed\n")
        sys.exit(1)

    if not model_path.exists():
        sys.stderr.write(f"Error: Fixture file not found: {model_path}\n")
        sys.exit(1)

    try:
        with open(model_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        sys.stderr.write(f"Error: Corrupted model fixture: {e}\n")
        sys.exit(1)

    if mode == "model_claude":
        data["command"]["data"]["id"] = "claude-3-7-sonnet"
        data["command"]["data"]["label"] = "Claude 3.7 Sonnet"
    elif mode == "model_unknown":
        data["command"]["data"]["id"] = "unrecognized-exotic-model"
        data["command"]["data"]["label"] = "Exotic Model"
    elif mode == "model_xgemini_substring":
        data["command"]["data"]["id"] = "xgemini-fake-model"
        data["command"]["data"]["label"] = "Fake Gemini Model"
    elif mode == "model_unsupported_status":
        data["status"] = "UNKNOWN"
    elif mode == "model_malformed_shape":
        data["command"] = "not-a-dict"
    elif mode == "model_wrong_command_name":
        data["command"]["name"] = "wrong_model"
    elif mode == "model_slash_command_name":
        data["command"]["name"] = "/model"
    elif mode == "model_missing_command_name":
        data["command"].pop("name", None)

    sys.stdout.write(json.dumps(data) + "\n")
    sys.stdout.flush()
    sys.exit(0)


def handle_stream_execution(mode: str, work_id: str, turn_id: str, conversation_id: str):
    conv_id = conversation_id or DEFAULT_CONVERSATION_ID
    w_id = work_id or DEFAULT_WORK_ID
    t_id = turn_id or DEFAULT_TURN_ID

    # Blocked stdin regression mode: sleep before reading stdin to test prompt timeout
    if mode == "blocked_stdin":
        time.sleep(0.2)

    # Read stdin user NDJSON until EOF
    stdin_content = ""
    try:
        for line in sys.stdin:
            stdin_content += line
    except Exception:
        pass

    counter_file = os.environ.get("FAKE_AGY_COUNTER_FILE")
    if counter_file:
        try:
            cf_path = Path(counter_file)
            count = 0
            if cf_path.exists():
                count = int(cf_path.read_text(encoding="utf-8").strip() or "0")
            cf_path.write_text(str(count + 1), encoding="utf-8")
        except Exception:
            pass

    if w_id == DEFAULT_WORK_ID and stdin_content:
        m_w = re.search(r"Work(?:\s+Assignment)?:?\s*([0-9a-fA-F-]{36})", stdin_content, re.IGNORECASE)
        if m_w:
            w_id = m_w.group(1)
        m_t = re.search(r"Turn:?\s*([0-9a-fA-F-]{36})", stdin_content, re.IGNORECASE)
        if m_t:
            t_id = m_t.group(1)

    sleep_secs = float(os.environ.get("FAKE_AGY_SLEEP", "0"))
    if sleep_secs > 0:
        time.sleep(sleep_secs)

    if mode == "long_turn":
        time.sleep(15)
        sys.exit(0)

    if mode == "canary_stderr":
        sys.stderr.write("FATAL: permission denied with CANARY_SECRET_KEY_98765\n")
        sys.stderr.flush()
        sys.exit(1)

    if mode == "oversized_line":
        # Emit a line larger than 64KB
        big_comment = "A" * (70 * 1024)
        sys.stdout.write(f'{{"event": "init", "conversation_id": "{conv_id}", "comment": "{big_comment}"}}\n')
        sys.stdout.flush()
        sys.exit(0)

    if mode == "malformed_json":
        sys.stdout.write(f'{{"event": "init", "conversation_id": "{conv_id}"\n')  # truncated
        sys.stdout.flush()
        sys.exit(0)

    if mode == "unknown_event":
        sys.stdout.write(json.dumps({"event": "init", "conversation_id": conv_id}) + "\n")
        sys.stdout.write(json.dumps({"event": "unknown_future_event", "payload": {}}) + "\n")
        sys.stdout.flush()
        sys.exit(0)

    if mode == "changed_conversation_uuid":
        # Emits a DIFFERENT conversation UUID than requested
        sys.stdout.write(json.dumps({"event": "init", "conversation_id": "99999999-9999-9999-9999-999999999999"}) + "\n")
        sys.stdout.flush()
        sys.exit(0)

    # 1. Official Init event envelope
    sys.stdout.write(json.dumps({"event": "init", "conversation_id": conv_id}) + "\n")
    sys.stdout.flush()

    if mode == "duplicate_init":
        sys.stdout.write(json.dumps({"event": "init", "conversation_id": conv_id}) + "\n")
        sys.stdout.flush()
        sys.exit(0)

    if mode == "invalid_step_type":
        sys.stdout.write(json.dumps({
            "event": "step_update",
            "step_update": {
                "step_index": 1,
                "step_type": "unrecognized_step_type",
                "state": "ACTIVE"
            }
        }) + "\n")
        sys.stdout.flush()
        sys.exit(0)

    if mode == "invalid_step_index_bool":
        sys.stdout.write(json.dumps({
            "event": "step_update",
            "step_update": {
                "step_index": True,
                "step_type": "tool",
                "state": "ACTIVE"
            }
        }) + "\n")
        sys.stdout.flush()
        sys.exit(0)

    if mode == "step_changes_conversation_uuid":
        sys.stdout.write(json.dumps({
            "event": "step_update",
            "conversation_id": "99999999-9999-9999-9999-999999999999",
            "step_update": {
                "step_index": 1,
                "step_type": "tool",
                "state": "ACTIVE"
            }
        }) + "\n")
        sys.stdout.flush()
        sys.exit(0)

    if mode == "canary_tool_error":
        sys.stdout.write(json.dumps({
            "event": "step_update",
            "step_update": {
                "step_index": 1,
                "step_type": "tool",
                "state": "DONE",
                "tool_info": {
                    "name": "run_command",
                    "error": "Permission denied for CANARY_TOOL_TOKEN_54321"
                }
            }
        }) + "\n")
        sys.stdout.write(json.dumps({
            "event": "result",
            "result": {
                "status": "SUCCESS",
                "structured_output": {}
            }
        }) + "\n")
        sys.stdout.flush()
        sys.exit(0)

    if mode == "error_exit0":
        # Official nested result error envelope with exit code 0
        sys.stdout.write(json.dumps({
            "event": "result",
            "result": {
                "status": "ERROR",
                "error": "Provider 503 Service Unavailable"
            }
        }) + "\n")
        sys.stdout.flush()
        sys.exit(0)

    if mode == "denied_exit0":
        # Official nested step_update tool error envelope
        sys.stdout.write(json.dumps({
            "event": "step_update",
            "step_update": {
                "step_index": 1,
                "step_type": "tool",
                "state": "DONE",
                "tool_info": {
                    "name": "run_command",
                    "error": "Tool execution denied: RunCommand permission denied"
                }
            }
        }) + "\n")
        sys.stdout.write(json.dumps({
            "event": "result",
            "result": {
                "status": "SUCCESS",
                "structured_output": {}
            }
        }) + "\n")
        sys.stdout.flush()
        sys.exit(0)

    if mode == "stderr_denied_exit0":
        sys.stderr.write("FATAL: permission denied by security sandbox\n")
        sys.stderr.flush()
        sys.stdout.write(json.dumps({
            "event": "result",
            "result": {
                "status": "SUCCESS",
                "conversation_id": conv_id,
                "structured_output": {
                    "schema_version": 1,
                    "work_id": w_id,
                    "turn_id": t_id,
                    "kind": "implementation_result",
                    "summary": "Should be rejected due to stderr",
                    "claimed_files": [],
                    "checks": [],
                    "remaining_actions": [],
                    "question": None,
                    "block_reason": None
                }
            }
        }) + "\n")
        sys.stdout.flush()
        sys.exit(0)
    if mode == "result_denied_actions":
        sys.stdout.write(json.dumps({
            "event": "result",
            "result": {
                "status": "SUCCESS",
                "denied_actions": ["run_command"],
                "structured_output": {}
            }
        }) + "\n")
        sys.stdout.flush()
        sys.exit(0)

    if mode == "nested_conv_mismatch":
        sys.stdout.write(json.dumps({
            "event": "result",
            "conversation_id": conv_id,
            "result": {
                "status": "SUCCESS",
                "conversation_id": "00000000-0000-0000-0000-000000000000",
                "structured_output": {}
            }
        }) + "\n")
        sys.stdout.flush()
        sys.exit(0)

    if mode == "pending_tools":
        # Tool step remains ACTIVE when stream closes
        sys.stdout.write(json.dumps({
            "event": "step_update",
            "step_update": {
                "step_index": 1,
                "step_type": "tool",
                "state": "ACTIVE",
                "tool_info": {
                    "name": "run_command"
                }
            }
        }) + "\n")
        sys.stdout.write(json.dumps({
            "event": "step_update",
            "step_update": {
                "step_index": 2,
                "step_type": "agent_response",
                "state": "DONE"
            }
        }) + "\n")
        sys.stdout.write(json.dumps({
            "event": "result",
            "result": {
                "status": "SUCCESS",
                "structured_output": {
                    "schema_version": 1,
                    "work_id": w_id,
                    "turn_id": t_id,
                    "kind": "implementation_result",
                    "summary": "Completed with pending tool",
                    "claimed_files": [],
                    "checks": [],
                    "remaining_actions": [],
                    "question": None,
                    "block_reason": None
                }
            }
        }) + "\n")
        sys.stdout.flush()
        sys.exit(0)

    if mode == "agent_active_no_tool":
        sys.stdout.write(json.dumps({
            "event": "step_update",
            "step_update": {
                "step_index": 1,
                "step_type": "agent_response",
                "state": "ACTIVE"
            }
        }) + "\n")
        sys.stdout.write(json.dumps({
            "event": "result",
            "result": {
                "status": "SUCCESS",
                "structured_output": {
                    "schema_version": 1,
                    "work_id": w_id,
                    "turn_id": t_id,
                    "kind": "implementation_result",
                    "summary": "Agent active is not pending tool",
                    "claimed_files": [],
                    "checks": [],
                    "remaining_actions": [],
                    "question": None,
                    "block_reason": None
                }
            }
        }) + "\n")
        sys.stdout.flush()
        sys.exit(0)

    if mode == "missing_result":
        sys.stdout.write(json.dumps({
            "event": "step_update",
            "step_update": {
                "step_index": 1,
                "step_type": "tool",
                "state": "DONE"
            }
        }) + "\n")
        sys.stdout.flush()
        sys.exit(0)

    if mode == "late_error":
        sys.stdout.write(json.dumps({
            "event": "step_update",
            "step_update": {
                "step_index": 1,
                "step_type": "tool",
                "state": "DONE"
            }
        }) + "\n")
        sys.stdout.write(json.dumps({
            "event": "step_update",
            "step_update": {
                "step_index": 2,
                "step_type": "tool",
                "state": "DONE",
                "tool_info": {
                    "name": "write_file",
                    "error": "Late fatal disk error"
                }
            }
        }) + "\n")
        sys.stdout.flush()
        sys.exit(1)

    if mode == "success_empty":
        sys.stdout.write(json.dumps({
            "event": "result",
            "result": {
                "status": "SUCCESS",
                "structured_output": {}
            }
        }) + "\n")
        sys.stdout.flush()
        sys.exit(0)

    # Standard clean execution
    sys.stdout.write(json.dumps({
        "event": "step_update",
        "step_update": {
            "step_index": 1,
            "step_type": "tool",
            "state": "ACTIVE",
            "tool_info": {"name": "read_file"}
        }
    }) + "\n")
    sys.stdout.write(json.dumps({
        "event": "step_update",
        "step_update": {
            "step_index": 1,
            "step_type": "tool",
            "state": "DONE"
        }
    }) + "\n")
    sys.stdout.flush()

    # Load structured output fixture
    fixtures_dir = get_fixtures_dir()
    struct_path = fixtures_dir / "structured_output_sample.json"
    if not struct_path.exists():
        sys.stderr.write(f"Error: Fixture file not found: {struct_path}\n")
        sys.exit(1)

    try:
        with open(struct_path, "r", encoding="utf-8") as f:
            struct_out = json.load(f)
    except Exception as e:
        sys.stderr.write(f"Error: Corrupted structured output fixture: {e}\n")
        sys.exit(1)

    if mode == "wrong_ids":
        struct_out["work_id"] = "99999999-9999-9999-9999-999999999999"
        struct_out["turn_id"] = "88888888-8888-8888-8888-888888888888"
    elif mode == "wrong_ids_canary":
        struct_out["work_id"] = "CANARY_SECRET_WORK_ID_778899"
        struct_out["turn_id"] = "CANARY_SECRET_TURN_ID_778899"
    else:
        struct_out["work_id"] = w_id
        struct_out["turn_id"] = t_id

    # Emit terminal result
    sys.stdout.write(json.dumps({
        "event": "result",
        "result": {
            "status": "SUCCESS",
            "structured_output": struct_out
        }
    }) + "\n")
    sys.stdout.flush()

    if mode == "duplicate_result":
        sys.stdout.write(json.dumps({
            "event": "result",
            "result": {
                "status": "SUCCESS",
                "structured_output": struct_out
            }
        }) + "\n")
        sys.stdout.flush()

    if mode == "delayed_fatal_error":
        time.sleep(0.1)
        sys.stdout.write(json.dumps({
            "event": "step_update",
            "step_update": {
                "step_index": 2,
                "step_type": "tool",
                "state": "DONE",
                "tool_info": {
                    "name": "run_command",
                    "error": "Late fatal process crash"
                }
            }
        }) + "\n")
        sys.stdout.flush()

    if mode == "active_reader_leak":
        # Deterministic exit with active reader: spawn background child holding stdout open, then exit 0 immediately
        try:
            creationflags = 0
            if sys.platform == "win32":
                import msvcrt
                import ctypes
                handle = msvcrt.get_osfhandle(sys.stdout.fileno())
                ctypes.windll.kernel32.SetHandleInformation(handle, 1, 1)  # HANDLE_FLAG_INHERIT = 1
                creationflags = subprocess.DETACHED_PROCESS

            subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(3)"],
                stdout=sys.stdout,
                stdin=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=False,
                creationflags=creationflags
            )
        except Exception:
            pass
        os._exit(0)

    sys.exit(0)


def main():
    parser = argparse.ArgumentParser(description="Fake agy CLI for collaboration tests")
    parser.add_argument("-p", "--print", dest="print_cmd", help="Print command e.g. /usage, /model")
    parser.add_argument("--output-format", dest="output_format", default="text")
    parser.add_argument("--input-format", dest="input_format", default="text")
    parser.add_argument("--json-schema", dest="json_schema", help="Path to json schema")
    parser.add_argument("--mode", dest="mode", help="Running mode")
    parser.add_argument("--conversation", dest="conversation", help="Conversation UUID")
    parser.add_argument("--fake-mode", dest="fake_mode", default=None, help="Mode override for tests")
    parser.add_argument("--work-id", dest="work_id", default=None, help="Work UUID for test responses")
    parser.add_argument("--turn-id", dest="turn_id", default=None, help="Turn UUID for test responses")
    parser.add_argument("--model", dest="model", default=None, help="Target model identifier")

    args, unknown = parser.parse_known_args()

    mode = args.fake_mode or os.environ.get("FAKE_AGY_MODE", "success_result")
    work_id = args.work_id or os.environ.get("FAKE_AGY_WORK_ID", DEFAULT_WORK_ID)
    turn_id = args.turn_id or os.environ.get("FAKE_AGY_TURN_ID", DEFAULT_TURN_ID)

    if args.print_cmd == "/usage":
        handle_usage_command(mode)
    elif args.print_cmd == "/model":
        handle_model_command(mode)
    else:
        handle_stream_execution(mode, work_id, turn_id, args.conversation)


if __name__ == "__main__":
    main()
