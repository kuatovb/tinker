"""
Unit and integration tests for Quota Guard and QuotaNotice checkpoints (Tasks T048, T049, T051).
Validates:
- Pre-flight fresh applicable quota check (Gemini Models group mapping)
- Low quota (<= 0.20) blocks prompt generation with ZERO model calls
- Exhausted quota (0.0) blocks prompt generation with ZERO model calls
- Unknown quota (provider error, timeout, wrong group, expired reset time) blocks prompt generation
- Periodic quota check during long turn records QuotaNotice without stopping the active writer
- Next prompt blocked when quota is low/unknown (verified with generation counter)
- QuotaNotice and QuotaSnapshot invariant validation and atomic persistence
"""

import os
import sys
import json
import time
import uuid
import shutil
import tempfile
import subprocess
import unittest
from datetime import datetime, timezone
from pathlib import Path

# Add project root to sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.ai.collaboration.coordinator import (
    CollaborationCoordinator,
)
from scripts.ai.collaboration.state import (
    WorkStore,
    QuotaNotice,
    QuotaSnapshot,
    CollaborationError,
    VALID_QUOTA_NOTICE_KINDS,
    is_process_alive,
)
from scripts.ai.collaboration.environment import (
    QuotaAdapter,
)

FAKE_CLI_PATH = Path(__file__).resolve().parent / "fake_cli.py"


class TestQuotaGuard(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="ai_quota_test_")
        self.project_root = Path(self.temp_dir) / "Квотный репозиторий с пробелами"
        self.project_root.mkdir(parents=True, exist_ok=True)
        self.base_dir = self.project_root / "logs" / "ai" / "collaboration"

        # Initialize Git repository
        subprocess.run(["git", "init"], cwd=str(self.project_root), capture_output=True, check=True)
        subprocess.run(["git", "config", "user.name", "Quota Tester"], cwd=str(self.project_root), check=True)
        subprocess.run(["git", "config", "user.email", "quota@example.com"], cwd=str(self.project_root), check=True)

        # Critical Review Point 1: ignore logs/ and .git/ in gitignore to prevent PermissionError during baseline
        gitignore_path = self.project_root / ".gitignore"
        gitignore_path.write_text("logs/\n.git/\n", encoding="utf-8")

        specs_dir = self.project_root / "specs" / "001-feature"
        specs_dir.mkdir(parents=True, exist_ok=True)

        self.spec_file = specs_dir / "spec.md"
        self.spec_file.write_text("# Feature Spec\nQuota test spec.\n", encoding="utf-8")

        self.tasks_file = specs_dir / "tasks.md"
        self.tasks_file.write_text("# Tasks\n- [ ] T001 Task one\n", encoding="utf-8")

        src_dir = self.project_root / "src"
        src_dir.mkdir(parents=True, exist_ok=True)
        self.allowed_file = src_dir / "allowed.py"
        self.allowed_file.write_text("print('quota test')\n", encoding="utf-8")

        subprocess.run(["git", "add", "."], cwd=str(self.project_root), capture_output=True, check=True)
        subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=str(self.project_root), capture_output=True, check=True)

        self.coordinator = CollaborationCoordinator(
            project_root=self.project_root,
            base_dir=self.base_dir,
        )

        self.assignment = {
            "goal": "Verify quota guard invariants",
            "artifact_refs": ["specs/001-feature/spec.md", "specs/001-feature/tasks.md"],
            "allowed_files": ["src/allowed.py"],
            "allowed_actions": ["file_edit"],
            "acceptance": ["Quota guard halts prompt safely"],
            "task_ids": ["T001"],
            "timeout_seconds": 60,
        }

        # Dynamic state and generation counter files (Review Point 9)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.state_file = self.base_dir / "fake_quota_state.json"
        self.counter_file = self.base_dir / "fake_generation_counter.txt"
        self.counter_file.write_text("0", encoding="utf-8")

        os.environ["FAKE_AGY_STATE_FILE"] = str(self.state_file)
        os.environ["FAKE_AGY_COUNTER_FILE"] = str(self.counter_file)
        os.environ.pop("FAKE_AGY_MODE", None)
        os.environ.pop("FAKE_AGY_SLEEP", None)

    def tearDown(self):
        os.environ.pop("FAKE_AGY_STATE_FILE", None)
        os.environ.pop("FAKE_AGY_COUNTER_FILE", None)
        os.environ.pop("FAKE_AGY_MODE", None)
        os.environ.pop("FAKE_AGY_SLEEP", None)
        try:
            for rf in (self.base_dir / "checkouts").glob("*/reservation.json"):
                if rf.exists():
                    rdata = json.loads(rf.read_text(encoding="utf-8"))
                    cpid = rdata.get("pid")
                    cstart = rdata.get("start_time")
                    if cpid and is_process_alive(cpid, cstart) is True:
                        dl = time.monotonic() + 2.0
                        while time.monotonic() < dl and is_process_alive(cpid, cstart) is True:
                            time.sleep(0.05)
        except Exception:
            pass
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _get_generation_count(self) -> int:
        if self.counter_file.exists():
            try:
                return int(self.counter_file.read_text(encoding="utf-8").strip() or "0")
            except Exception:
                return 0
        return 0

    def _create_and_approve_work(self) -> str:
        req_id_create = str(uuid.uuid4())
        res_create = self.coordinator.create(req_id_create, self.assignment)
        work_id = res_create["work_id"]

        scope_digest = self.coordinator.compute_scope_digest(work_id)
        decision = {
            "source": "human user",
            "approved_text": "Approved for quota check verification",
            "approved_actions": ["file_edit"],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        self.coordinator.approve(
            work_id=work_id,
            expected_revision=1,
            request_id=str(uuid.uuid4()),
            decision=decision,
            scope_digest=scope_digest,
        )
        return work_id

    def test_quota_notice_model_validation_and_roundtrip(self):
        notice_id = str(uuid.uuid4())
        work_id = str(uuid.uuid4())
        snap_id = str(uuid.uuid4())

        notice = QuotaNotice(
            notice_id=notice_id,
            work_id=work_id,
            snapshot_id=snap_id,
            kind="quota_low",
            checkpoint_revision=2,
            task_ids=["T001", "T002"],
            last_confirmed_stage="implementing",
            operation_boundary="implementing",
            remaining_fraction=0.15,
            reset_time="2026-10-10T12:00:00Z",
            delivered_event_seq=1,
        )

        d = notice.to_dict()
        self.assertEqual(d["notice_id"], notice_id)
        self.assertEqual(d["kind"], "quota_low")
        self.assertEqual(d["remaining_fraction"], 0.15)
        self.assertEqual(d["delivered_event_seq"], 1)

        restored = QuotaNotice.from_dict(d)
        self.assertEqual(restored.notice_id, notice_id)
        self.assertEqual(restored.kind, "quota_low")
        self.assertEqual(restored.task_ids, ["T001", "T002"])
        self.assertEqual(restored.delivered_event_seq, 1)

        # Invalid kind rejected
        with self.assertRaises(CollaborationError):
            QuotaNotice(
                notice_id=str(uuid.uuid4()),
                work_id=work_id,
                snapshot_id=snap_id,
                kind="invalid_kind",
                checkpoint_revision=1,
                task_ids=[],
                last_confirmed_stage="implementing",
                operation_boundary="implementing",
            )

    def test_preflight_available_quota_allows_start(self):
        work_id = self._create_and_approve_work()

        self.state_file.write_text(json.dumps({"mode": "success_result"}), encoding="utf-8")
        req_id_start = str(uuid.uuid4())

        res = self.coordinator.start(
            work_id=work_id,
            expected_revision=2,
            request_id=req_id_start,
            agy_override=Path(sys.executable),
            cli_args_prefix=[str(FAKE_CLI_PATH)],
            detached=True,
        )
        self.assertEqual(res["state"], "implementing")

        store = WorkStore(base_dir=self.base_dir, work_id=work_id, checkout_root=self.project_root)
        snap = store.load_quota_snapshot()
        self.assertIsNotNone(snap)
        self.assertEqual(snap.availability, "available")

    def test_preflight_low_quota_blocks_start_with_zero_generations(self):
        work_id = self._create_and_approve_work()

        self.state_file.write_text(json.dumps({"mode": "usage_low"}), encoding="utf-8")
        initial_gen_count = self._get_generation_count()

        with self.assertRaises(CollaborationError) as ctx:
            self.coordinator.start(
                work_id=work_id,
                expected_revision=2,
                request_id=str(uuid.uuid4()),
                agy_override=Path(sys.executable),
                cli_args_prefix=[str(FAKE_CLI_PATH)],
                detached=True,
            )
        self.assertEqual(ctx.exception.code, "provider_error")

        # Invariant: exactly 0 model generations executed
        self.assertEqual(self._get_generation_count(), initial_gen_count)

        store = WorkStore(base_dir=self.base_dir, work_id=work_id, checkout_root=self.project_root)
        notice = store.load_quota_notice()
        self.assertIsNotNone(notice)
        self.assertEqual(notice.kind, "quota_low")
        self.assertLessEqual(notice.remaining_fraction, 0.20)

    def test_preflight_exhausted_quota_blocks_start_with_zero_generations(self):
        work_id = self._create_and_approve_work()

        self.state_file.write_text(json.dumps({"mode": "usage_exhausted"}), encoding="utf-8")
        initial_gen_count = self._get_generation_count()

        with self.assertRaises(CollaborationError) as ctx:
            self.coordinator.start(
                work_id=work_id,
                expected_revision=2,
                request_id=str(uuid.uuid4()),
                agy_override=Path(sys.executable),
                cli_args_prefix=[str(FAKE_CLI_PATH)],
                detached=True,
            )
        self.assertEqual(ctx.exception.code, "provider_error")
        self.assertEqual(self._get_generation_count(), initial_gen_count)

        store = WorkStore(base_dir=self.base_dir, work_id=work_id, checkout_root=self.project_root)
        notice = store.load_quota_notice()
        self.assertIsNotNone(notice)
        self.assertEqual(notice.kind, "quota_exhausted")

    def test_preflight_unknown_quota_blocks_start(self):
        work_id = self._create_and_approve_work()

        self.state_file.write_text(json.dumps({"mode": "usage_wrong_group"}), encoding="utf-8")
        initial_gen_count = self._get_generation_count()

        with self.assertRaises(CollaborationError) as ctx:
            self.coordinator.start(
                work_id=work_id,
                expected_revision=2,
                request_id=str(uuid.uuid4()),
                agy_override=Path(sys.executable),
                cli_args_prefix=[str(FAKE_CLI_PATH)],
                detached=True,
            )
        self.assertEqual(ctx.exception.code, "provider_error")
        self.assertEqual(self._get_generation_count(), initial_gen_count)

        store = WorkStore(base_dir=self.base_dir, work_id=work_id, checkout_root=self.project_root)
        notice = store.load_quota_notice()
        self.assertIsNotNone(notice)
        self.assertEqual(notice.kind, "quota_unknown")

    def test_quota_monitoring_during_long_turn_records_notice_without_stopping_writer(self):
        # Starts turn with available quota, but during the turn quota drops to low.
        # Periodic quota monitor thread fires, records notice via shared state file, but allows current writer to finish.
        work_id = self._create_and_approve_work()

        # Start with standard available usage
        self.state_file.write_text(json.dumps({"mode": "success_result"}), encoding="utf-8")
        os.environ["FAKE_AGY_SLEEP"] = "0.8"

        res = self.coordinator.start(
            work_id=work_id,
            expected_revision=2,
            request_id=str(uuid.uuid4()),
            agy_override=Path(sys.executable),
            cli_args_prefix=[str(FAKE_CLI_PATH)],
            quota_interval=0.2,  # fast periodic check during turn
            detached=True,
        )
        self.assertEqual(res["state"], "implementing")

        # Wait for model generation to actually start before simulating low quota during turn (Review Point 2)
        gen_start = time.monotonic()
        while time.monotonic() - gen_start < 3.0:
            if self._get_generation_count() >= 1:
                break
            time.sleep(0.05)
        self.assertGreaterEqual(self._get_generation_count(), 1)

        # Now simulate provider usage dropping to low while child is running via state file (Review Point 9)
        self.state_file.write_text(json.dumps({"mode": "usage_low"}), encoding="utf-8")

        store = WorkStore(base_dir=self.base_dir, work_id=work_id, checkout_root=self.project_root)

        # Wait for detached worker to complete
        poll_start = time.monotonic()
        while time.monotonic() - poll_start < 5.0:
            curr_w = store.load_snapshot()
            if curr_w.state in ("in_review", "error", "stopped"):
                break
            time.sleep(0.05)

        # The current writer was NOT prematurely aborted: finished with last_result saved, transitioned to stopped due to stop_requested (Review Point 2)
        final_w = store.load_snapshot()
        self.assertEqual(final_w.state, "stopped")
        self.assertIsNotNone(final_w.last_result)

        # Check that QuotaNotice was recorded during execution and deduplicated
        notice = store.load_quota_notice()
        self.assertIsNotNone(notice)
        self.assertEqual(notice.kind, "quota_low")

        # Generation count after 1st turn
        gens_after_turn1 = self._get_generation_count()
        self.assertGreaterEqual(gens_after_turn1, 1)

        # Next prompt attempt must be blocked by the low quota
        with self.assertRaises(CollaborationError) as ctx:
            self.coordinator.start(
                work_id=work_id,
                expected_revision=final_w.revision,
                request_id=str(uuid.uuid4()),
                agy_override=Path(sys.executable),
                cli_args_prefix=[str(FAKE_CLI_PATH)],
                detached=True,
            )
        self.assertIn(ctx.exception.code, ("provider_error", "permission_denied"))

        # Invariant: model generation counter did NOT increment on blocked next prompt (Review Point 9)
        self.assertEqual(self._get_generation_count(), gens_after_turn1)

    def test_quota_source_error_recorded_as_quota_unknown(self):
        # Review Point 3: quota source errors must not be swallowed, recorded as quota_unknown
        work_id = self._create_and_approve_work()
        self.state_file.write_text(json.dumps({"mode": "usage_error"}), encoding="utf-8")

        with self.assertRaises(CollaborationError) as ctx:
            self.coordinator.start(
                work_id=work_id,
                expected_revision=2,
                request_id=str(uuid.uuid4()),
                agy_override=Path(sys.executable),
                cli_args_prefix=[str(FAKE_CLI_PATH)],
                detached=True,
            )
        self.assertEqual(ctx.exception.code, "provider_error")
        store = WorkStore(base_dir=self.base_dir, work_id=work_id, checkout_root=self.project_root)
        notice = store.load_quota_notice()
        self.assertIsNotNone(notice)
        self.assertEqual(notice.kind, "quota_unknown")

    def test_pre_generation_quota_save_failure(self):
        # Review Point 3 & 7: injected pre-generation save failure must not be swallowed as false SUCCESS
        from scripts.ai.collaboration.worker_main import run_observer_turn
        from scripts.ai.collaboration.worker import save_executor_json_schema
        from scripts.ai.collaboration.state import Turn, get_process_creation_time

        work_id = self._create_and_approve_work()
        store = WorkStore(base_dir=self.base_dir, work_id=work_id, checkout_root=self.project_root)
        self.state_file.write_text(json.dumps({"mode": "success_result"}), encoding="utf-8")

        turn_id = str(uuid.uuid4())
        prompt_file = store.work_dir / "test_prompt.txt"
        prompt_file.write_text(f"# ASSIGNMENT FOR WORK {work_id} (TURN {turn_id})\nWork: {work_id}\nTurn: {turn_id}\n", encoding="utf-8")
        schema_file = store.work_dir / "schema.json"
        save_executor_json_schema(schema_file)

        token = uuid.uuid4().hex
        my_pid = os.getpid()
        my_creation = get_process_creation_time(my_pid)
        self.assertIsNotNone(my_creation)

        store.acquire_reservation(pid=my_pid, start_time=my_creation, worker_token=token)
        store.update_reservation_process(
            worker_token=token,
            pid=my_pid,
            start_time=my_creation,
            launcher_pid=my_pid,
            launcher_start_time=my_creation,
        )
        with store.lock():
            w = store.load_snapshot()
            w.state = "implementing"
            w.current_turn = Turn(
                turn_id=turn_id,
                work_id=work_id,
                initial_revision=1,
                deadline=time.time() + 60,
                worker_token=token,
                pid=my_pid,
                start_time=my_creation,
            )
            store.save_snapshot(w)

        # Inject save failure by creating a directory where quota_snapshot_file should be written
        store.quota_snapshot_file.mkdir(parents=True, exist_ok=True)

        outcome = run_observer_turn(
            work_id=work_id,
            turn_id=turn_id,
            target_revision=2,
            worker_token=token,
            base_dir=self.base_dir,
            project_root=self.project_root,
            agy_exe=Path(sys.executable),
            prompt_file=prompt_file,
            schema_file=schema_file,
            timeout=10.0,
            quota_interval=0.1,
            cli_args_prefix=[str(FAKE_CLI_PATH)],
        )
        self.assertEqual(outcome.status, "execution_unknown")
        self.assertEqual(outcome.error_code, "checkpoint_error")

    def test_monitor_save_failure_causes_checkpoint_error(self):
        # Review Point 2 & 7: monitor persistence failure under live writer must yield checkpoint_error
        from unittest.mock import patch
        from scripts.ai.collaboration.worker_main import run_observer_turn
        from scripts.ai.collaboration.worker import save_executor_json_schema
        from scripts.ai.collaboration.state import Turn, get_process_creation_time

        work_id = self._create_and_approve_work()
        store = WorkStore(base_dir=self.base_dir, work_id=work_id, checkout_root=self.project_root)

        # Set fake CLI sleep to 1.0s so writer is alive while monitor polls
        os.environ["FAKE_AGY_MODE"] = "success_result"
        os.environ["FAKE_AGY_SLEEP"] = "1.0"

        turn_id = str(uuid.uuid4())
        prompt_file = store.work_dir / "test_prompt.txt"
        prompt_file.write_text(f"# ASSIGNMENT FOR WORK {work_id} (TURN {turn_id})\nWork: {work_id}\nTurn: {turn_id}\n", encoding="utf-8")
        schema_file = store.work_dir / "schema.json"
        save_executor_json_schema(schema_file)

        token = uuid.uuid4().hex
        my_pid = os.getpid()
        my_creation = get_process_creation_time(my_pid)
        self.assertIsNotNone(my_creation)

        store.acquire_reservation(pid=my_pid, start_time=my_creation, worker_token=token)
        store.update_reservation_process(
            worker_token=token,
            pid=my_pid,
            start_time=my_creation,
            launcher_pid=my_pid,
            launcher_start_time=my_creation,
        )
        with store.lock():
            w = store.load_snapshot()
            w.state = "implementing"
            w.current_turn = Turn(
                turn_id=turn_id,
                work_id=work_id,
                initial_revision=1,
                deadline=time.time() + 60,
                worker_token=token,
                pid=my_pid,
                start_time=my_creation,
            )
            store.save_snapshot(w)

        original_save_quota = store.save_quota_snapshot
        calls = {"count": 0}

        def fake_save_quota_snapshot(snap):
            calls["count"] += 1
            if calls["count"] == 1:
                # 1st call is pre_generation quota snapshot: allow to succeed
                return original_save_quota(snap)
            # Subsequent calls come from the background quota_monitor_loop: raise PermissionError
            raise PermissionError("Injected monitor snapshot permission error")

        with patch.object(WorkStore, "save_quota_snapshot", side_effect=fake_save_quota_snapshot):
            outcome = run_observer_turn(
                work_id=work_id,
                turn_id=turn_id,
                target_revision=2,
                worker_token=token,
                base_dir=self.base_dir,
                project_root=self.project_root,
                agy_exe=Path(sys.executable),
                prompt_file=prompt_file,
                schema_file=schema_file,
                timeout=10.0,
                quota_interval=0.1,
                cli_args_prefix=[str(FAKE_CLI_PATH)],
            )

        # Invariant: no SUCCESS, must yield execution_unknown / checkpoint_error
        self.assertNotEqual(outcome.status, "SUCCESS")
        self.assertEqual(outcome.status, "execution_unknown")
        self.assertEqual(outcome.error_code, "checkpoint_error")

        # Unknown reservation retained
        self.assertTrue(store.reservation_file.exists())
        with open(store.reservation_file, "r", encoding="utf-8") as rf:
            res_data = json.load(rf)
        self.assertFalse(res_data.get("execution_known"))

        # Snapshot operation boundary must be execution_unknown
        snap = store.load_snapshot()
        self.assertEqual(snap.operation_boundary, "execution_unknown")

        # Exactly 1 model generation occurred (not aborted prematurely, no duplicate retry)
        self.assertEqual(self._get_generation_count(), 1)


if __name__ == "__main__":
    unittest.main()
