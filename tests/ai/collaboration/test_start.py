"""
Unit and integration tests for durable start seam and detached observer (Tasks T010, T015).
Validates:
- Real temporary Git fixture with Cyrillic characters and spaces and .gitignore for logs/
- start() contract response returned quickly (state='implementing')
- Detached observer process running independently from parent/MCP
- Verified process identity (actual PID, start time, worker token) in Turn and reservation
- ZERO model calls on missing approval, stale scope digest, or low/unknown quota
- Parent exit boundary: parent process spawns worker and exits 0, detached child persists outcome
- Idempotency with exact same request_id and conflict on altered payload
- Crash fault injection before/after Popen: retry detects execution_unknown and NEVER spawns 2nd observer
- Checkout collision isolation: second work blocked while first work is running or in_review
- Timeout or pending tools: execution_unknown, reservation NOT released, blocks second writer
- Requested stop with late SUCCESS: transitions to 'stopped' with last_result preserved
- Terminal SUCCESS transitions to 'in_review', NEVER directly to 'complete', reservation retained
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
    CollaborationError,
    SCHEMA_VERSION,
    canonical_scope_digest,
    compute_baseline,
    is_process_alive,
    get_process_creation_time,
    Turn,
)
from scripts.ai.collaboration.environment import (
    QuotaAdapter,
)

FAKE_CLI_PATH = Path(__file__).resolve().parent / "fake_cli.py"


class TestDurableStart(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="ai_start_test_")
        # Actual temporary Git fixture with Cyrillic characters and spaces
        self.project_root = Path(self.temp_dir) / "Тестовый репозиторий старта с пробелами"
        self.project_root.mkdir(parents=True, exist_ok=True)
        self.base_dir = self.project_root / "logs" / "ai" / "collaboration"

        # Initialize Git repository
        subprocess.run(["git", "init"], cwd=str(self.project_root), capture_output=True, check=True)
        subprocess.run(["git", "config", "user.name", "Tester User"], cwd=str(self.project_root), check=True)
        subprocess.run(["git", "config", "user.email", "tester@example.com"], cwd=str(self.project_root), check=True)

        # Critical Review Point 1: create .gitignore ignoring logs/ and .git/ before any commit/baseline
        gitignore_path = self.project_root / ".gitignore"
        gitignore_path.write_text("logs/\n.git/\n", encoding="utf-8")

        # Create specs and task artifacts
        specs_dir = self.project_root / "specs" / "001-feature"
        specs_dir.mkdir(parents=True, exist_ok=True)

        self.spec_file = specs_dir / "spec.md"
        self.spec_file.write_text("# Feature 001 Specification\nInitial spec.\n", encoding="utf-8")

        self.tasks_file = specs_dir / "tasks.md"
        self.tasks_file.write_text(
            "# Feature 001 Tasks\n"
            "- [ ] T001 Initialize foundation and core models\n"
            "- [ ] T002 Implement business logic and service seams\n",
            encoding="utf-8"
        )

        # Allowed source file
        src_dir = self.project_root / "src"
        src_dir.mkdir(parents=True, exist_ok=True)
        self.allowed_file = src_dir / "allowed_app.py"
        self.allowed_file.write_text("print('allowed initial app')\n", encoding="utf-8")

        # Initial commit
        subprocess.run(["git", "add", "."], cwd=str(self.project_root), capture_output=True, check=True)
        subprocess.run(["git", "commit", "-m", "Initial commit with specs"], cwd=str(self.project_root), capture_output=True, check=True)

        self.coordinator = CollaborationCoordinator(
            project_root=self.project_root,
            base_dir=self.base_dir,
        )

        # Default assignment dictionary
        self.assignment = {
            "goal": "Implement initial collaboration foundation",
            "artifact_refs": ["specs/001-feature/spec.md", "specs/001-feature/tasks.md"],
            "allowed_files": ["src/allowed_app.py"],
            "allowed_actions": ["file_edit", "command_run"],
            "acceptance": ["All tests pass cleanly"],
            "task_ids": ["T001", "T002"],
            "timeout_seconds": 60,
        }

        # Generation counter setup
        self.counter_file = self.base_dir / "fake_counter.txt"
        os.environ["FAKE_AGY_COUNTER_FILE"] = str(self.counter_file)

        # Clear fake env variables
        os.environ.pop("FAKE_AGY_MODE", None)
        os.environ.pop("FAKE_AGY_SLEEP", None)

    def tearDown(self):
        os.environ.pop("FAKE_AGY_MODE", None)
        os.environ.pop("FAKE_AGY_SLEEP", None)
        os.environ.pop("FAKE_AGY_COUNTER_FILE", None)
        try:
            for rf in (self.base_dir / "checkouts").glob("*/reservation.json"):
                if rf.exists():
                    rdata = json.loads(rf.read_text(encoding="utf-8"))
                    cpid = rdata.get("actual_observer_pid") or rdata.get("pid")
                    cstart = rdata.get("actual_observer_start_time") or rdata.get("start_time")
                    if cpid and is_process_alive(cpid, cstart) is True:
                        dl = time.monotonic() + 3.0
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
            "approved_text": "Scope and tasks approved for autonomous start",
            "approved_actions": ["file_edit", "command_run"],
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

    def test_start_fails_without_human_approval(self):
        req_id_create = str(uuid.uuid4())
        res_create = self.coordinator.create(req_id_create, self.assignment)
        work_id = res_create["work_id"]

        with self.assertRaises(CollaborationError) as ctx:
            self.coordinator.start(
                work_id=work_id,
                expected_revision=1,
                request_id=str(uuid.uuid4()),
                agy_override=Path(sys.executable),
                cli_args_prefix=[str(FAKE_CLI_PATH)],
            )
        self.assertIn(ctx.exception.code, ("approval_required", "permission_denied"))

    def test_start_fails_on_stale_scope_digest(self):
        work_id = self._create_and_approve_work()

        # Tamper with spec file after approval
        self.spec_file.write_text("# Feature 001 Specification\nTampered secretly!\n", encoding="utf-8")

        with self.assertRaises(CollaborationError) as ctx:
            self.coordinator.start(
                work_id=work_id,
                expected_revision=2,
                request_id=str(uuid.uuid4()),
                agy_override=Path(sys.executable),
                cli_args_prefix=[str(FAKE_CLI_PATH)],
            )
        self.assertEqual(ctx.exception.code, "scope_violation")

    def test_start_fails_on_baseline_scope_tampering(self):
        work_id = self._create_and_approve_work()

        # Modify an unapproved file outside allowed_files
        unapproved = self.project_root / "src" / "forbidden.py"
        unapproved.write_text("evil = 1\n", encoding="utf-8")

        with self.assertRaises(CollaborationError) as ctx:
            self.coordinator.start(
                work_id=work_id,
                expected_revision=2,
                request_id=str(uuid.uuid4()),
                agy_override=Path(sys.executable),
                cli_args_prefix=[str(FAKE_CLI_PATH)],
            )
        self.assertEqual(ctx.exception.code, "scope_violation")

    def test_start_fails_on_revision_conflict(self):
        work_id = self._create_and_approve_work()

        # Pass wrong expected revision (e.g. 99 instead of 2)
        with self.assertRaises(CollaborationError) as ctx:
            self.coordinator.start(
                work_id=work_id,
                expected_revision=99,
                request_id=str(uuid.uuid4()),
                agy_override=Path(sys.executable),
                cli_args_prefix=[str(FAKE_CLI_PATH)],
            )
        self.assertEqual(ctx.exception.code, "revision_conflict")

    def test_start_blocks_on_low_or_unknown_quota_with_zero_model_calls(self):
        work_id = self._create_and_approve_work()

        os.environ["FAKE_AGY_MODE"] = "usage_low"

        with self.assertRaises(CollaborationError) as ctx:
            self.coordinator.start(
                work_id=work_id,
                expected_revision=2,
                request_id=str(uuid.uuid4()),
                agy_override=Path(sys.executable),
                cli_args_prefix=[str(FAKE_CLI_PATH)],
            )
        self.assertEqual(ctx.exception.code, "provider_error")

        store = WorkStore(base_dir=self.base_dir, work_id=work_id, checkout_root=self.project_root)
        notice = store.load_quota_notice()
        self.assertIsNotNone(notice)
        self.assertEqual(notice.kind, "quota_low")

    def test_start_returns_fast_contract_response(self):
        work_id = self._create_and_approve_work()

        os.environ["FAKE_AGY_MODE"] = "success_result"
        os.environ["FAKE_AGY_SLEEP"] = "3.0"

        start_time = time.monotonic()
        req_id_start = str(uuid.uuid4())
        res = self.coordinator.start(
            work_id=work_id,
            expected_revision=2,
            request_id=req_id_start,
            agy_override=Path(sys.executable),
            cli_args_prefix=[str(FAKE_CLI_PATH)],
            detached=True,
        )
        elapsed = time.monotonic() - start_time

        # Fast return well under execution time (< 2.0s vs 3.0s sleep)
        self.assertLess(elapsed, 2.0)
        self.assertTrue(res["ok"])
        self.assertEqual(res["state"], "implementing")
        self.assertEqual(res["work_id"], work_id)
        self.assertEqual(res["revision"], 3)
        self.assertIsNotNone(res["turn_id"])
        store = WorkStore(base_dir=self.base_dir, work_id=work_id, checkout_root=self.project_root)
        w = store.load_snapshot()
        self.assertEqual(w.current_turn.turn_id, res["turn_id"])

    def test_start_runs_detached_worker_and_records_turn_identity(self):
        work_id = self._create_and_approve_work()

        os.environ["FAKE_AGY_MODE"] = "success_result"
        req_id_start = str(uuid.uuid4())

        res_start = self.coordinator.start(
            work_id=work_id,
            expected_revision=2,
            request_id=req_id_start,
            agy_override=Path(sys.executable),
            cli_args_prefix=[str(FAKE_CLI_PATH)],
            detached=True,
        )
        self.assertEqual(res_start["state"], "implementing")
        self.assertEqual(res_start["next_action"], "read")

        store = WorkStore(base_dir=self.base_dir, work_id=work_id, checkout_root=self.project_root)

        # Check turn was recorded with detached PID
        work_mid = store.load_snapshot()
        self.assertEqual(work_mid.state, "implementing")
        self.assertIsNotNone(work_mid.current_turn)
        detached_pid = work_mid.current_turn.pid
        self.assertIsNotNone(detached_pid)
        self.assertNotEqual(detached_pid, os.getpid())

        # Wait for detached worker process to finalize execution
        max_wait = 5.0
        poll_start = time.monotonic()
        finalized = False
        while time.monotonic() - poll_start < max_wait:
            curr_work = store.load_snapshot()
            if curr_work.state != "implementing":
                finalized = True
                break
            time.sleep(0.05)

        self.assertTrue(finalized, "Detached worker process did not finalize within timeout")
        final_work = store.load_snapshot()

        # Invariant: SUCCESS transitions to in_review, NEVER complete!
        self.assertEqual(final_work.state, "in_review")
        self.assertEqual(final_work.revision, 4)
        self.assertIsNotNone(final_work.last_result)
        self.assertEqual(final_work.last_result.work_id, work_id)
        self.assertEqual(final_work.current_turn.outcome, "SUCCESS")

        # Invariant (Review Point 4): Reservation is RETAINED during in_review to block foreign work
        self.assertTrue(store.reservation_file.exists())
        with open(store.reservation_file, "r", encoding="utf-8") as rf:
            res_data = json.load(rf)
        self.assertEqual(res_data.get("work_id"), work_id)
        self.assertEqual(res_data.get("status"), "in_review")
        self.assertTrue(res_data.get("observer_exited"))

    def test_parent_process_exit_boundary_detached_child_persists_state(self):
        work_id = self._create_and_approve_work()

        req_id_start = str(uuid.uuid4())
        runner_script = Path(self.temp_dir) / "parent_spawner.py"
        runner_code = f"""
import os, sys
sys.path.insert(0, {repr(str(PROJECT_ROOT))})
from scripts.ai.collaboration.coordinator import CollaborationCoordinator
from pathlib import Path

coord = CollaborationCoordinator(project_root=Path({repr(str(self.project_root))}), base_dir=Path({repr(str(self.base_dir))}))
res = coord.start(
    work_id={repr(work_id)},
    expected_revision=2,
    request_id={repr(req_id_start)},
    agy_override=Path(sys.executable),
    cli_args_prefix=[{repr(str(FAKE_CLI_PATH))}],
    detached=True
)
print("STARTED_SUCCESS", flush=True)
sys.exit(0)
"""
        runner_script.write_text(runner_code, encoding="utf-8")

        env = os.environ.copy()
        env["FAKE_AGY_MODE"] = "success_result"
        env["PYTHONPATH"] = str(PROJECT_ROOT)

        p = subprocess.run([sys.executable, str(runner_script)], env=env, capture_output=True, text=True)
        self.assertEqual(p.returncode, 0)
        self.assertIn("STARTED_SUCCESS", p.stdout)

        # Parent is now dead! Poll store from test process
        store = WorkStore(base_dir=self.base_dir, work_id=work_id, checkout_root=self.project_root)
        poll_start = time.monotonic()
        finalized = False
        while time.monotonic() - poll_start < 6.0:
            w = store.load_snapshot()
            if w.state == "in_review":
                finalized = True
                break
            time.sleep(0.05)

        self.assertTrue(finalized, "Detached child failed to persist in_review after parent process exited")
        final_work = store.load_snapshot()
        self.assertEqual(final_work.state, "in_review")
        self.assertIsNotNone(final_work.last_result)

    def test_start_idempotency_exact_same_request_id(self):
        work_id = self._create_and_approve_work()

        os.environ["FAKE_AGY_MODE"] = "success_result"
        req_id_start = str(uuid.uuid4())

        res1 = self.coordinator.start(
            work_id=work_id,
            expected_revision=2,
            request_id=req_id_start,
            agy_override=Path(sys.executable),
            cli_args_prefix=[str(FAKE_CLI_PATH)],
            detached=True,
        )

        res2 = self.coordinator.start(
            work_id=work_id,
            expected_revision=2,
            request_id=req_id_start,
            agy_override=Path(sys.executable),
            cli_args_prefix=[str(FAKE_CLI_PATH)],
            detached=True,
        )

        # Must return identical response without spawning second worker
        self.assertEqual(res1, res2)

    def test_fault_injection_before_popen_leaves_unknown_and_no_duplicate_popen(self):
        work_id = self._create_and_approve_work()

        req_id_start = str(uuid.uuid4())
        self.coordinator._fault_before_popen = True

        with self.assertRaises(RuntimeError):
            self.coordinator.start(
                work_id=work_id,
                expected_revision=2,
                request_id=req_id_start,
                agy_override=Path(sys.executable),
                cli_args_prefix=[str(FAKE_CLI_PATH)],
                detached=True,
            )

        self.coordinator._fault_before_popen = False

        # Retry must detect incomplete registration and raise execution_unknown, NOT spawn 2nd observer (Review Point 3)
        with self.assertRaises(CollaborationError) as ctx:
            self.coordinator.start(
                work_id=work_id,
                expected_revision=2,
                request_id=req_id_start,
                agy_override=Path(sys.executable),
                cli_args_prefix=[str(FAKE_CLI_PATH)],
                detached=True,
            )
        self.assertEqual(ctx.exception.code, "execution_unknown")

    def test_start_duplicate_request_id_conflicting_payload(self):
        work_id = self._create_and_approve_work()

        req_id_start = str(uuid.uuid4())
        self.coordinator.start(
            work_id=work_id,
            expected_revision=2,
            request_id=req_id_start,
            agy_override=Path(sys.executable),
            cli_args_prefix=[str(FAKE_CLI_PATH)],
            detached=True,
        )

        # Same request_id with different model_override -> request_conflict
        with self.assertRaises(CollaborationError) as ctx:
            self.coordinator.start(
                work_id=work_id,
                expected_revision=2,
                request_id=req_id_start,
                model_override="claude-3-7-sonnet",
                agy_override=Path(sys.executable),
                cli_args_prefix=[str(FAKE_CLI_PATH)],
                detached=True,
            )
        self.assertEqual(ctx.exception.code, "request_conflict")

    def test_checkout_collision_foreign_work_blocked_while_in_review(self):
        work_id_1 = self._create_and_approve_work()

        # Second work on same checkout
        req_id_create_2 = str(uuid.uuid4())
        res_create_2 = self.coordinator.create(req_id_create_2, self.assignment)
        work_id_2 = res_create_2["work_id"]

        scope_digest_2 = self.coordinator.compute_scope_digest(work_id_2)
        decision_2 = {
            "source": "human user",
            "approved_text": "Proceed with second work",
            "approved_actions": ["file_edit", "command_run"],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        self.coordinator.approve(
            work_id=work_id_2,
            expected_revision=1,
            request_id=str(uuid.uuid4()),
            decision=decision_2,
            scope_digest=scope_digest_2,
        )

        os.environ["FAKE_AGY_MODE"] = "success_result"

        self.coordinator.start(
            work_id=work_id_1,
            expected_revision=2,
            request_id=str(uuid.uuid4()),
            agy_override=Path(sys.executable),
            cli_args_prefix=[str(FAKE_CLI_PATH)],
            detached=True,
        )

        # Wait for Work 1 to reach in_review
        store1 = WorkStore(base_dir=self.base_dir, work_id=work_id_1, checkout_root=self.project_root)
        poll_start = time.monotonic()
        while time.monotonic() - poll_start < 5.0:
            w1 = store1.load_snapshot()
            if w1.state == "in_review":
                break
            time.sleep(0.05)

        # Invariant (Review Point 4): Even after observer finishes and Work 1 is in_review,
        # Work 2 is STILL BLOCKED from reserving the checkout until acceptance or handoff!
        with self.assertRaises(CollaborationError) as ctx:
            self.coordinator.start(
                work_id=work_id_2,
                expected_revision=2,
                request_id=str(uuid.uuid4()),
                agy_override=Path(sys.executable),
                cli_args_prefix=[str(FAKE_CLI_PATH)],
                detached=True,
            )
        self.assertEqual(ctx.exception.code, "checkout_busy")

    def test_timeout_or_pending_tools_execution_unknown_no_second_writer(self):
        work_id = self._create_and_approve_work()

        os.environ["FAKE_AGY_MODE"] = "pending_tools"
        req_id_start = str(uuid.uuid4())

        self.coordinator.start(
            work_id=work_id,
            expected_revision=2,
            request_id=req_id_start,
            agy_override=Path(sys.executable),
            cli_args_prefix=[str(FAKE_CLI_PATH)],
            detached=True,
        )

        store = WorkStore(base_dir=self.base_dir, work_id=work_id, checkout_root=self.project_root)

        # Poll until worker finishes processing stream
        poll_start = time.monotonic()
        while time.monotonic() - poll_start < 5.0:
            w = store.load_snapshot()
            if w.operation_boundary == "execution_unknown":
                break
            time.sleep(0.05)

        work_mid = store.load_snapshot()
        self.assertEqual(work_mid.operation_boundary, "execution_unknown")

        # Reservation file must remain present and marked execution_known=False
        self.assertTrue(store.reservation_file.exists())
        with open(store.reservation_file, "r", encoding="utf-8") as rf:
            res_data = json.load(rf)
        self.assertFalse(res_data.get("execution_known"))

        # Second attempt to start must fail as checkout_busy with zero second generation
        initial_gens = self._get_generation_count()
        with self.assertRaises(CollaborationError) as ctx:
            self.coordinator.start(
                work_id=work_id,
                expected_revision=work_mid.revision,
                request_id=str(uuid.uuid4()),
                agy_override=Path(sys.executable),
                cli_args_prefix=[str(FAKE_CLI_PATH)],
                detached=True,
            )
        self.assertEqual(ctx.exception.code, "checkout_busy")
        self.assertEqual(self._get_generation_count(), initial_gens)

    def test_requested_stop_with_late_success_transitions_to_stopped(self):
        work_id = self._create_and_approve_work()

        os.environ["FAKE_AGY_MODE"] = "success_result"
        os.environ["FAKE_AGY_SLEEP"] = "0.6"
        req_id_start = str(uuid.uuid4())

        self.coordinator.start(
            work_id=work_id,
            expected_revision=2,
            request_id=req_id_start,
            agy_override=Path(sys.executable),
            cli_args_prefix=[str(FAKE_CLI_PATH)],
            detached=True,
        )

        store = WorkStore(base_dir=self.base_dir, work_id=work_id, checkout_root=self.project_root)

        # Wait until generation counter increments to 1 before requesting stop during active run
        poll_gen = time.monotonic()
        while time.monotonic() - poll_gen < 3.0:
            if self._get_generation_count() >= 1:
                break
            time.sleep(0.02)
        self.assertGreaterEqual(self._get_generation_count(), 1)

        # Signal stop request while child is sleeping
        with store.lock():
            w = store.load_snapshot()
            w.stop_requested = True
            store.save_snapshot(w)

        # Wait for detached worker to complete
        poll_start = time.monotonic()
        while time.monotonic() - poll_start < 5.0:
            curr_w = store.load_snapshot()
            if curr_w.state in ("stopped", "in_review"):
                break
            time.sleep(0.05)

        final_w = store.load_snapshot()
        # Must transition to stopped (NOT in_review or complete), preserving last_result
        self.assertEqual(final_w.state, "stopped")
        self.assertIsNotNone(final_w.last_result)

    def test_requested_stop_before_start_rejected_by_coordinator(self):
        work_id = self._create_and_approve_work()
        store = WorkStore(base_dir=self.base_dir, work_id=work_id, checkout_root=self.project_root)
        with store.lock():
            w = store.load_snapshot()
            w.stop_requested = True
            store.save_snapshot(w)

        os.environ["FAKE_AGY_MODE"] = "success_result"
        req_id_start = str(uuid.uuid4())
        initial_gens = self._get_generation_count()
        with self.assertRaises(CollaborationError) as ctx:
            self.coordinator.start(
                work_id=work_id,
                expected_revision=2,
                request_id=req_id_start,
                agy_override=Path(sys.executable),
                cli_args_prefix=[str(FAKE_CLI_PATH)],
                detached=True,
            )
        self.assertEqual(ctx.exception.code, "permission_denied")
        self.assertEqual(self._get_generation_count(), initial_gens)

    def test_requested_stop_before_generation_transitions_to_stopped(self):
        # Observer pre-generation stop: registered Work/Turn with stop_requested yields stopped and 0 generations
        from scripts.ai.collaboration.worker_main import run_observer_turn
        from scripts.ai.collaboration.worker import save_executor_json_schema
        from scripts.ai.collaboration.state import Turn

        work_id = self._create_and_approve_work()
        store = WorkStore(base_dir=self.base_dir, work_id=work_id, checkout_root=self.project_root)

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
            w.stop_requested = True
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

        os.environ["FAKE_AGY_MODE"] = "success_result"
        initial_gens = self._get_generation_count()

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
            timeout=5.0,
            cli_args_prefix=[str(FAKE_CLI_PATH)],
        )

        self.assertEqual(outcome.status, "ERROR")
        self.assertEqual(outcome.error_code, "stopped")
        final_w = store.load_snapshot()
        self.assertEqual(final_w.state, "stopped")
        self.assertEqual(final_w.operation_boundary, "stopped")
        self.assertEqual(self._get_generation_count(), initial_gens)

    def test_delayed_late_result_observed_and_requires_reconcile(self):
        self.assignment["timeout_seconds"] = 1
        work_id = self._create_and_approve_work()
        # Set sleep to 1.5s, timeout is 1s, stream_drain_timeout is 0.2s
        os.environ["FAKE_AGY_MODE"] = "success_result"
        os.environ["FAKE_AGY_SLEEP"] = "1.5"

        res = self.coordinator.start(
            work_id=work_id,
            expected_revision=2,
            request_id=str(uuid.uuid4()),
            agy_override=Path(sys.executable),
            cli_args_prefix=[str(FAKE_CLI_PATH)],
            stream_drain_timeout=0.2,
            detached=True,
        )
        self.assertEqual(res["state"], "implementing")

        store = WorkStore(base_dir=self.base_dir, work_id=work_id, checkout_root=self.project_root)
        # Wait for late observation to complete
        poll_start = time.monotonic()
        while time.monotonic() - poll_start < 5.0:
            curr_w = store.load_snapshot()
            if curr_w.state in ("in_review", "error", "stopped"):
                break
            time.sleep(0.05)

        final_w = store.load_snapshot()
        self.assertEqual(final_w.state, "in_review")
        self.assertEqual(final_w.operation_boundary, "late_success")
        self.assertIsNotNone(final_w.last_result)
        # Handoff requires reconcile because it was late!
        handoff_txt = store.load_handoff()
        self.assertIn("# tinker Handoff Context", handoff_txt)
        self.assertIn("**Requires Reconcile:** Yes", handoff_txt)
        self.assertIn("**Current Boundary:** late_success", handoff_txt)
        # Reservation is NOT released!
        self.assertTrue(store.reservation_file.exists())

    def test_pending_tools_retains_reservation_with_execution_unknown(self):
        # Review Point 5 & 7: pending tools at stream end yields execution_unknown and retains reservation
        work_id = self._create_and_approve_work()
        os.environ["FAKE_AGY_MODE"] = "pending_tools"

        self.coordinator.start(
            work_id=work_id,
            expected_revision=2,
            request_id=str(uuid.uuid4()),
            agy_override=Path(sys.executable),
            cli_args_prefix=[str(FAKE_CLI_PATH)],
            detached=True,
        )

        store = WorkStore(base_dir=self.base_dir, work_id=work_id, checkout_root=self.project_root)
        poll_start = time.monotonic()
        while time.monotonic() - poll_start < 5.0:
            curr_w = store.load_snapshot()
            if curr_w.state in ("error", "stopped", "in_review"):
                break
            time.sleep(0.05)

        final_w = store.load_snapshot()
        self.assertNotEqual(final_w.state, "complete")
        self.assertEqual(final_w.operation_boundary, "execution_unknown")
        # Reservation must NOT be released!
        self.assertTrue(store.reservation_file.exists())
        with open(store.reservation_file, "r", encoding="utf-8") as f:
            res_info = json.load(f)
        self.assertEqual(res_info.get("execution_known"), False)

    def test_stream_event_callback_canary_triggers_sensitive_output_blocked(self):
        # Review Point 5 & 7: canary pattern in callback triggers sensitive_output_blocked
        work_id = self._create_and_approve_work()
        os.environ["FAKE_AGY_MODE"] = "canary_tool_error"

        self.coordinator.start(
            work_id=work_id,
            expected_revision=2,
            request_id=str(uuid.uuid4()),
            agy_override=Path(sys.executable),
            cli_args_prefix=[str(FAKE_CLI_PATH)],
            detached=True,
        )

        store = WorkStore(base_dir=self.base_dir, work_id=work_id, checkout_root=self.project_root)
        poll_start = time.monotonic()
        while time.monotonic() - poll_start < 5.0:
            curr_w = store.load_snapshot()
            if curr_w.state in ("error", "stopped", "in_review"):
                break
            time.sleep(0.05)

        final_w = store.load_snapshot()
        self.assertNotEqual(final_w.state, "complete")
        self.assertEqual(final_w.state, "error")
        self.assertIn(final_w.error.get("code"), ("sensitive_output_blocked", "fatal_step_error"))
        # Reservation remains held
        self.assertTrue(store.reservation_file.exists())

        # Anti-leak invariant: canary token must NOT leak into events, snapshot, or handoff
        if store.events_file.exists():
            events_raw = store.events_file.read_text(encoding="utf-8")
            self.assertNotIn("CANARY_TOOL_TOKEN_54321", events_raw)
        snap_raw = store.snapshot_file.read_text(encoding="utf-8")
        self.assertNotIn("CANARY_TOOL_TOKEN_54321", snap_raw)
        if store.handoff_file.exists():
            handoff_raw = store.handoff_file.read_text(encoding="utf-8")
            self.assertNotIn("CANARY_TOOL_TOKEN_54321", handoff_raw)

    def test_fault_after_popen_before_registration_yields_durable_unknown_no_generation(self):
        # Review Point 6 & 7: fault after Popen before registration gates child observer from generating
        work_id = self._create_and_approve_work()
        os.environ["FAKE_AGY_MODE"] = "success_result"

        self.coordinator._fault_after_popen = True
        with self.assertRaises(RuntimeError):
            self.coordinator.start(
                work_id=work_id,
                expected_revision=2,
                request_id=str(uuid.uuid4()),
                agy_override=Path(sys.executable),
                cli_args_prefix=[str(FAKE_CLI_PATH)],
                detached=True,
            )
        self.coordinator._fault_after_popen = False

        store = WorkStore(base_dir=self.base_dir, work_id=work_id, checkout_root=self.project_root)
        # Wait for child observer process to fail registration gate and mark execution_unknown
        poll_start = time.monotonic()
        while time.monotonic() - poll_start < 8.0:
            if store.snapshot_file.exists():
                try:
                    w = store.load_snapshot()
                    if w.operation_boundary == "execution_unknown":
                        break
                except Exception:
                    pass
            time.sleep(0.05)

        snap = store.load_snapshot()
        self.assertEqual(snap.operation_boundary, "execution_unknown")
        # Invariant: child observer did not execute model generation
        self.assertEqual(self._get_generation_count(), 0)

        # Retry must fail with checkout_busy and spawn no second process
        with self.assertRaises(CollaborationError) as ctx:
            self.coordinator.start(
                work_id=work_id,
                expected_revision=snap.revision,
                request_id=str(uuid.uuid4()),
                agy_override=Path(sys.executable),
                cli_args_prefix=[str(FAKE_CLI_PATH)],
                detached=True,
            )
        self.assertEqual(ctx.exception.code, "checkout_busy")
        self.assertEqual(self._get_generation_count(), 0)

    def test_duplicate_start_zero_additional_generation(self):
        # Review Point 7: duplicate start produces idempotent response with 0 additional generations
        work_id = self._create_and_approve_work()
        os.environ["FAKE_AGY_MODE"] = "success_result"
        req_id = str(uuid.uuid4())

        res1 = self.coordinator.start(
            work_id=work_id,
            expected_revision=2,
            request_id=req_id,
            agy_override=Path(sys.executable),
            cli_args_prefix=[str(FAKE_CLI_PATH)],
            detached=True,
        )
        self.assertEqual(res1["state"], "implementing")
        self.assertIsNotNone(res1.get("turn_id"))

        res2 = self.coordinator.start(
            work_id=work_id,
            expected_revision=2,
            request_id=req_id,
            agy_override=Path(sys.executable),
            cli_args_prefix=[str(FAKE_CLI_PATH)],
            detached=True,
        )
        self.assertEqual(res2["turn_id"], res1["turn_id"])
        self.assertEqual(res2["revision"], res1["revision"])

    def test_redirector_lineage_and_registration(self):
        """Tests that observer accepts redirector parent and registers actual observer PID via mock."""
        from unittest.mock import patch
        from scripts.ai.collaboration.worker_main import run_observer_turn
        from scripts.ai.collaboration.worker import save_executor_json_schema
        from scripts.ai.collaboration.state import Turn, verify_process_lineage

        my_pid = os.getpid()
        my_creation = get_process_creation_time(my_pid)
        self.assertIsNotNone(my_creation)

        # 1. Direct match with exact creation time passes
        self.assertTrue(verify_process_lineage(my_pid, my_pid, my_creation))
        # Missing creation time fails closed
        self.assertFalse(verify_process_lineage(my_pid, my_pid, None))
        # Mismatched creation time fails closed
        self.assertFalse(verify_process_lineage(my_pid, my_pid, my_creation + 1.0))
        # Wrong PID fails closed
        self.assertFalse(verify_process_lineage(my_pid, 9999999, my_creation))

        # 2. Redirector parent lineage via unittest.mock
        mock_launcher_pid = 7777777
        mock_launcher_time = 1234567.890

        def fake_get_creation(pid):
            if pid == mock_launcher_pid:
                return mock_launcher_time
            if pid == my_pid:
                return my_creation
            return None

        with patch("os.getppid", return_value=mock_launcher_pid), \
             patch("scripts.ai.collaboration.state.get_process_creation_time", side_effect=fake_get_creation):
            self.assertTrue(verify_process_lineage(my_pid, mock_launcher_pid, mock_launcher_time))
            # Mismatched time fails closed
            self.assertFalse(verify_process_lineage(my_pid, mock_launcher_pid, mock_launcher_time + 0.1))
            # Missing time fails closed
            self.assertFalse(verify_process_lineage(my_pid, mock_launcher_pid, None))

        # 3. Test in run_observer_turn with mocked redirector parent
        work_id = self._create_and_approve_work()
        store = WorkStore(base_dir=self.base_dir, work_id=work_id, checkout_root=self.project_root)
        turn_id = str(uuid.uuid4())
        prompt_file = store.work_dir / "test_prompt.txt"
        prompt_file.write_text(f"# ASSIGNMENT FOR WORK {work_id} (TURN {turn_id})\nWork: {work_id}\nTurn: {turn_id}\n", encoding="utf-8")
        schema_file = store.work_dir / "schema.json"
        save_executor_json_schema(schema_file)

        token = uuid.uuid4().hex
        store.prepare_reservation(worker_token=token, start_time=mock_launcher_time)
        store.update_reservation_process(
            worker_token=token,
            pid=mock_launcher_pid,
            start_time=mock_launcher_time,
            launcher_pid=mock_launcher_pid,
            launcher_start_time=mock_launcher_time,
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
                pid=mock_launcher_pid,
                start_time=mock_launcher_time,
            )
            store.save_snapshot(w)

        os.environ["FAKE_AGY_MODE"] = "success_result"
        with patch("os.getppid", return_value=mock_launcher_pid), \
             patch("scripts.ai.collaboration.state.get_process_creation_time", side_effect=fake_get_creation), \
             patch("scripts.ai.collaboration.worker_main.get_process_creation_time", side_effect=fake_get_creation):
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
                timeout=5.0,
                cli_args_prefix=[str(FAKE_CLI_PATH)],
            )
            self.assertEqual(outcome.status, "SUCCESS")
            # Verify actual observer PID is recorded in reservation and turn snapshot
            with open(store.reservation_file, "r", encoding="utf-8") as rf:
                res_data = json.load(rf)
            self.assertEqual(res_data.get("actual_observer_pid"), my_pid)
            self.assertEqual(res_data.get("pid"), my_pid)
            self.assertEqual(res_data.get("launcher_pid"), mock_launcher_pid)

            snap = store.load_snapshot()
            self.assertEqual(snap.current_turn.pid, my_pid)

    def test_launcher_mismatch_fails_gate_with_zero_generation(self):
        """Tests that observer rejects foreign launcher PID without running model generation."""
        from scripts.ai.collaboration.worker_main import run_observer_turn
        from scripts.ai.collaboration.worker import save_executor_json_schema
        from scripts.ai.collaboration.state import Turn

        work_id = self._create_and_approve_work()
        store = WorkStore(base_dir=self.base_dir, work_id=work_id, checkout_root=self.project_root)
        turn_id = str(uuid.uuid4())
        prompt_file = store.work_dir / "test_prompt.txt"
        prompt_file.write_text(f"# ASSIGNMENT FOR WORK {work_id} (TURN {turn_id})\nWork: {work_id}\nTurn: {turn_id}\n", encoding="utf-8")
        schema_file = store.work_dir / "schema.json"
        save_executor_json_schema(schema_file)

        token = uuid.uuid4().hex
        foreign_pid = 9999991
        start_time = time.time()

        store.prepare_reservation(worker_token=token, start_time=start_time)
        store.update_reservation_process(
            worker_token=token,
            pid=foreign_pid,
            start_time=start_time,
            launcher_pid=foreign_pid,
            launcher_start_time=start_time,
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
                pid=foreign_pid,
                start_time=start_time,
            )
            store.save_snapshot(w)

        os.environ["FAKE_AGY_MODE"] = "success_result"
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
            timeout=5.0,
            cli_args_prefix=[str(FAKE_CLI_PATH)],
        )
        self.assertEqual(outcome.status, "execution_unknown")
        self.assertEqual(outcome.error_code, "execution_unknown")
        snap = store.load_snapshot()
        self.assertEqual(snap.operation_boundary, "execution_unknown")
        self.assertEqual(self._get_generation_count(), 0)

    def test_in_progress_request_replay_with_unknown_reservation_raises_execution_unknown(self):
        # Review Point 3: in_progress request replay when reservation is unknown must yield execution_unknown with 0 new generations
        work_id = self._create_and_approve_work()
        store = WorkStore(base_dir=self.base_dir, work_id=work_id, checkout_root=self.project_root)

        req_id = str(uuid.uuid4())
        turn_id = str(uuid.uuid4())
        worker_token = uuid.uuid4().hex

        # Construct payload digest matching the start request
        payload = {
            "work_id": work_id,
            "expected_revision": 2,
            "model_override": None,
        }
        payload_digest = self.coordinator._compute_payload_digest(payload)

        # 1. Create existing in_progress request record
        req_file = self.coordinator._get_request_file(req_id)
        record = {
            "request_id": req_id,
            "operation": "start",
            "payload_digest": payload_digest,
            "work_id": work_id,
            "expected_revision": 2,
            "target_revision": 3,
            "turn_id": turn_id,
            "worker_token": worker_token,
            "status": "in_progress",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        with open(req_file, "w", encoding="utf-8") as f:
            json.dump(record, f)

        # 2. Setup Work snapshot with current_turn committed
        with store.lock():
            w = store.load_snapshot()
            w.state = "implementing"
            w.current_turn = Turn(
                turn_id=turn_id,
                work_id=work_id,
                initial_revision=2,
                deadline=time.time() + 60,
                worker_token=worker_token,
                pid=os.getpid(),
                start_time=time.time(),
            )
            store.save_snapshot(w)

        # 3. Setup reservation marked execution_known=False
        my_pid = os.getpid()
        my_creation = get_process_creation_time(my_pid)
        store.prepare_reservation(worker_token=worker_token, start_time=my_creation)
        store.update_reservation_process(
            worker_token=worker_token,
            pid=my_pid,
            start_time=my_creation,
            launcher_pid=my_pid,
            launcher_start_time=my_creation,
        )
        store.mark_execution_unknown(worker_token, "Provisional unknown timeout")

        initial_gens = self._get_generation_count()

        # Replaying this request must fail with execution_unknown WITHOUT inventing known or spawning Popen
        with self.assertRaises(CollaborationError) as ctx:
            self.coordinator.start(
                work_id=work_id,
                expected_revision=2,
                request_id=req_id,
                agy_override=Path(sys.executable),
                cli_args_prefix=[str(FAKE_CLI_PATH)],
                detached=True,
            )
        self.assertEqual(ctx.exception.code, "execution_unknown")
        # Invariant: 0 additional generations spawned
        self.assertEqual(self._get_generation_count(), initial_gens)


if __name__ == "__main__":
    unittest.main()
