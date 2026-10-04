"""
Unit tests for Antigravity single-turn detached worker and quota adapter (Tasks T011, T048).
Tests protocol/process negative cases using configurable fake_cli.py with official nested envelopes.
Includes regressions for:
- Blocked stdin prompt delivery and late observation
- Chunk-based bounded line decoding and overflow handling
- Stream EOF and reader thread verification (delayed fatal error)
- Official step_type validation and rejection of invalid types/types as bool
- Conversation UUID consistency across all stream steps
- Canary token / diagnostic secret sanitization in stderr and tool error
- Structured process identity attributes and hooks
"""

import os
import sys
import json
import uuid
import time
import shutil
import tempfile
import unittest
from pathlib import Path

# Add project root to sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.ai.collaboration.worker import (
    AntigravityTurnWorker,
    save_executor_json_schema,
    TurnOutcome,
)
from scripts.ai.collaboration.environment import QuotaAdapter
from scripts.ai.collaboration.state import CollaborationError

FAKE_CLI_PATH = Path(__file__).resolve().parent / "fake_cli.py"


class TestWorkerAndQuota(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.work_id = str(uuid.uuid4())
        self.turn_id = str(uuid.uuid4())
        self.schema_path = Path(self.temp_dir) / "test_schema.json"
        save_executor_json_schema(self.schema_path)

        os.environ.pop("FAKE_AGY_MODE", None)
        os.environ["FAKE_AGY_WORK_ID"] = self.work_id
        os.environ["FAKE_AGY_TURN_ID"] = self.turn_id

    def tearDown(self):
        os.environ.pop("FAKE_AGY_MODE", None)
        os.environ.pop("FAKE_AGY_WORK_ID", None)
        os.environ.pop("FAKE_AGY_TURN_ID", None)
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _create_worker(self, timeout_seconds=10.0, conversation_id=None, stream_drain_timeout=3.0):
        return AntigravityTurnWorker(
            agy_exe=Path(sys.executable),
            root_dir=Path(self.temp_dir),
            work_id=self.work_id,
            turn_id=self.turn_id,
            prompt="Test prompt for fake CLI",
            conversation_id=conversation_id,
            timeout_seconds=timeout_seconds,
            stream_drain_timeout=stream_drain_timeout,
            cli_args_prefix=[str(FAKE_CLI_PATH)],
        )

    def test_success_turn(self):
        os.environ["FAKE_AGY_MODE"] = "success_result"
        worker = self._create_worker()
        outcome = worker.execute(self.schema_path)

        self.assertEqual(outcome.status, "SUCCESS")
        self.assertEqual(outcome.exit_code, 0)
        self.assertIsNotNone(outcome.result)
        self.assertEqual(outcome.result.work_id, self.work_id)
        self.assertEqual(outcome.result.turn_id, self.turn_id)
        self.assertIsNotNone(worker.pid)

    def test_error_exit0_nested_envelope(self):
        # Official nested result error envelope with exit code 0 must fail
        os.environ["FAKE_AGY_MODE"] = "error_exit0"
        worker = self._create_worker()
        outcome = worker.execute(self.schema_path)

        self.assertEqual(outcome.status, "ERROR")
        self.assertEqual(outcome.error_code, "provider_error")
        self.assertIn("provider", outcome.error_message.lower())

    def test_denied_tool_exit0_nested_envelope(self):
        # Tool denial in step_update tool_info must fail even with exit 0
        os.environ["FAKE_AGY_MODE"] = "denied_exit0"
        worker = self._create_worker()
        outcome = worker.execute(self.schema_path)

        self.assertEqual(outcome.status, "ERROR")
        self.assertEqual(outcome.error_code, "permission_denied")
        self.assertIn("denied", outcome.error_message.lower())

    def test_stderr_denied_exit0(self):
        # Tool or sandbox denial in stderr with exit 0 must fail without leaking raw stderr
        os.environ["FAKE_AGY_MODE"] = "stderr_denied_exit0"
        worker = self._create_worker()
        outcome = worker.execute(self.schema_path)

        self.assertEqual(outcome.status, "ERROR")
        self.assertEqual(outcome.error_code, "permission_denied")

    def test_pending_tool_tracked_by_step_index(self):
        # Pending tool step remains ACTIVE even if another agent step finishes -> execution_unknown
        os.environ["FAKE_AGY_MODE"] = "pending_tools"
        worker = self._create_worker()
        outcome = worker.execute(self.schema_path)

        self.assertEqual(outcome.status, "execution_unknown")
        self.assertEqual(outcome.error_code, "process_error")
        self.assertIn("Pending tool steps", outcome.error_message)

    def test_result_envelope_with_denied_actions_rejected(self):
        # Result status SUCCESS but with denied_actions must NOT be treated as success
        os.environ["FAKE_AGY_MODE"] = "result_denied_actions"
        worker = self._create_worker()
        outcome = worker.execute(self.schema_path)

        self.assertEqual(outcome.status, "ERROR")
        self.assertEqual(outcome.error_code, "permission_denied")

    def test_nested_result_conversation_id_mismatch_rejected(self):
        # Mismatch between top-level and nested result conversation_id must raise protocol_error
        os.environ["FAKE_AGY_MODE"] = "nested_conv_mismatch"
        worker = self._create_worker()
        outcome = worker.execute(self.schema_path)

        self.assertEqual(outcome.status, "ERROR")
        self.assertEqual(outcome.error_code, "protocol_error")

    def test_event_callback_failure_raises_checkpoint_error(self):
        def failing_callback(evt):
            raise IOError("Simulated persistence/database write failure")

        worker = AntigravityTurnWorker(
            agy_exe=Path(sys.executable),
            root_dir=Path(self.temp_dir),
            work_id=self.work_id,
            turn_id=self.turn_id,
            prompt="Test prompt",
            on_event_callback=failing_callback,
            cli_args_prefix=[str(FAKE_CLI_PATH)],
        )
        os.environ["FAKE_AGY_MODE"] = "success_result"
        outcome = worker.execute(self.schema_path)

        self.assertEqual(outcome.status, "ERROR")
        self.assertEqual(outcome.error_code, "checkpoint_error")

    def test_agent_active_is_not_pending_tool(self):
        # Step type 'agent_response' in ACTIVE state does NOT trigger pending tool error
        os.environ["FAKE_AGY_MODE"] = "agent_active_no_tool"
        worker = self._create_worker()
        outcome = worker.execute(self.schema_path)

        self.assertEqual(outcome.status, "SUCCESS")

    def test_oversized_line_triggers_buffer_error(self):
        # A single line exceeding 64KB triggers buffer protocol error
        os.environ["FAKE_AGY_MODE"] = "oversized_line"
        worker = self._create_worker()
        outcome = worker.execute(self.schema_path)

        self.assertEqual(outcome.status, "ERROR")
        self.assertEqual(outcome.error_code, "protocol_error")
        self.assertIn("buffer", outcome.error_message.lower())

    def test_unknown_event_type(self):
        os.environ["FAKE_AGY_MODE"] = "unknown_event"
        worker = self._create_worker()
        outcome = worker.execute(self.schema_path)

        self.assertEqual(outcome.status, "ERROR")
        self.assertEqual(outcome.error_code, "protocol_error")
        self.assertIn("Unknown stream event", outcome.error_message)

    def test_duplicate_init_event(self):
        os.environ["FAKE_AGY_MODE"] = "duplicate_init"
        worker = self._create_worker()
        outcome = worker.execute(self.schema_path)

        self.assertEqual(outcome.status, "ERROR")
        self.assertEqual(outcome.error_code, "protocol_error")
        self.assertIn("Duplicate init", outcome.error_message)

    def test_changed_conversation_uuid_rejected(self):
        # Resuming with expected UUID but stream returns different UUID
        expected_conv = str(uuid.uuid4())
        os.environ["FAKE_AGY_MODE"] = "changed_conversation_uuid"
        worker = self._create_worker(conversation_id=expected_conv)
        outcome = worker.execute(self.schema_path)

        self.assertEqual(outcome.status, "ERROR")
        self.assertEqual(outcome.error_code, "session_unavailable")

    def test_timeout_execution_unknown_and_late_observation(self):
        # Process exceeds short deadline -> execution_unknown
        os.environ["FAKE_AGY_MODE"] = "long_turn"
        worker = self._create_worker(timeout_seconds=0.2)
        outcome = worker.execute(self.schema_path)

        self.assertEqual(outcome.status, "execution_unknown")
        self.assertEqual(outcome.error_code, "timeout")

        # Late observation while process is still sleeping continues to report execution_unknown
        late_outcome = worker.observe_late_exit(timeout_seconds=0.1)
        self.assertEqual(late_outcome.status, "execution_unknown")

    # -------------------------------------------------------------------------
    # Regression Tests: Prompt Deadline, EOF, Wire step_type, Canary & Identity
    # -------------------------------------------------------------------------
    def test_blocked_stdin_prompt_timeout_and_late_observe(self):
        # Regression 1: CLI delays reading stdin by 0.2s; short timeout of 0.03s must NOT claim success
        os.environ["FAKE_AGY_MODE"] = "blocked_stdin"
        worker = self._create_worker(timeout_seconds=0.03)
        outcome = worker.execute(self.schema_path)

        self.assertEqual(outcome.status, "execution_unknown")
        self.assertEqual(outcome.error_code, "timeout")
        self.assertIsNotNone(worker.pid)

        # Observing later allows delayed stdin read & process completion
        late_outcome = worker.observe_late_exit(timeout_seconds=2.0)
        self.assertEqual(late_outcome.status, "SUCCESS")
        self.assertEqual(late_outcome.exit_code, 0)
        self.assertIsNotNone(late_outcome.result)

    def test_delayed_fatal_error_rejected_before_eof(self):
        # Regression 3: CLI outputs valid result, then late fatal error before stream EOF
        os.environ["FAKE_AGY_MODE"] = "delayed_fatal_error"
        worker = self._create_worker()
        outcome = worker.execute(self.schema_path)

        self.assertNotEqual(outcome.status, "SUCCESS")
        self.assertEqual(outcome.status, "ERROR")

    def test_stream_eof_not_reached_active_reader(self):
        # Regression 3: CLI hangs stdout open (active reader after drain timeout) -> NOT SUCCESS
        os.environ["FAKE_AGY_MODE"] = "active_reader_leak"
        worker = self._create_worker(timeout_seconds=1.0, stream_drain_timeout=0.2)
        outcome = worker.execute(self.schema_path)

        self.assertEqual(outcome.status, "execution_unknown")
        self.assertEqual(outcome.error_code, "process_error")
        self.assertIn("EOF", outcome.error_message)

    def test_invalid_step_type_rejected(self):
        # Regression 4: Unrecognized step_type in official envelope must be rejected
        os.environ["FAKE_AGY_MODE"] = "invalid_step_type"
        worker = self._create_worker()
        outcome = worker.execute(self.schema_path)

        self.assertEqual(outcome.status, "ERROR")
        self.assertEqual(outcome.error_code, "protocol_error")
        self.assertIn("step_type", outcome.error_message)

    def test_step_index_bool_rejected(self):
        # Regression 4: step_index given as boolean (True) must be rejected
        os.environ["FAKE_AGY_MODE"] = "invalid_step_index_bool"
        worker = self._create_worker()
        outcome = worker.execute(self.schema_path)

        self.assertEqual(outcome.status, "ERROR")
        self.assertEqual(outcome.error_code, "protocol_error")
        self.assertIn("step_index", outcome.error_message)

    def test_step_changes_conversation_uuid_rejected(self):
        # Regression 4: step_update attempting to switch conversation UUID is rejected
        os.environ["FAKE_AGY_MODE"] = "step_changes_conversation_uuid"
        worker = self._create_worker()
        outcome = worker.execute(self.schema_path)

        self.assertEqual(outcome.status, "ERROR")
        self.assertEqual(outcome.error_code, "protocol_error")
        self.assertIn("conversation_id", outcome.error_message)

    def test_canary_token_sanitization_in_stderr(self):
        # Regression 5: Stderr containing secret canary token must NOT leak into outcome
        canary = "CANARY_SECRET_KEY_98765"
        os.environ["FAKE_AGY_MODE"] = "canary_stderr"
        worker = self._create_worker()
        outcome = worker.execute(self.schema_path)

        self.assertEqual(outcome.status, "ERROR")
        self.assertEqual(outcome.error_code, "permission_denied")
        self.assertNotIn(canary, outcome.error_message)
        self.assertNotIn(canary, str(outcome.to_dict()))

    def test_canary_token_sanitization_in_tool_error(self):
        # Regression 5: Tool error containing canary token must NOT leak into outcome
        canary = "CANARY_TOOL_TOKEN_54321"
        os.environ["FAKE_AGY_MODE"] = "canary_tool_error"
        worker = self._create_worker()
        outcome = worker.execute(self.schema_path)

        self.assertEqual(outcome.status, "ERROR")
        self.assertEqual(outcome.error_code, "permission_denied")
        self.assertNotIn(canary, outcome.error_message)
        self.assertNotIn(canary, str(outcome.to_dict()))

    def test_canary_token_sanitization_in_structured_output_ids(self):
        # Regression: Structured output containing canary in work_id / turn_id must NOT leak into outcome
        canary = "CANARY_SECRET_WORK_ID_778899"
        os.environ["FAKE_AGY_MODE"] = "wrong_ids_canary"
        worker = self._create_worker()
        outcome = worker.execute(self.schema_path)

        self.assertEqual(outcome.status, "ERROR")
        self.assertEqual(outcome.error_code, "protocol_error")
        self.assertNotIn(canary, str(outcome.error_message))
        self.assertNotIn(canary, str(outcome.to_dict()))

    def test_process_identity_attributes(self):
        # Regression 6: Worker exposes structured process identity attributes
        worker = self._create_worker()
        ident = worker.get_process_identity()

        self.assertIn("worker_token", ident)
        self.assertIn("pid", ident)
        self.assertIn("is_alive", ident)
        self.assertEqual(ident["work_id"], self.work_id)
        self.assertEqual(ident["turn_id"], self.turn_id)

    # -------------------------------------------------------------------------
    # Quota Adapter Tests (T048)
    # -------------------------------------------------------------------------
    def test_quota_adapter_gemini_models_available(self):
        os.environ["FAKE_AGY_MODE"] = "usage_ok"
        adapter = QuotaAdapter(
            agy_exe=Path(sys.executable),
            threshold=0.20,
            cli_args_prefix=[str(FAKE_CLI_PATH)],
        )
        snapshot = adapter.check_quota(model_override="gemini-3.8-flash")

        self.assertEqual(snapshot.availability, "available")
        self.assertEqual(snapshot.group_name, "Gemini Models")
        self.assertGreater(snapshot.buckets[0]["remaining_fraction"], 0.20)

    def test_quota_adapter_claude_gpt_group(self):
        # Live verified group name: 'Claude and GPT models'
        os.environ["FAKE_AGY_MODE"] = "usage_ok"
        adapter = QuotaAdapter(
            agy_exe=Path(sys.executable),
            threshold=0.20,
            cli_args_prefix=[str(FAKE_CLI_PATH)],
        )
        snapshot = adapter.check_quota(model_override="claude-3-7-sonnet")

        self.assertEqual(snapshot.availability, "available")
        self.assertEqual(snapshot.group_name, "Claude and GPT models")

    def test_quota_adapter_unknown_model_no_fallback(self):
        # An unknown model must NOT fall back to Gemini! Must be unknown.
        os.environ["FAKE_AGY_MODE"] = "usage_ok"
        adapter = QuotaAdapter(
            agy_exe=Path(sys.executable),
            threshold=0.20,
            cli_args_prefix=[str(FAKE_CLI_PATH)],
        )
        snapshot = adapter.check_quota(model_override="custom-unrecognized-model")

        self.assertEqual(snapshot.availability, "unknown")
        self.assertIn("Unrecognized model", snapshot.uncertainty_reason)

    def test_quota_adapter_boolean_fraction_rejected(self):
        # Boolean remaining_fraction (True) must be rejected and marked unknown
        os.environ["FAKE_AGY_MODE"] = "usage_boolean_fraction"
        adapter = QuotaAdapter(
            agy_exe=Path(sys.executable),
            threshold=0.20,
            cli_args_prefix=[str(FAKE_CLI_PATH)],
        )
        snapshot = adapter.check_quota(model_override="gemini-3.8-flash")

        self.assertEqual(snapshot.availability, "unknown")
        self.assertIn("invalid/missing remaining_fraction", snapshot.uncertainty_reason)

    def test_quota_adapter_nan_fraction_rejected(self):
        os.environ["FAKE_AGY_MODE"] = "usage_nan_fraction"
        adapter = QuotaAdapter(
            agy_exe=Path(sys.executable),
            threshold=0.20,
            cli_args_prefix=[str(FAKE_CLI_PATH)],
        )
        snapshot = adapter.check_quota(model_override="gemini-3.8-flash")

        self.assertEqual(snapshot.availability, "unknown")

    def test_quota_adapter_missing_fraction_rejected(self):
        os.environ["FAKE_AGY_MODE"] = "usage_missing_fraction"
        adapter = QuotaAdapter(
            agy_exe=Path(sys.executable),
            threshold=0.20,
            cli_args_prefix=[str(FAKE_CLI_PATH)],
        )
        snapshot = adapter.check_quota(model_override="gemini-3.8-flash")

        self.assertEqual(snapshot.availability, "unknown")

    def test_quota_adapter_expired_reset_rejected(self):
        # Past reset timestamp indicates stale/expired quota
        os.environ["FAKE_AGY_MODE"] = "usage_expired_reset"
        adapter = QuotaAdapter(
            agy_exe=Path(sys.executable),
            threshold=0.20,
            cli_args_prefix=[str(FAKE_CLI_PATH)],
        )
        snapshot = adapter.check_quota(model_override="gemini-3.8-flash")

        self.assertEqual(snapshot.availability, "unknown")
        self.assertIn("missing, naive, or expired reset_time", snapshot.uncertainty_reason)

    def test_quota_adapter_duplicate_group_rejected(self):
        os.environ["FAKE_AGY_MODE"] = "usage_duplicate_group"
        adapter = QuotaAdapter(
            agy_exe=Path(sys.executable),
            threshold=0.20,
            cli_args_prefix=[str(FAKE_CLI_PATH)],
        )
        snapshot = adapter.check_quota(model_override="gemini-3.8-flash")

        self.assertEqual(snapshot.availability, "unknown")
        self.assertIn("Duplicate group", snapshot.uncertainty_reason)

    def test_worker_main_invalid_cli_args_prefix_rejected_before_execution(self):
        # Review Point 5 & 7: invalid --cli-args-prefix JSON or non-list-of-strings rejected with exit!=0 before model call
        import subprocess
        launcher_script = PROJECT_ROOT / "scripts" / "ai" / "collaboration" / "worker_main.py"

        # 1. Invalid JSON string
        r1 = subprocess.run(
            [
                sys.executable,
                str(launcher_script),
                "--work-id", str(uuid.uuid4()),
                "--turn-id", str(uuid.uuid4()),
                "--target-revision", "2",
                "--worker-token", "token123",
                "--base-dir", str(self.temp_dir),
                "--project-root", str(self.temp_dir),
                "--agy-exe", sys.executable,
                "--prompt-file", str(Path(self.temp_dir) / "prompt.txt"),
                "--schema-file", str(Path(self.temp_dir) / "schema.json"),
                "--cli-args-prefix", "{invalid json",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(r1.returncode, 0)
        self.assertIn("Invalid JSON for --cli-args-prefix", r1.stderr)

        # 2. Non-list JSON (e.g. integer)
        r2 = subprocess.run(
            [
                sys.executable,
                str(launcher_script),
                "--work-id", str(uuid.uuid4()),
                "--turn-id", str(uuid.uuid4()),
                "--target-revision", "2",
                "--worker-token", "token123",
                "--base-dir", str(self.temp_dir),
                "--project-root", str(self.temp_dir),
                "--agy-exe", sys.executable,
                "--prompt-file", str(Path(self.temp_dir) / "prompt.txt"),
                "--schema-file", str(Path(self.temp_dir) / "schema.json"),
                "--cli-args-prefix", "12345",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(r2.returncode, 0)
        self.assertIn("must decode to a JSON list of strings", r2.stderr)


if __name__ == "__main__":
    unittest.main()
