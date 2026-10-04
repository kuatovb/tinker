"""
Antigravity single-turn detached worker, JSON schema generator, and NDJSON decoder
for Codex and Antigravity collaboration (Feature 007, Tasks T009, T010, T011).
Uses strict official nested envelopes:
- evt['result']['status']
- evt['step_update']['state'], evt['step_update']['step_type'], and evt['step_update']['tool_info']
- Incremental chunk-based bounded decoding with pipe cleanup to avoid ResourceWarnings.
- Early monotonic deadline starting before Popen and covering prompt delivery.
- Controlled non-blocking stdin writer thread.
- Stream EOF & thread join verification on readers (never SUCCESS on active readers).
- Strict wire validation of step_type ('user_input', 'agent_response', 'tool', 'checkpoint').
- Safe error classification preventing canary token or secret leaks.
- Structured process identity attributes and hooks for coordinator.
"""

import os
import sys
import json
import time
import uuid
import codecs
import threading
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Any, Tuple, Callable

from .state import (
    CollaborationError,
    ExecutorResult,
    CheckEvidence,
    Turn,
    SCHEMA_VERSION,
    DEFAULT_TIMEOUT_SECONDS,
    is_valid_uuid,
    validate_uuid,
    validate_safe_relative_path,
    sanitize_safe_output,
)

MAX_LINE_LENGTH_BYTES = 64 * 1024  # 64 KB per NDJSON line
MAX_TOTAL_BUFFER_BYTES = 5 * 1024 * 1024  # 5 MB total stream buffer
DEFAULT_STREAM_DRAIN_TIMEOUT = 3.0  # seconds to wait for EOF on finish

VALID_STEP_TYPES = ("user_input", "agent_response", "tool", "checkpoint")


def build_executor_json_schema() -> Dict[str, Any]:
    """Returns the strict root object JSON Schema for executor structured output (T009)."""
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "AntigravityExecutorResult",
        "type": "object",
        "additionalProperties": False,
        "required": [
            "schema_version",
            "work_id",
            "turn_id",
            "kind",
            "summary",
            "claimed_files",
            "checks",
            "remaining_actions",
            "question",
            "block_reason"
        ],
        "properties": {
            "schema_version": {
                "type": "integer",
                "const": SCHEMA_VERSION
            },
            "work_id": {
                "type": "string",
                "format": "uuid"
            },
            "turn_id": {
                "type": "string",
                "format": "uuid"
            },
            "kind": {
                "type": "string",
                "enum": ["question", "implementation_result", "blocked"]
            },
            "summary": {
                "type": "string",
                "minLength": 1
            },
            "claimed_files": {
                "type": "array",
                "items": {
                    "type": "string"
                }
            },
            "checks": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["criterion_id", "status", "command_or_description"],
                    "properties": {
                        "criterion_id": {"type": "string"},
                        "task_id": {"type": ["string", "null"]},
                        "status": {
                            "type": "string",
                            "enum": ["passed", "failed", "not_run"]
                        },
                        "command_or_description": {"type": "string"},
                        "exit_code": {"type": ["integer", "null"]},
                        "evidence": {"type": ["string", "null"]},
                        "limitation": {"type": ["string", "null"]}
                    }
                }
            },
            "remaining_actions": {
                "type": "array",
                "items": {
                    "type": "string"
                }
            },
            "question": {
                "type": ["object", "null"],
                "additionalProperties": False,
                "properties": {
                    "question_id": {"type": "string"},
                    "text": {"type": "string"},
                    "decision_kind": {
                        "type": "string",
                        "enum": ["technical", "scope", "permission"]
                    }
                }
            },
            "block_reason": {
                "type": ["object", "null"],
                "additionalProperties": False,
                "properties": {
                    "code": {"type": "string"},
                    "needed_action": {"type": "string"}
                }
            }
        }
    }


def save_executor_json_schema(target_path: Path) -> Path:
    target_path = Path(target_path).resolve()
    target_path.parent.mkdir(parents=True, exist_ok=True)
    schema = build_executor_json_schema()
    temp_path = target_path.parent / f".tmp_{uuid.uuid4().hex}_{target_path.name}"
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(schema, f, indent=2, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp_path, target_path)
    return target_path


def decode_and_validate_structured_output(
    raw_dict: Any,
    expected_work_id: str,
    expected_turn_id: str
) -> ExecutorResult:
    if not isinstance(raw_dict, dict) or not raw_dict:
        raise CollaborationError(
            code="empty_result",
            message="Structured output is empty or not an object; free text cannot substitute structured output"
        )

    w_id = raw_dict.get("work_id")
    t_id = raw_dict.get("turn_id")
    if str(w_id).lower() != expected_work_id.lower() or str(t_id).lower() != expected_turn_id.lower():
        raise CollaborationError(
            code="protocol_error",
            message="ID mismatch in structured output for work_id or turn_id",
            details={"field": "work_id_or_turn_id"}
        )

    return ExecutorResult.from_dict(raw_dict)


def classify_error_safely(
    raw_diagnostic: Optional[str],
    default_code: str = "process_error",
    default_msg: str = "Execution error occurred"
) -> Tuple[str, str]:
    """
    Classifies a raw stderr/tool error into a safe category code and non-leaking message.
    NEVER echoes raw text to avoid leaking canary tokens, passwords, or secrets.
    """
    if not raw_diagnostic:
        return default_code, default_msg

    diag_lower = str(raw_diagnostic).lower()
    if any(k in diag_lower for k in ("permission denied", "access denied", "unauthorized", "forbidden")):
        return "permission_denied", "Execution permission denied by security policy or tool sandbox"
    if any(k in diag_lower for k in ("sandbox violation", "sandbox", "restricted")):
        return "sandbox_violation", "Sandbox security violation detected during execution"
    if any(k in diag_lower for k in ("503", "service unavailable", "rate limit", "quota exceeded")):
        return "provider_error", "Provider service unavailable or rate limit exceeded"
    if any(k in diag_lower for k in ("fatal", "panic", "disk full", "io error", "out of memory")):
        return "fatal_step_error", "Fatal step execution failure encountered"

    return default_code, default_msg


class TurnOutcome:
    def __init__(
        self,
        status: str,  # "SUCCESS", "ERROR", "execution_unknown"
        result: Optional[ExecutorResult] = None,
        conversation_id: Optional[str] = None,
        exit_code: Optional[int] = None,
        error_code: Optional[str] = None,
        error_message: Optional[str] = None,
        raw_events: Optional[List[Dict[str, Any]]] = None,
        process_identity: Optional[Dict[str, Any]] = None,
    ):
        self.status = status
        self.result = result
        self.conversation_id = conversation_id
        self.exit_code = exit_code
        self.error_code = error_code
        self.error_message = sanitize_safe_output(error_message) if error_message else None
        self.raw_events = raw_events or []
        self.process_identity = process_identity or {}

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "conversation_id": self.conversation_id,
            "exit_code": self.exit_code,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "result": self.result.to_dict() if self.result else None,
            "process_identity": self.process_identity,
        }


class AntigravityTurnWorker:
    """
    Executes a single detached turn using native EXE and official nested envelopes.
    Includes:
    - Early monotonic deadline starting BEFORE Popen and covering prompt delivery
    - Dedicated controlled thread for stdin writing
    - Chunk-based bounded stream reading with incremental UTF-8 & NDJSON decoding
    - Stream EOF & thread join verification on readers (never SUCCESS on active readers)
    - Strict wire validation of step_type ('user_input', 'agent_response', 'tool', 'checkpoint')
    - Tool tracking strictly for step_type == 'tool' by step_index
    - Safe error classification preventing canary token or secret leaks
    - Structured process identity attributes and hooks for coordinator
    """
    def __init__(
        self,
        agy_exe: Path,
        root_dir: Path,
        work_id: str,
        turn_id: str,
        prompt: str,
        conversation_id: Optional[str] = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_buffer_bytes: int = MAX_TOTAL_BUFFER_BYTES,
        cli_args_prefix: Optional[List[str]] = None,
        on_event_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
        stream_drain_timeout: float = DEFAULT_STREAM_DRAIN_TIMEOUT,
        model: Optional[str] = None,
    ):
        self.agy_exe = Path(agy_exe).resolve()
        self.root_dir = Path(root_dir).resolve()
        self.work_id = validate_uuid(work_id, "work_id")
        self.turn_id = validate_uuid(turn_id, "turn_id")
        self.prompt = prompt
        self.conversation_id = validate_uuid(conversation_id, "conversation_id") if conversation_id else None
        self.timeout_seconds = float(timeout_seconds)
        self.max_buffer_bytes = max_buffer_bytes
        self.cli_args_prefix = list(cli_args_prefix or [])
        self.on_event_callback = on_event_callback
        self.stream_drain_timeout = float(stream_drain_timeout)
        self.model = model

        # Process & coordinator hooks
        self.worker_token: str = uuid.uuid4().hex
        self.proc: Optional[subprocess.Popen] = None
        self.pid: Optional[int] = None
        self.start_time: Optional[float] = None  # Monotonic
        self.start_timestamp: Optional[str] = None  # ISO 8601 UTC
        self.deadline: Optional[float] = None  # Monotonic

        # Thread handles
        self.t_in: Optional[threading.Thread] = None
        self.t_out: Optional[threading.Thread] = None
        self.t_err: Optional[threading.Thread] = None

        # Incremental stream state
        self.parsed_events: List[Dict[str, Any]] = []
        self.captured_conv_id: Optional[str] = self.conversation_id
        self.terminal_result_event: Optional[Dict[str, Any]] = None
        self.pending_tool_steps: Dict[int, str] = {}  # step_index -> tool_name
        self.seen_init: bool = False
        self.seen_result: bool = False

        # Reader diagnostics & bounds
        self.buffer_overflow: bool = False
        self.reader_error: Optional[Tuple[str, str]] = None
        self.stream_protocol_error: Optional[Tuple[str, str]] = None
        self.stdin_error: Optional[str] = None

        # Safe classified error flags
        self.has_denied_tool: bool = False
        self.denied_code: Optional[str] = None
        self.has_step_error: bool = False
        self.step_error_code: Optional[str] = None
        self.has_stderr_denial: bool = False
        self.has_stderr_error: bool = False
        self.stderr_classified_code: Optional[str] = None

    def get_process_identity(self) -> Dict[str, Any]:
        """Returns structured process identity for coordinator inspection."""
        return {
            "worker_token": self.worker_token,
            "pid": self.pid,
            "start_timestamp": self.start_timestamp,
            "deadline_monotonic": self.deadline,
            "is_alive": self.proc.poll() is None if self.proc else False,
            "work_id": self.work_id,
            "turn_id": self.turn_id,
            "conversation_id": self.captured_conv_id or self.conversation_id,
        }

    def execute(self, schema_path: Path) -> TurnOutcome:
        start_mono = time.monotonic()
        self.start_time = start_mono
        self.deadline = start_mono + self.timeout_seconds
        self.start_timestamp = datetime.now(timezone.utc).isoformat()

        cmd = [str(self.agy_exe)]
        if self.cli_args_prefix:
            cmd.extend(self.cli_args_prefix)
        cmd.extend([
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--json-schema", str(schema_path),
            "--mode", "accept-edits",
        ])
        if self.conversation_id:
            cmd.extend(["--conversation", self.conversation_id])
        if self.model:
            cmd.extend(["--model", self.model])

        self.parsed_events = []
        self.captured_conv_id = self.conversation_id
        self.terminal_result_event = None
        self.pending_tool_steps = {}
        self.seen_init = False
        self.seen_result = False
        self.buffer_overflow = False
        self.reader_error = None
        self.stream_protocol_error = None
        self.stdin_error = None
        self.has_denied_tool = False
        self.denied_code = None
        self.has_step_error = False
        self.step_error_code = None
        self.has_stderr_denial = False
        self.has_stderr_error = False
        self.stderr_classified_code = None

        try:
            self.proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=str(self.root_dir),
                shell=False,
                bufsize=0,
            )
            self.pid = self.proc.pid
        except Exception:
            return TurnOutcome(
                status="ERROR",
                error_code="process_error",
                error_message="Failed to spawn native worker process",
                process_identity=self.get_process_identity()
            )

        self.t_out = threading.Thread(target=self._read_stdout, daemon=True)
        self.t_err = threading.Thread(target=self._read_stderr, daemon=True)
        self.t_out.start()
        self.t_err.start()

        user_msg = (json.dumps({"event": "user", "message": {"content": self.prompt}}, ensure_ascii=False) + "\n").encode("utf-8")

        def write_stdin():
            try:
                if self.proc and self.proc.stdin:
                    self.proc.stdin.write(user_msg)
                    self.proc.stdin.flush()
                    self.proc.stdin.close()
            except Exception as e:
                self.stdin_error = str(e)

        self.t_in = threading.Thread(target=write_stdin, daemon=True)
        self.t_in.start()

        process_exited = False
        while time.monotonic() < self.deadline:
            ret = self.proc.poll()
            if ret is not None:
                process_exited = True
                break
            time.sleep(0.02)

        if not process_exited:
            return TurnOutcome(
                status="execution_unknown",
                error_code="timeout",
                error_message=f"Worker turn exceeded deadline ({self.timeout_seconds}s); process {self.pid} still running",
                process_identity=self.get_process_identity()
            )

        if self.t_in:
            self.t_in.join(timeout=0.5)

        return self._finish_execution()

    def observe_late_exit(self, timeout_seconds: float = 5.0) -> TurnOutcome:
        """Allows observing a previously timed-out process for late completion under existing reservation."""
        if not self.proc:
            return TurnOutcome(status="ERROR", error_code="process_error", error_message="No process to observe")

        obs_start = time.monotonic()
        obs_deadline = obs_start + timeout_seconds
        while time.monotonic() < obs_deadline:
            if self.proc.poll() is not None:
                if self.t_in:
                    self.t_in.join(timeout=0.5)
                return self._finish_execution()
            time.sleep(0.02)

        return TurnOutcome(
            status="execution_unknown",
            error_code="timeout",
            error_message="Process is still running upon late observation",
            process_identity=self.get_process_identity()
        )

    def _drain_pipe(self, pipe) -> None:
        """Drain remaining bytes until EOF without storing, preventing pipe deadlocks."""
        try:
            while pipe.read(4096):
                pass
        except Exception:
            if not self.reader_error:
                self.reader_error = ("process_error", "Exception encountered while draining worker pipe")

    def _read_stdout(self) -> None:
        stdout_bytes_total = 0
        decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
        char_buffer = ""
        try:
            assert self.proc and self.proc.stdout
            while True:
                chunk = self.proc.stdout.read(4096)
                if not chunk:
                    final_chars = decoder.decode(b"", final=True)
                    if final_chars:
                        char_buffer += final_chars
                    if char_buffer.strip():
                        self._handle_incremental_line(char_buffer.strip())
                    break

                stdout_bytes_total += len(chunk)
                if stdout_bytes_total > self.max_buffer_bytes:
                    self.buffer_overflow = True
                    if not self.reader_error:
                        self.reader_error = ("protocol_error", "Stream exceeded maximum total buffer limit")
                    self._drain_pipe(self.proc.stdout)
                    break

                try:
                    decoded_chars = decoder.decode(chunk, final=False)
                except UnicodeDecodeError:
                    if not self.reader_error:
                        self.reader_error = ("protocol_error", "Invalid UTF-8 sequence in worker stdout stream")
                    self._drain_pipe(self.proc.stdout)
                    break

                char_buffer += decoded_chars

                while "\n" in char_buffer:
                    line, char_buffer = char_buffer.split("\n", 1)
                    line_bytes_len = len(line.encode("utf-8", errors="replace"))
                    if line_bytes_len > MAX_LINE_LENGTH_BYTES:
                        self.buffer_overflow = True
                        if not self.reader_error:
                            self.reader_error = ("protocol_error", "Stream line exceeded maximum length buffer")
                        self._drain_pipe(self.proc.stdout)
                        return

                    line_str = line.strip()
                    if line_str:
                        self._handle_incremental_line(line_str)
                        if self.stream_protocol_error:
                            self._drain_pipe(self.proc.stdout)
                            return

                if len(char_buffer.encode("utf-8", errors="replace")) > MAX_LINE_LENGTH_BYTES:
                    self.buffer_overflow = True
                    if not self.reader_error:
                        self.reader_error = ("protocol_error", "Stream line exceeded maximum length buffer")
                    self._drain_pipe(self.proc.stdout)
                    break

        except Exception:
            if not self.reader_error:
                self.reader_error = ("process_error", "Exception reading worker stdout stream")
        finally:
            try:
                if self.proc and self.proc.stdout:
                    self.proc.stdout.close()
            except Exception:
                pass

    def _read_stderr(self) -> None:
        stderr_bytes_total = 0
        decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
        char_buffer = ""
        try:
            assert self.proc and self.proc.stderr
            while True:
                chunk = self.proc.stderr.read(4096)
                if not chunk:
                    final_chars = decoder.decode(b"", final=True)
                    if final_chars:
                        char_buffer += final_chars
                    if char_buffer.strip():
                        self._handle_incremental_stderr(char_buffer.strip())
                    break

                stderr_bytes_total += len(chunk)
                if stderr_bytes_total > self.max_buffer_bytes:
                    if not self.reader_error:
                        self.reader_error = ("protocol_error", "Stderr stream exceeded maximum total buffer limit")
                    self._drain_pipe(self.proc.stderr)
                    break

                try:
                    decoded_chars = decoder.decode(chunk, final=False)
                except UnicodeDecodeError:
                    if not self.reader_error:
                        self.reader_error = ("protocol_error", "Invalid UTF-8 sequence in worker stderr stream")
                    self._drain_pipe(self.proc.stderr)
                    break

                char_buffer += decoded_chars
                while "\n" in char_buffer:
                    line, char_buffer = char_buffer.split("\n", 1)
                    if len(line.encode("utf-8", errors="replace")) > MAX_LINE_LENGTH_BYTES:
                        if not self.reader_error:
                            self.reader_error = ("protocol_error", "Stderr stream line exceeded maximum length buffer")
                        self._drain_pipe(self.proc.stderr)
                        return
                    line_str = line.strip()
                    if line_str:
                        self._handle_incremental_stderr(line_str)

                if len(char_buffer.encode("utf-8", errors="replace")) > MAX_LINE_LENGTH_BYTES:
                    if not self.reader_error:
                        self.reader_error = ("protocol_error", "Stderr stream line exceeded maximum length buffer")
                    self._drain_pipe(self.proc.stderr)
                    break

        except Exception:
            if not self.reader_error:
                self.reader_error = ("process_error", "Exception reading worker stderr stream")
        finally:
            try:
                if self.proc and self.proc.stderr:
                    self.proc.stderr.close()
            except Exception:
                pass

    def _handle_incremental_line(self, line_str: str) -> None:
        """Parses and validates NDJSON event incrementally against official wire schema."""
        if self.stream_protocol_error:
            return

        try:
            evt = json.loads(line_str)
        except json.JSONDecodeError as e:
            self.stream_protocol_error = ("protocol_error", f"Malformed NDJSON line in worker stdout: {e.msg}")
            return

        if not isinstance(evt, dict) or "event" not in evt:
            self.stream_protocol_error = ("protocol_error", "Invalid event envelope: missing 'event' field or not an object")
            return

        evt_type = evt.get("event")
        if evt_type not in ("init", "step_update", "result"):
            self.stream_protocol_error = ("protocol_error", "Unknown stream event encountered in worker stdout")
            return

        if self.seen_result:
            self.stream_protocol_error = ("protocol_error", "Received unexpected stream event after terminal result event")
            return

        if evt_type == "init":
            if self.seen_init:
                self.stream_protocol_error = ("protocol_error", "Duplicate init event in stream")
                return
            self.seen_init = True

            c_id = evt.get("conversation_id")
            if not c_id and isinstance(evt.get("init"), dict):
                c_id = evt["init"].get("conversation_id")

            if not c_id:
                self.stream_protocol_error = ("protocol_error", "Missing conversation_id in init event")
                return

            if not is_valid_uuid(str(c_id)):
                self.stream_protocol_error = ("protocol_error", "Malformed conversation UUID in init event")
                return

            if self.conversation_id and str(c_id).lower() != self.conversation_id.lower():
                self.stream_protocol_error = (
                    "session_unavailable",
                    "Conversation UUID mismatch during continuation"
                )
                return

            self.captured_conv_id = str(c_id).lower()

        elif evt_type == "step_update":
            if not self.seen_init:
                self.stream_protocol_error = ("protocol_error", "step_update event received before init event")
                return

            step_data = evt.get("step_update")
            if not isinstance(step_data, dict):
                self.stream_protocol_error = ("protocol_error", "Malformed step_update envelope: not an object")
                return

            step_conv = step_data.get("conversation_id") or evt.get("conversation_id")
            if step_conv:
                if not is_valid_uuid(str(step_conv)) or str(step_conv).lower() != self.captured_conv_id:
                    self.stream_protocol_error = ("protocol_error", "step_update conversation_id does not match session UUID")
                    return

            step_idx = step_data.get("step_index")
            if not isinstance(step_idx, int) or isinstance(step_idx, bool):
                self.stream_protocol_error = ("protocol_error", "step_update step_index must be an integer (not bool)")
                return

            step_type = step_data.get("step_type")
            if step_type not in VALID_STEP_TYPES:
                self.stream_protocol_error = ("protocol_error", f"Invalid or missing 'step_type': '{step_type}'")
                return

            step_state = step_data.get("state")
            if step_state not in ("ACTIVE", "DONE"):
                self.stream_protocol_error = ("protocol_error", f"Invalid step state '{step_state}'")
                return

            tool_info = step_data.get("tool_info")
            if tool_info is not None and not isinstance(tool_info, dict):
                self.stream_protocol_error = ("protocol_error", "tool_info must be an object")
                return

            if step_type == "tool":
                tool_name = tool_info.get("name", "tool") if isinstance(tool_info, dict) else "tool"
                if step_state == "ACTIVE":
                    self.pending_tool_steps[step_idx] = str(tool_name)
                elif step_state == "DONE":
                    self.pending_tool_steps.pop(step_idx, None)

                if isinstance(tool_info, dict) and "error" in tool_info:
                    err_val = tool_info["error"]
                    if err_val:
                        code, _ = classify_error_safely(str(err_val), default_code="fatal_step_error")
                        if code in ("permission_denied", "sandbox_violation"):
                            self.has_denied_tool = True
                            self.denied_code = code
                        else:
                            self.has_step_error = True
                            self.step_error_code = code

        elif evt_type == "result":
            if not self.seen_init:
                self.stream_protocol_error = ("protocol_error", "result event received before init event")
                return

            if self.seen_result:
                self.stream_protocol_error = ("protocol_error", "Duplicate result event in stream")
                return
            self.seen_result = True

            res_conv = evt.get("conversation_id")
            res_data = evt.get("result")
            if not isinstance(res_data, dict):
                self.stream_protocol_error = ("protocol_error", "Malformed result event: 'result' is not an object")
                return

            nested_conv = res_data.get("conversation_id")
            if res_conv and nested_conv and str(res_conv).lower() != str(nested_conv).lower():
                self.stream_protocol_error = ("protocol_error", "Mismatched conversation_id between top-level and nested result envelope")
                return

            effective_conv = res_conv or nested_conv
            if effective_conv:
                if not is_valid_uuid(str(effective_conv)) or str(effective_conv).lower() != self.captured_conv_id:
                    self.stream_protocol_error = ("protocol_error", "result conversation_id does not match session UUID")
                    return

            self.terminal_result_event = evt

        if self.on_event_callback:
            try:
                self.on_event_callback(evt)
            except CollaborationError as e:
                self.stream_protocol_error = (e.code, e.message)
                return
            except Exception as e:
                self.stream_protocol_error = ("checkpoint_error", f"Event callback failed during stream processing: {type(e).__name__}")
                return

        self.parsed_events.append(evt)

    def _handle_incremental_stderr(self, line: str) -> None:
        """Classifies stderr lines into safe error categories without leaking raw text."""
        line_str = line.strip()
        if not line_str:
            return

        code, _ = classify_error_safely(line_str, default_code="process_failure")
        if code in ("permission_denied", "sandbox_violation"):
            self.has_stderr_denial = True
            self.stderr_classified_code = code
        elif code == "fatal_step_error":
            self.has_stderr_error = True
            self.stderr_classified_code = code
        else:
            self.has_stderr_error = True
            if not self.stderr_classified_code:
                self.stderr_classified_code = "process_failure"

    def _finish_execution(self) -> TurnOutcome:
        """Finalizes execution after process exit, checking EOF and strictly validating results."""
        if self.t_in:
            self.t_in.join(timeout=1.0)
        if self.t_out:
            self.t_out.join(timeout=self.stream_drain_timeout)
        if self.t_err:
            self.t_err.join(timeout=self.stream_drain_timeout)

        exit_code = self.proc.returncode if self.proc else None

        if (self.t_out and self.t_out.is_alive()) or (self.t_err and self.t_err.is_alive()):
            return TurnOutcome(
                status="execution_unknown",
                exit_code=exit_code,
                conversation_id=self.captured_conv_id,
                error_code="process_error",
                error_message="Stream reader threads did not reach EOF within timeout; background operations remain active",
                process_identity=self.get_process_identity()
            )

        if self.reader_error:
            code, msg = self.reader_error
            return TurnOutcome(
                status="ERROR",
                exit_code=exit_code,
                conversation_id=self.captured_conv_id,
                error_code=code,
                error_message=msg,
                process_identity=self.get_process_identity()
            )

        if self.stream_protocol_error:
            code, msg = self.stream_protocol_error
            return TurnOutcome(
                status="ERROR",
                exit_code=exit_code,
                conversation_id=self.captured_conv_id,
                error_code=code,
                error_message=msg,
                process_identity=self.get_process_identity()
            )

        if self.has_stderr_denial:
            return TurnOutcome(
                status="ERROR",
                exit_code=exit_code,
                conversation_id=self.captured_conv_id,
                error_code=self.stderr_classified_code or "permission_denied",
                error_message="Execution permission denied in stderr output",
                process_identity=self.get_process_identity()
            )

        if self.has_denied_tool:
            return TurnOutcome(
                status="ERROR",
                exit_code=exit_code,
                conversation_id=self.captured_conv_id,
                error_code=self.denied_code or "permission_denied",
                error_message="Tool execution denied by policy or permission check",
                process_identity=self.get_process_identity()
            )

        if self.has_step_error:
            return TurnOutcome(
                status="ERROR",
                exit_code=exit_code,
                conversation_id=self.captured_conv_id,
                error_code=self.step_error_code or "fatal_step_error",
                error_message="Fatal step execution failure encountered",
                process_identity=self.get_process_identity()
            )

        if exit_code != 0 or self.has_stderr_error:
            code = self.stderr_classified_code or "process_failure"
            msg = f"Process exited with error code {exit_code}" if exit_code != 0 else "Execution error reported in stderr stream"
            return TurnOutcome(
                status="ERROR",
                exit_code=exit_code,
                conversation_id=self.captured_conv_id,
                error_code=code,
                error_message=msg,
                process_identity=self.get_process_identity()
            )

        if self.stdin_error:
            return TurnOutcome(
                status="ERROR",
                exit_code=exit_code,
                conversation_id=self.captured_conv_id,
                error_code="process_error",
                error_message="Stdin pipe write failed during prompt delivery",
                process_identity=self.get_process_identity()
            )

        if not self.terminal_result_event:
            return TurnOutcome(
                status="ERROR",
                exit_code=exit_code,
                conversation_id=self.captured_conv_id,
                error_code="empty_result",
                error_message="Worker terminated without terminal 'result' event",
                process_identity=self.get_process_identity()
            )

        res_data = self.terminal_result_event.get("result")
        if not isinstance(res_data, dict):
            return TurnOutcome(
                status="ERROR",
                exit_code=exit_code,
                conversation_id=self.captured_conv_id,
                error_code="protocol_error",
                error_message="Malformed result event envelope",
                process_identity=self.get_process_identity()
            )

        res_status = res_data.get("status")
        has_result_error = bool(res_data.get("error"))
        has_denied = bool(res_data.get("denied_actions") or res_data.get("denied_tools") or res_data.get("permission_denied"))
        if res_status != "SUCCESS" or has_result_error or has_denied:
            raw_err = res_data.get("error") or "Execution denied or returned error in result envelope"
            code, safe_msg = classify_error_safely(
                str(raw_err),
                default_code="permission_denied" if has_denied else "provider_error",
                default_msg="Tool execution denied" if has_denied else "Provider returned error status"
            )
            return TurnOutcome(
                status="ERROR",
                exit_code=exit_code,
                conversation_id=self.captured_conv_id,
                error_code=code,
                error_message=safe_msg,
                process_identity=self.get_process_identity()
            )

        if self.pending_tool_steps:
            return TurnOutcome(
                status="execution_unknown",
                exit_code=exit_code,
                conversation_id=self.captured_conv_id,
                error_code="process_error",
                error_message=f"Pending tool steps remaining active at stream EOF (count={len(self.pending_tool_steps)})",
                process_identity=self.get_process_identity()
            )

        raw_struct = res_data.get("structured_output")
        try:
            validated_result = decode_and_validate_structured_output(
                raw_struct,
                expected_work_id=self.work_id,
                expected_turn_id=self.turn_id
            )
        except CollaborationError as e:
            return TurnOutcome(
                status="ERROR",
                exit_code=exit_code,
                conversation_id=self.captured_conv_id,
                error_code=e.code,
                error_message=e.message,
                process_identity=self.get_process_identity()
            )

        return TurnOutcome(
            status="SUCCESS",
            result=validated_result,
            conversation_id=self.captured_conv_id,
            exit_code=0,
            raw_events=self.parsed_events,
            process_identity=self.get_process_identity()
        )
