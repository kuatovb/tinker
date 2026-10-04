"""
Unit tests for collaboration state models, durable storage, baseline integrity,
shared checkout reservation, CAS ownership, and secrets blocking (Feature 007, Tasks T004-T007).
Includes regression coverage for:
- Git scope integrity on deletions, renames, UTF-8 paths, mode changes
- Snapshot & event coupled mutation protocol, revision divergence rejection, recovery
- Reservation CAS ownership, same-work idempotency, unknown execution lock guards
- Secret canary non-leakage in UUID/unknown key errors and handoff markdown
- Strict data model invariants (ExecutorResult nullable fields, Codex reviewer, types)
"""

import os
import sys
import json
import uuid
import time
import shutil
import tempfile
import threading
import subprocess
import unittest
import unittest.mock
from pathlib import Path

# Add project root to sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.ai.collaboration.state import (
    Work,
    Turn,
    CheckEvidence,
    ExecutorResult,
    Question,
    BlockReason,
    Review,
    Handoff,
    WorkStore,
    CollaborationError,
    canonical_scope_digest,
    compute_baseline,
    verify_scope_integrity,
    compute_file_fingerprint,
    is_valid_uuid,
    validate_uuid,
    validate_strict_keys,
    is_secret_path,
    detect_sensitive_content,
    sanitize_safe_output,
    SCHEMA_VERSION,
)


class TestCollaborationState(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.checkout_dir = Path(self.temp_dir) / "test_checkout"
        self.checkout_dir.mkdir()
        self.work_id = str(uuid.uuid4())
        self.turn_id = str(uuid.uuid4())
        self.base_dir = Path(self.temp_dir) / "logs"

        # Initialize Git repo in checkout_dir for baseline tests
        subprocess.run(["git", "init"], cwd=str(self.checkout_dir), capture_output=True, check=True)
        subprocess.run(["git", "config", "user.name", "TestUser"], cwd=str(self.checkout_dir), check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(self.checkout_dir), check=True)

        self.store = WorkStore(
            base_dir=self.base_dir,
            work_id=self.work_id,
            checkout_root=self.checkout_dir
        )

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _create_sample_work(self, revision=1):
        return Work(
            work_id=self.work_id,
            goal="Test goal",
            artifact_refs=["spec.md"],
            allowed_files=["allowed.py"],
            allowed_actions=["file_edit"],
            acceptance=["Tests pass"],
            task_ids=["T001"],
            revision=revision,
            schema_version=1,
            state="implementing",
        )

    # -------------------------------------------------------------------------
    # 1. UUID & Secret Canary Sanitization Tests (Requirement 4)
    # -------------------------------------------------------------------------
    def test_uuid_validation_and_no_canary_reflection(self):
        self.assertTrue(is_valid_uuid(self.work_id))
        self.assertFalse(is_valid_uuid("not-a-uuid"))
        self.assertFalse(is_valid_uuid(12345))
        self.assertFalse(is_valid_uuid(None))
        self.assertFalse(is_valid_uuid(True))

        canary = "CANARY_SECRET_UUID_99999"
        with self.assertRaises(CollaborationError) as ctx:
            validate_uuid(canary, "test_field")
        self.assertEqual(ctx.exception.code, "protocol_error")
        # Ensure the secret string is NOT echoed back in the error message or details
        self.assertNotIn(canary, ctx.exception.message)
        self.assertNotIn(canary, str(ctx.exception.to_dict()))

    def test_unknown_keys_no_canary_reflection(self):
        canary = "CANARY_SECRET_KEY_12345"
        with self.assertRaises(CollaborationError) as ctx:
            validate_strict_keys({canary: "value"}, {"allowed_field"}, "TestEntity")
        self.assertNotIn(canary, ctx.exception.message)
        self.assertNotIn(canary, str(ctx.exception.to_dict()))

    def test_handoff_markdown_sanitizes_tasks_and_blocks_secrets(self):
        canary = "CANARY_SECRET_TASK_98765"
        with self.assertRaises(CollaborationError) as ctx:
            Handoff(
                work_id=self.work_id,
                revision=1,
                goal="Valid goal",
                relative_artifacts=["spec.md"],
                completed_tasks=[canary],
                remaining_tasks=["Normal task"],
                safe_check_summaries=["Passed"],
            )
        self.assertEqual(ctx.exception.code, "sensitive_output_blocked")

    # -------------------------------------------------------------------------
    # 2. Strict Types & Model Invariants (Requirement 5)
    # -------------------------------------------------------------------------
    def test_strict_schema_no_type_coercion(self):
        with self.assertRaises(CollaborationError) as ctx:
            Work(
                work_id=self.work_id,
                goal="Valid goal",
                artifact_refs=[],
                allowed_files=["file.py"],
                allowed_actions=["file_edit"],
                acceptance=["Tests pass"],
                task_ids=["T001"],
                schema_version=True  # bool is forbidden
            )
        self.assertEqual(ctx.exception.code, "protocol_error")

    def test_executor_result_requires_both_question_and_block_reason(self):
        # 1. Missing 'question' or 'block_reason' in dict raises protocol_error
        raw = {
            "schema_version": 1,
            "work_id": self.work_id,
            "turn_id": self.turn_id,
            "kind": "implementation_result",
            "summary": "Done",
            "claimed_files": [],
            "checks": [],
            "remaining_actions": [],
            # question and block_reason missing!
        }
        with self.assertRaises(CollaborationError) as ctx:
            ExecutorResult.from_dict(raw)
        self.assertEqual(ctx.exception.code, "protocol_error")

        # 2. Both present as null -> valid implementation_result
        raw["question"] = None
        raw["block_reason"] = None
        res = ExecutorResult.from_dict(raw)
        self.assertEqual(res.kind, "implementation_result")
        self.assertIsNone(res.question)
        self.assertIsNone(res.block_reason)

        # 3. Kind 'question' requires question object and block_reason=None
        raw_q = dict(raw)
        raw_q["kind"] = "question"
        with self.assertRaises(CollaborationError):
            ExecutorResult.from_dict(raw_q)

        raw_q["question"] = {"question_id": "Q1", "text": "Need clarification", "decision_kind": "technical"}
        res_q = ExecutorResult.from_dict(raw_q)
        self.assertIsNotNone(res_q.question)

    def test_review_reviewer_must_be_strictly_codex(self):
        # Any reviewer other than 'Codex' is rejected
        with self.assertRaises(CollaborationError) as ctx:
            Review(
                review_id=str(uuid.uuid4()),
                work_id=self.work_id,
                verdict="accepted",
                checked_fingerprint="abc",
                reviewer="Antigravity"
            )
        self.assertEqual(ctx.exception.code, "protocol_error")

        rev = Review(
            review_id=str(uuid.uuid4()),
            work_id=self.work_id,
            verdict="accepted",
            checked_fingerprint="abc",
            reviewer="Codex"
        )
        self.assertEqual(rev.reviewer, "Codex")

    def test_turn_model_strict_types(self):
        turn = Turn(
            turn_id=self.turn_id,
            work_id=self.work_id,
            initial_revision=1,
            deadline=60.0,
            outcome="SUCCESS",
            worker_token="tok_123",
            pending_tools=["tool_a", "tool_b"]
        )
        self.assertEqual(turn.outcome, "SUCCESS")
        self.assertEqual(turn.worker_token, "tok_123")
        self.assertEqual(turn.pending_tools, ["tool_a", "tool_b"])

        with self.assertRaises(CollaborationError):
            Turn(
                turn_id=self.turn_id,
                work_id=self.work_id,
                initial_revision=1,
                deadline=60.0,
                outcome="INVALID_OUTCOME"
            )

    # -------------------------------------------------------------------------
    # 3. Artifact Scope Digest (Requirement 6)
    # -------------------------------------------------------------------------
    def test_canonical_scope_digest_requires_valid_root_and_detects_changes(self):
        spec_file = self.checkout_dir / "spec.md"
        spec_file.write_text("Specification content v1", encoding="utf-8")

        # Must provide valid root directory
        d1 = canonical_scope_digest(
            goal="Goal 1",
            artifact_refs=["spec.md"],
            allowed_files=["a.py"],
            allowed_actions=["file_edit"],
            acceptance=["criteria"],
            task_ids=["T001"],
            timeout_seconds=600,
            root_dir=self.checkout_dir
        )

        # Modifying spec content must invalidate the digest
        spec_file.write_text("Specification content v2 MODIFIED", encoding="utf-8")
        d2 = canonical_scope_digest(
            goal="Goal 1",
            artifact_refs=["spec.md"],
            allowed_files=["a.py"],
            allowed_actions=["file_edit"],
            acceptance=["criteria"],
            task_ids=["T001"],
            timeout_seconds=600,
            root_dir=self.checkout_dir
        )
        self.assertNotEqual(d1, d2)

    # -------------------------------------------------------------------------
    # 4. Git Scope Integrity & Edge Cases (Requirement 1)
    # -------------------------------------------------------------------------
    def test_git_scope_integrity_all_variants(self):
        allowed_file = self.checkout_dir / "allowed.py"
        allowed_file.write_text("print('allowed')", encoding="utf-8")

        outside_file = self.checkout_dir / "outside.py"
        outside_file.write_text("print('outside v1')", encoding="utf-8")

        cyrillic_file = self.checkout_dir / "тест с пробелом.py"
        cyrillic_file.write_text("print('cyrillic')", encoding="utf-8")

        subprocess.run(["git", "add", "."], cwd=str(self.checkout_dir), check=True)
        subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=str(self.checkout_dir), check=True)

        baseline = compute_baseline(self.checkout_dir, ["allowed.py"])

        # 1. Allowed file change -> OK
        allowed_file.write_text("print('allowed modified')", encoding="utf-8")
        ok, violations = verify_scope_integrity(self.checkout_dir, baseline, ["allowed.py"])
        self.assertTrue(ok)
        self.assertEqual(len(violations), 0)

        # 2. Staged deletion outside scope -> VIOLATION
        subprocess.run(["git", "rm", "outside.py"], cwd=str(self.checkout_dir), check=True)
        ok, violations = verify_scope_integrity(self.checkout_dir, baseline, ["allowed.py"])
        self.assertFalse(ok)
        self.assertTrue(any("outside.py" in v for v in violations))
        # Revert deletion
        subprocess.run(["git", "checkout", "HEAD", "--", "outside.py"], cwd=str(self.checkout_dir), check=True)

        # 3. Rename outside scope (both destination and source checked) -> VIOLATION
        subprocess.run(["git", "mv", "outside.py", "renamed_outside.py"], cwd=str(self.checkout_dir), check=True)
        ok, violations = verify_scope_integrity(self.checkout_dir, baseline, ["allowed.py"])
        self.assertFalse(ok)
        self.assertTrue(any("renamed_outside.py" in v or "outside.py" in v for v in violations))
        # Revert rename
        subprocess.run(["git", "reset", "--hard", "HEAD"], cwd=str(self.checkout_dir), check=True)

        # 4. Modifying file with UTF-8 spaces outside scope -> VIOLATION
        cyrillic_file.write_text("print('cyrillic modified')", encoding="utf-8")
        ok, violations = verify_scope_integrity(self.checkout_dir, baseline, ["allowed.py"])
        self.assertFalse(ok)
        self.assertTrue(any("тест с пробелом.py" in v for v in violations))
        subprocess.run(["git", "checkout", "HEAD", "--", "."], cwd=str(self.checkout_dir), check=True)

        # 5. Untracked file outside scope -> VIOLATION
        untracked = self.checkout_dir / "untracked_new.py"
        untracked.write_text("print('untracked')", encoding="utf-8")
        ok, violations = verify_scope_integrity(self.checkout_dir, baseline, ["allowed.py"])
        self.assertFalse(ok)
        self.assertTrue(any("untracked_new.py" in v for v in violations))
        untracked.unlink()

    # -------------------------------------------------------------------------
    # 5. Snapshot & Event Coupled Mutation Protocol & Recovery (Requirement 2)
    # -------------------------------------------------------------------------
    def test_coupled_mutation_protocol_and_idempotency(self):
        work = self._create_sample_work(revision=1)

        # 1. Commit first mutation (revision 1)
        seq1 = self.store.commit_mutation(
            work=work,
            event_type="work_created",
            payload={"action": "create"},
            request_id="REQ-001"
        )
        self.assertEqual(seq1, 1)

        # 2. Idempotent replay with same request_id and revision -> returns same seq
        seq_replay = self.store.commit_mutation(
            work=work,
            event_type="work_created",
            payload={"action": "create"},
            request_id="REQ-001"
        )
        self.assertEqual(seq_replay, 1)

        # 3. Duplicate request_id with conflicting revision -> request_conflict error
        work_conflicting = self._create_sample_work(revision=2)
        with self.assertRaises(CollaborationError) as ctx:
            self.store.commit_mutation(
                work=work_conflicting,
                event_type="work_created",
                payload={"action": "create"},
                request_id="REQ-001"
            )
        self.assertEqual(ctx.exception.code, "request_conflict")

        # 4. Strict revision coupling: trying to skip revision (e.g. rev 5 instead of rev 2)
        work_skip = self._create_sample_work(revision=5)
        with self.assertRaises(CollaborationError) as ctx:
            self.store.commit_mutation(
                work=work_skip,
                event_type="step_done",
                payload={},
                request_id="REQ-002"
            )
        self.assertEqual(ctx.exception.code, "revision_conflict")

        # 5. Valid consecutive revision 2 succeeds
        work_rev2 = self._create_sample_work(revision=2)
        seq2 = self.store.commit_mutation(
            work=work_rev2,
            event_type="step_done",
            payload={},
            request_id="REQ-003"
        )
        self.assertEqual(seq2, 2)

    def test_snapshot_event_divergence_rejected_on_load(self):
        # Manually create divergent state: snapshot rev=1, but event rev=99
        work = self._create_sample_work(revision=1)
        self.store.save_snapshot(work)
        self.store.append_event("test_event", {"val": 1}, revision=99)

        with self.assertRaises(CollaborationError) as ctx:
            self.store.load_snapshot()
        self.assertEqual(ctx.exception.code, "corruption_detected")
        self.assertIn("revision", ctx.exception.message.lower())

    def test_interrupted_append_tail_recovery(self):
        work = self._create_sample_work(revision=1)
        self.store.commit_mutation(work, "init", {}, request_id="R1")

        # Simulate power failure / crash leaving incomplete trailing JSON
        with open(self.store.events_file, "a", encoding="utf-8") as f:
            f.write('{"seq": 2, "work_id": "' + self.work_id + '", "partially_written')

        # Recovery should detect and cleanly truncate the incomplete tail line
        recovered = self.store.recover_interrupted_state()
        self.assertTrue(recovered)

        events = self.store.read_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["seq"], 1)

    # -------------------------------------------------------------------------
    # 6. Reservation CAS Ownership, Unknown Lock & Release Guards (Requirement 3)
    # -------------------------------------------------------------------------
    def test_reservation_cas_ownership_and_idempotency(self):
        store = self.store
        pid = os.getpid()
        start_time = time.time()

        # 1. First acquire succeeds
        store.acquire_reservation(pid=pid, start_time=start_time, worker_token="token_A")
        self.assertTrue(store.reservation_file.exists())

        # 2. Same identity reacquire is idempotent no-op
        rec = store.acquire_reservation(pid=pid, start_time=start_time, worker_token="token_A")
        self.assertEqual(rec["worker_token"], "token_A")

        # 3. Different worker token while process is alive -> checkout_busy
        with self.assertRaises(CollaborationError) as ctx:
            store.acquire_reservation(pid=pid, start_time=start_time, worker_token="token_B")
        self.assertEqual(ctx.exception.code, "checkout_busy")

    def test_pre_existing_dirty_and_staged_outside_scope_allowed(self):
        allowed_file = self.checkout_dir / "allowed.py"
        allowed_file.write_text("print('allowed')", encoding="utf-8")

        outside_dirty = self.checkout_dir / "pre_dirty.py"
        outside_dirty.write_text("print('pre_dirty v1')", encoding="utf-8")

        outside_orig = self.checkout_dir / "pre_rename.py"
        outside_orig.write_text("print('pre_rename')", encoding="utf-8")

        subprocess.run(["git", "add", "."], cwd=str(self.checkout_dir), check=True)
        subprocess.run(["git", "commit", "-m", "Base commit"], cwd=str(self.checkout_dir), check=True)

        # Pre-existing changes made BEFORE baseline:
        # 1. Unstaged dirty modification
        outside_dirty.write_text("print('pre_dirty dirty')", encoding="utf-8")
        # 2. Staged rename
        subprocess.run(["git", "mv", "pre_rename.py", "pre_rename_dest.py"], cwd=str(self.checkout_dir), check=True)

        # Compute baseline capturing pre-existing state
        baseline = compute_baseline(self.checkout_dir, ["allowed.py"])

        # Change allowed file
        allowed_file.write_text("print('allowed modified')", encoding="utf-8")

        # Pre-existing dirty and staged files outside scope MUST pass integrity check
        ok, violations = verify_scope_integrity(self.checkout_dir, baseline, ["allowed.py"])
        self.assertTrue(ok)
        self.assertEqual(len(violations), 0)

        # But modifying the pre-existing dirty file further outside scope MUST fail
        outside_dirty.write_text("print('pre_dirty dirty FURTHER')", encoding="utf-8")
        ok2, violations2 = verify_scope_integrity(self.checkout_dir, baseline, ["allowed.py"])
        self.assertFalse(ok2)
        self.assertTrue(any("pre_dirty.py" in v for v in violations2))

    def test_scope_integrity_initially_dirty_reverted_to_clean_and_untracked_disappeared(self):
        allowed_file = self.checkout_dir / "allowed.py"
        allowed_file.write_text("print('allowed')", encoding="utf-8")

        outside_dirty = self.checkout_dir / "outside_dirty.py"
        outside_dirty.write_text("print('outside clean HEAD')", encoding="utf-8")

        subprocess.run(["git", "add", "."], cwd=str(self.checkout_dir), check=True)
        subprocess.run(["git", "commit", "-m", "Base commit"], cwd=str(self.checkout_dir), check=True)

        # 1. Pre-existing dirty modification to outside_dirty.py
        outside_dirty.write_text("print('outside dirty worktree')", encoding="utf-8")

        # 2. Pre-existing untracked file outside scope
        outside_untracked = self.checkout_dir / "outside_untracked.py"
        outside_untracked.write_text("print('outside untracked')", encoding="utf-8")

        baseline = compute_baseline(self.checkout_dir, ["allowed.py"])
        self.assertIn("outside_dirty.py", baseline["worktree_fingerprints"])
        self.assertIn("outside_untracked.py", baseline["worktree_fingerprints"])

        # Modifying allowed file within scope
        allowed_file.write_text("print('allowed modified')", encoding="utf-8")

        # Verifying without touching outside files passes
        ok_base, viols_base = verify_scope_integrity(self.checkout_dir, baseline, ["allowed.py"])
        self.assertTrue(ok_base)
        self.assertEqual(len(viols_base), 0)

        # REGRESSION 1: Initially dirty outside file is reverted to clean HEAD version.
        # Current git status becomes empty for outside_dirty.py, but this MUST fail as a scope violation.
        outside_dirty.write_text("print('outside clean HEAD')", encoding="utf-8")
        ok_rev, viols_rev = verify_scope_integrity(self.checkout_dir, baseline, ["allowed.py"])
        self.assertFalse(ok_rev)
        self.assertTrue(any("outside_dirty.py" in v for v in viols_rev))

        # Restore outside_dirty back to dirty baseline content -> passes again
        outside_dirty.write_text("print('outside dirty worktree')", encoding="utf-8")
        ok_restored, viols_restored = verify_scope_integrity(self.checkout_dir, baseline, ["allowed.py"])
        self.assertTrue(ok_restored)
        self.assertEqual(len(viols_restored), 0)

        # REGRESSION 2: Initially untracked file outside scope disappears (deleted).
        # Current git status has no entry for it, but this MUST fail as a scope violation.
        outside_untracked.unlink()
        ok_del, viols_del = verify_scope_integrity(self.checkout_dir, baseline, ["allowed.py"])
        self.assertFalse(ok_del)
        self.assertTrue(any("outside_untracked.py" in v for v in viols_del))

    def test_fingerprint_stability_mtime_independent_and_unreadable_process_error(self):
        f = self.checkout_dir / "stable_test.txt"
        f.write_text("initial content", encoding="utf-8")
        fp1 = compute_file_fingerprint(f, is_secret=False)
        self.assertNotIn("mtime", fp1)
        self.assertEqual(fp1["status"], "tracked")
        self.assertIn("sha256", fp1)
        self.assertIn("size", fp1)
        self.assertIn("mode", fp1)

        # Rewriting identical content updates mtime, but fingerprint remains equal
        time.sleep(0.01)
        f.write_text("initial content", encoding="utf-8")
        fp2 = compute_file_fingerprint(f, is_secret=False)
        self.assertEqual(fp1, fp2)

        # Secret files preserve mtime metadata and do not compute content sha256
        secret_f = self.checkout_dir / "secret.env"
        secret_f.write_text("SECRET=12345", encoding="utf-8")
        sec_fp1 = compute_file_fingerprint(secret_f, is_secret=True)
        self.assertEqual(sec_fp1["status"], "redacted_secret")
        self.assertIn("mtime", sec_fp1)
        self.assertNotIn("sha256", sec_fp1)

        # Inability to read file raises CollaborationError process_error, not fallback
        unreadable_dir = self.checkout_dir / "unreadable_dir"
        unreadable_dir.mkdir()
        with self.assertRaises(CollaborationError) as ctx:
            compute_file_fingerprint(unreadable_dir, is_secret=False)
        self.assertEqual(ctx.exception.code, "process_error")

    def test_reservation_unknown_lock_cannot_be_safely_released(self):
        store = self.store
        dead_proc = subprocess.Popen([sys.executable, "-c", "import sys; sys.exit(0)"])
        dead_proc.wait()
        dead_pid = dead_proc.pid
        proc_start = time.time() - 100

        store.acquire_reservation(pid=dead_pid, start_time=proc_start, worker_token="tok_1")

        # Mark turn execution_unknown
        store.mark_execution_unknown("tok_1", "Subprocess turn timed out")

        # is_safe_completion=True MUST be rejected with reconciliation_required because execution is unknown!
        with self.assertRaises(CollaborationError) as ctx:
            store.release_reservation(
                "Claiming completion",
                worker_token="tok_1",
                is_safe_completion=True,
                is_recovery=False
            )
        self.assertEqual(ctx.exception.code, "reconciliation_required")
        self.assertTrue(store.reservation_file.exists())

        # Cannot reacquire while execution_unknown is active!
        with self.assertRaises(CollaborationError) as ctx:
            store.acquire_reservation(pid=dead_pid, start_time=proc_start, worker_token="tok_2")
        self.assertEqual(ctx.exception.code, "checkout_busy")

        # In this phase, UNKNOWN reservation CANNOT be released even with is_recovery=True and MANUAL_AUDIT!
        with self.assertRaises(CollaborationError) as ctx:
            store.release_reservation(
                "MANUAL_AUDIT: verified clean tree and no dangling processes",
                worker_token="tok_1",
                is_safe_completion=False,
                is_recovery=True
            )
        self.assertEqual(ctx.exception.code, "reconciliation_required")
        self.assertTrue(store.reservation_file.exists())

        # Arbitrary recovery evidence with is_recovery=True is also rejected with reconciliation_required
        with self.assertRaises(CollaborationError) as ctx:
            store.release_reservation(
                "arbitrary recovery evidence string",
                worker_token="tok_1",
                is_safe_completion=False,
                is_recovery=True
            )
        self.assertEqual(ctx.exception.code, "reconciliation_required")
        self.assertTrue(store.reservation_file.exists())

    def test_reservation_release_strict_evidence_and_invariants(self):
        work_id = str(uuid.uuid4())
        store = WorkStore(
            base_dir=self.base_dir,
            work_id=work_id,
            checkout_root=self.checkout_dir
        )
        store.ensure_dir()

        dead_proc = subprocess.Popen([sys.executable, "-c", "import sys; sys.exit(0)"])
        dead_proc.wait()
        dead_pid = dead_proc.pid
        proc_start = time.time() - 50.0

        # Unlink any existing reservation on checkout
        if store.reservation_file.exists():
            store.reservation_file.unlink()

        # 1. Acquire reservation
        store.acquire_reservation(pid=dead_pid, start_time=proc_start, worker_token="tok_safe")
        self.assertTrue(store.reservation_file.exists())

        # NEGATIVE 1: dead PID but no persisted snapshot exists at all -> permission_denied
        with self.assertRaises(CollaborationError) as ctx:
            store.release_reservation("Finishing", is_safe_completion=True, worker_token="tok_safe")
        self.assertEqual(ctx.exception.code, "permission_denied")
        self.assertTrue(store.reservation_file.exists())

        # Setup base Work object
        turn_id = str(uuid.uuid4())
        turn = Turn(
            turn_id=turn_id,
            work_id=work_id,
            initial_revision=1,
            deadline=time.time() + 60,
            worker_token="tok_safe",
            pid=dead_pid,
            start_time=proc_start,
            outcome="SUCCESS",
            terminal_received=False,  # Initially False
            process_exited=True,
            pending_tools=[],
        )
        last_result = ExecutorResult(
            work_id=work_id,
            turn_id=turn_id,
            kind="implementation_result",
            summary="All work completed successfully",
            claimed_files=["allowed.py"],
            checks=[CheckEvidence("chk_1", "python test", "passed", evidence="all passed")],
            remaining_actions=[],
        )
        work = Work(
            work_id=work_id,
            goal="Test completion release",
            artifact_refs=["spec.md"],
            allowed_files=["allowed.py"],
            allowed_actions=["file_edit"],
            acceptance=["Tests pass"],
            task_ids=["T001"],
            revision=1,
            state="implementing",
            current_turn=turn,
            last_result=last_result,
        )
        store.save_snapshot(work)

        # NEGATIVE 2: dead PID + no terminal (terminal_received=False)
        with self.assertRaises(CollaborationError) as ctx:
            store.release_reservation("Finishing", is_safe_completion=True, worker_token="tok_safe")
        self.assertEqual(ctx.exception.code, "permission_denied")
        self.assertTrue(store.reservation_file.exists())

        # NEGATIVE 2b: process_exited=False
        turn.terminal_received = True
        turn.process_exited = False
        work.current_turn = turn
        store.save_snapshot(work)
        with self.assertRaises(CollaborationError) as ctx:
            store.release_reservation("Finishing", is_safe_completion=True, worker_token="tok_safe")
        self.assertEqual(ctx.exception.code, "permission_denied")
        self.assertTrue(store.reservation_file.exists())

        # Restore process_exited
        turn.process_exited = True
        work.current_turn = turn
        store.save_snapshot(work)

        # NEGATIVE 3: stale checkpoint
        # 3a. caller checkpoint_revision mismatch
        with self.assertRaises(CollaborationError) as ctx:
            store.release_reservation(
                "Finishing",
                is_safe_completion=True,
                worker_token="tok_safe",
                checkpoint_revision=999
            )
        self.assertEqual(ctx.exception.code, "stale_checkpoint")
        self.assertTrue(store.reservation_file.exists())

        # 3b. proof revision mismatch
        with self.assertRaises(CollaborationError) as ctx:
            store.release_reservation(
                "Finishing",
                is_safe_completion=True,
                worker_token="tok_safe",
                proof={"revision": 42}
            )
        self.assertEqual(ctx.exception.code, "stale_checkpoint")

        # NEGATIVE 4: wrong token / start identity
        # 4a. caller passes wrong worker_token
        with self.assertRaises(CollaborationError) as ctx:
            store.release_reservation("Finishing", is_safe_completion=True, worker_token="tok_wrong")
        self.assertEqual(ctx.exception.code, "permission_denied")

        # 4b. persisted turn worker_token mismatch
        turn_bad_tok = Turn(
            turn_id=str(uuid.uuid4()),
            work_id=work_id,
            initial_revision=1,
            deadline=time.time() + 60,
            worker_token="tok_alien",
            pid=dead_pid,
            start_time=proc_start,
            outcome="SUCCESS",
            terminal_received=True,
            process_exited=True,
            pending_tools=[],
        )
        work.current_turn = turn_bad_tok
        store.save_snapshot(work)
        with self.assertRaises(CollaborationError) as ctx:
            store.release_reservation("Finishing", is_safe_completion=True, worker_token="tok_safe")
        self.assertEqual(ctx.exception.code, "permission_denied")

        # 4c. persisted turn PID mismatch
        turn_bad_pid = Turn(
            turn_id=str(uuid.uuid4()),
            work_id=work_id,
            initial_revision=1,
            deadline=time.time() + 60,
            worker_token="tok_safe",
            pid=dead_pid + 8888,
            start_time=proc_start,
            outcome="SUCCESS",
            terminal_received=True,
            process_exited=True,
            pending_tools=[],
        )
        work.current_turn = turn_bad_pid
        store.save_snapshot(work)
        with self.assertRaises(CollaborationError) as ctx:
            store.release_reservation("Finishing", is_safe_completion=True, worker_token="tok_safe")
        self.assertEqual(ctx.exception.code, "permission_denied")

        # 4d. persisted turn start_time mismatch
        turn_bad_start = Turn(
            turn_id=str(uuid.uuid4()),
            work_id=work_id,
            initial_revision=1,
            deadline=time.time() + 60,
            worker_token="tok_safe",
            pid=dead_pid,
            start_time=proc_start + 100.0,
            outcome="SUCCESS",
            terminal_received=True,
            process_exited=True,
            pending_tools=[],
        )
        work.current_turn = turn_bad_start
        store.save_snapshot(work)
        with self.assertRaises(CollaborationError) as ctx:
            store.release_reservation("Finishing", is_safe_completion=True, worker_token="tok_safe")
        self.assertEqual(ctx.exception.code, "permission_denied")

        # Restore valid identity turn
        work.current_turn = turn
        store.save_snapshot(work)

        # NEGATIVE 5: pending tools
        turn_pending = Turn(
            turn_id=turn_id,
            work_id=work_id,
            initial_revision=1,
            deadline=time.time() + 60,
            worker_token="tok_safe",
            pid=dead_pid,
            start_time=proc_start,
            outcome="SUCCESS",
            terminal_received=True,
            process_exited=True,
            pending_tools=["execute_bash_command"],
        )
        work.current_turn = turn_pending
        store.save_snapshot(work)
        with self.assertRaises(CollaborationError) as ctx:
            store.release_reservation("Finishing", is_safe_completion=True, worker_token="tok_safe")
        self.assertEqual(ctx.exception.code, "permission_denied")

        # NEGATIVE 5b: SUCCESS outcome requires persisted last_result
        work.current_turn = turn
        work.last_result = None
        store.save_snapshot(work)
        with self.assertRaises(CollaborationError) as ctx:
            store.release_reservation("Finishing", is_safe_completion=True, worker_token="tok_safe")
        self.assertEqual(ctx.exception.code, "permission_denied")
        work.last_result = last_result
        store.save_snapshot(work)

        # NEGATIVE 6: live / unknown OS process
        # 6a. Live process (is_process_alive returns True)
        with unittest.mock.patch("scripts.ai.collaboration.state.is_process_alive", return_value=True):
            with self.assertRaises(CollaborationError) as ctx:
                store.release_reservation("Finishing", is_safe_completion=True, worker_token="tok_safe")
            self.assertEqual(ctx.exception.code, "permission_denied")

        # 6b. Unknown OS process (is_process_alive returns "unknown")
        with unittest.mock.patch("scripts.ai.collaboration.state.is_process_alive", return_value="unknown"):
            with self.assertRaises(CollaborationError) as ctx:
                store.release_reservation("Finishing", is_safe_completion=True, worker_token="tok_safe")
            self.assertEqual(ctx.exception.code, "permission_denied")

        # POSITIVE SAFE RELEASE:
        # Valid state, matching turn, dead process, verified flags
        store.release_reservation(
            "Clean and fully verified turn safe completion",
            is_safe_completion=True,
            worker_token="tok_safe",
            checkpoint_revision=1,
            proof={"turn_id": turn_id, "revision": 1, "worker_token": "tok_safe"}
        )
        self.assertFalse(store.reservation_file.exists())
        evidence_file = store.checkout_dir / f"released_{work_id}.json"
        self.assertTrue(evidence_file.exists())
        with open(evidence_file, "r", encoding="utf-8") as f:
            ev_data = json.load(f)
        self.assertTrue(ev_data.get("released"))
        self.assertEqual(ev_data.get("released_turn_id"), turn_id)
        self.assertEqual(ev_data.get("released_revision"), 1)
        self.assertEqual(ev_data.get("proof", {}).get("turn_id"), turn_id)

    def test_verify_scope_integrity_external_reparse_and_secret_never_opened(self):
        canary_secret = "CANARY_SECRET_NEVER_READ_VERIFY_445566"
        external_canary = Path(self.temp_dir) / "ext_canary_state.txt"
        external_canary.write_text(f"EXT_SECRET={canary_secret}\n", encoding="utf-8")

        in_repo_secret = self.checkout_dir / ".env"
        in_repo_secret.write_text(f"REPO_SECRET={canary_secret}\n", encoding="utf-8")

        # Initial commit with allowed.py
        allowed_file = self.checkout_dir / "allowed.py"
        allowed_file.write_text("print('allowed')", encoding="utf-8")
        subprocess.run(["git", "add", "allowed.py"], cwd=str(self.checkout_dir), check=True)
        subprocess.run(["git", "commit", "-m", "Commit allowed"], cwd=str(self.checkout_dir), check=True)

        opened_paths = []
        original_open = open

        def tracking_open(f, *args, **kwargs):
            try:
                p_str = str(Path(f).resolve())
                opened_paths.append(p_str)
            except Exception:
                pass
            return original_open(f, *args, **kwargs)

        symlinks_supported = False
        ext_link = self.checkout_dir / "ext_link_test.txt"
        secret_link = self.checkout_dir / "secret_link_test.txt"
        try:
            os.symlink(external_canary, ext_link)
            os.symlink(in_repo_secret, secret_link)
            symlinks_supported = True
        except (OSError, NotImplementedError):
            symlinks_supported = False

        with unittest.mock.patch("builtins.open", side_effect=tracking_open):
            if symlinks_supported:
                # 1. compute_file_fingerprint on external symlink returns symlink metadata without raw link_target
                fp_ext = compute_file_fingerprint(ext_link, is_secret=False, repo_root=self.checkout_dir)
                self.assertEqual(fp_ext["status"], "symlink")
                self.assertNotIn("sha256", fp_ext)
                self.assertNotIn("link_target", fp_ext)
                self.assertIn("link_digest", fp_ext)

                # 2. compute_file_fingerprint on in-repo secret link returns symlink metadata
                fp_sec = compute_file_fingerprint(secret_link, is_secret=False, repo_root=self.checkout_dir)
                self.assertEqual(fp_sec["status"], "symlink")
                self.assertNotIn("sha256", fp_sec)
                self.assertNotIn("link_target", fp_sec)
                self.assertIn("link_digest", fp_sec)

                # 3. compute_baseline and verify_scope_integrity pass repo_root and never open targets
                baseline = compute_baseline(self.checkout_dir, ["allowed.py"])
                self.assertIn("ext_link_test.txt", baseline["worktree_fingerprints"])
                self.assertIn("secret_link_test.txt", baseline["worktree_fingerprints"])

                ok, viols = verify_scope_integrity(self.checkout_dir, baseline, ["allowed.py"])
                self.assertTrue(ok)
                self.assertEqual(len(viols), 0)
            else:
                # Deterministic mock guard for external path outside repo_root
                external_mock_path = self.checkout_dir / "mock_ext_path.txt"
                external_mock_path.write_text("placeholder external", encoding="utf-8")

                secret_mock_path = self.checkout_dir / "mock_secret_path.txt"
                secret_mock_path.write_text("placeholder secret", encoding="utf-8")

                original_resolve = Path.resolve
                target_external_res = external_canary.resolve()
                target_secret_res = in_repo_secret.resolve()

                def selective_resolve(self, *args, **kwargs):
                    p_str = str(self).replace("\\", "/")
                    if p_str.endswith("mock_ext_path.txt"):
                        return target_external_res
                    if p_str.endswith("mock_secret_path.txt"):
                        return target_secret_res
                    return original_resolve(self, *args, **kwargs)

                with unittest.mock.patch.object(Path, "resolve", new=selective_resolve):
                    # 1. External path returns external_path without sha256 or reading
                    fp = compute_file_fingerprint(external_mock_path, is_secret=False, repo_root=self.checkout_dir)
                    self.assertEqual(fp["status"], "external_path")
                    self.assertNotIn("sha256", fp)
                    self.assertIn("resolved_path_digest", fp)

                    # 2. In-repo secret resolution returns redacted_secret without sha256 or reading
                    fp_sec = compute_file_fingerprint(secret_mock_path, is_secret=False, repo_root=self.checkout_dir)
                    self.assertEqual(fp_sec["status"], "redacted_secret")
                    self.assertNotIn("sha256", fp_sec)

                    # 3. Baseline and verify_scope_integrity with external and secret paths
                    baseline = compute_baseline(self.checkout_dir, ["allowed.py"])
                    self.assertIn("mock_ext_path.txt", baseline["worktree_fingerprints"])
                    self.assertIn("mock_secret_path.txt", baseline["worktree_fingerprints"])

                    ok, viols = verify_scope_integrity(self.checkout_dir, baseline, ["allowed.py"])
                    self.assertTrue(ok)
                    self.assertEqual(len(viols), 0)

            # 4. Valid nonsecret regular file separately verifies standard tracked SHA
            regular_file = self.checkout_dir / "regular_valid.py"
            regular_file.write_text("print('hello regular')", encoding="utf-8")
            fp_reg = compute_file_fingerprint(regular_file, is_secret=False, repo_root=self.checkout_dir)
            self.assertEqual(fp_reg["status"], "tracked")
            self.assertIn("sha256", fp_reg)
            self.assertNotIn("mtime", fp_reg)

            # CRUCIAL ASSERTION: Neither external canary nor in-repo secret was opened
            resolved_ext = str(external_canary.resolve())
            resolved_sec = str(in_repo_secret.resolve())
            self.assertNotIn(resolved_ext, opened_paths)
            self.assertNotIn(resolved_sec, opened_paths)

    def test_compute_file_fingerprint_permission_and_io_errors_not_suppressed_as_missing(self):
        """
        Anti-Error Suppression: Verifies that PermissionError and generic OSError in
        exists(), stat(), resolve(), or lstat() raise CollaborationError('process_error')
        instead of returning status: missing. Only genuine FileNotFoundError returns missing.
        Ensures target files are never opened on resolve/stat/lstat/exists errors.
        """
        opened_files = []
        original_builtin_open = open

        def tracking_open(f, *args, **kwargs):
            try:
                opened_files.append(str(Path(f).resolve()))
            except Exception:
                pass
            return original_builtin_open(f, *args, **kwargs)

        test_file = self.checkout_dir / "err_test_file.py"
        test_file.write_text("print('test err')", encoding="utf-8")
        test_file_resolved = str(test_file.resolve())

        # 1. Genuine missing file still returns status: missing
        missing_file = self.checkout_dir / "genuinely_missing.txt"
        fp_missing = compute_file_fingerprint(missing_file, is_secret=False, repo_root=self.checkout_dir)
        self.assertEqual(fp_missing, {"status": "missing"})

        # 2. Existing regular file returns valid tracked status with sha256
        fp_normal = compute_file_fingerprint(test_file, is_secret=False, repo_root=self.checkout_dir)
        self.assertEqual(fp_normal.get("status"), "tracked")
        self.assertIn("sha256", fp_normal)

        with unittest.mock.patch("builtins.open", side_effect=tracking_open):
            opened_files.clear()

            # 3. Injected PermissionError in exists() -> process_error (not missing), never opened
            original_exists = Path.exists

            def perm_err_exists(path_obj):
                if str(path_obj).endswith("err_test_file.py"):
                    raise PermissionError("EACCES: Access denied checking existence")
                return original_exists(path_obj)

            with unittest.mock.patch.object(Path, "exists", new=perm_err_exists):
                with self.assertRaises(CollaborationError) as ctx:
                    compute_file_fingerprint(test_file, is_secret=False, repo_root=self.checkout_dir)
                self.assertEqual(ctx.exception.code, "process_error")
                self.assertNotIn(test_file_resolved, opened_files)

            # 4. Injected generic OSError in exists() -> process_error (not missing), never opened
            def os_err_exists(path_obj):
                if str(path_obj).endswith("err_test_file.py"):
                    raise OSError(5, "EIO: Hardware I/O failure checking existence")
                return original_exists(path_obj)

            with unittest.mock.patch.object(Path, "exists", new=os_err_exists):
                with self.assertRaises(CollaborationError) as ctx:
                    compute_file_fingerprint(test_file, is_secret=False, repo_root=self.checkout_dir)
                self.assertEqual(ctx.exception.code, "process_error")
                self.assertNotIn(test_file_resolved, opened_files)

            # 5. Injected PermissionError in stat() -> process_error (not missing), never opened
            original_stat = Path.stat

            def perm_err_stat(path_obj):
                if str(path_obj).endswith("err_test_file.py"):
                    raise PermissionError("EACCES: Access denied on stat")
                return original_stat(path_obj)

            with unittest.mock.patch.object(Path, "stat", new=perm_err_stat):
                with self.assertRaises(CollaborationError) as ctx:
                    compute_file_fingerprint(test_file, is_secret=False, repo_root=self.checkout_dir)
                self.assertEqual(ctx.exception.code, "process_error")
                self.assertNotIn(test_file_resolved, opened_files)

            # 6. Injected generic OSError in stat() -> process_error (not missing), never opened
            def os_err_stat(path_obj):
                if str(path_obj).endswith("err_test_file.py"):
                    raise OSError(5, "EIO: Disk I/O error on stat")
                return original_stat(path_obj)

            with unittest.mock.patch.object(Path, "stat", new=os_err_stat):
                with self.assertRaises(CollaborationError) as ctx:
                    compute_file_fingerprint(test_file, is_secret=False, repo_root=self.checkout_dir)
                self.assertEqual(ctx.exception.code, "process_error")
                self.assertNotIn(test_file_resolved, opened_files)

            # 7. Injected PermissionError in resolve() (in-repo normal branch) -> process_error
            original_resolve = Path.resolve

            def perm_err_resolve(path_obj, *args, **kwargs):
                if str(path_obj).endswith("err_test_file.py"):
                    raise PermissionError("EACCES: Access denied on resolve")
                return original_resolve(path_obj, *args, **kwargs)

            with unittest.mock.patch.object(Path, "resolve", new=perm_err_resolve):
                with self.assertRaises(CollaborationError) as ctx:
                    compute_file_fingerprint(test_file, is_secret=False, repo_root=self.checkout_dir)
                self.assertEqual(ctx.exception.code, "process_error")
                self.assertNotIn(test_file_resolved, opened_files)

            # 8. Injected generic OSError in resolve() -> process_error
            def os_err_resolve(path_obj, *args, **kwargs):
                if str(path_obj).endswith("err_test_file.py"):
                    raise OSError(5, "EIO: Disk I/O error on resolve")
                return original_resolve(path_obj, *args, **kwargs)

            with unittest.mock.patch.object(Path, "resolve", new=os_err_resolve):
                with self.assertRaises(CollaborationError) as ctx:
                    compute_file_fingerprint(test_file, is_secret=False, repo_root=self.checkout_dir)
                self.assertEqual(ctx.exception.code, "process_error")
                self.assertNotIn(test_file_resolved, opened_files)

            # 9. Injected PermissionError & generic OSError on external path branch
            ext_mock = self.checkout_dir / "mock_ext_error.txt"
            ext_mock.write_text("ext content", encoding="utf-8")
            outside_target = Path(self.temp_dir) / "outside_target.txt"

            def resolve_to_outside(path_obj, *args, **kwargs):
                if str(path_obj).endswith("mock_ext_error.txt"):
                    return outside_target
                return original_resolve(path_obj, *args, **kwargs)

            original_lstat = os.lstat

            def perm_err_lstat(path_val, *args, **kwargs):
                if "mock_ext_error.txt" in str(path_val):
                    raise PermissionError("EACCES: Access denied on external lstat")
                return original_lstat(path_val, *args, **kwargs)

            with unittest.mock.patch.object(Path, "resolve", new=resolve_to_outside):
                with unittest.mock.patch("os.lstat", side_effect=perm_err_lstat):
                    with self.assertRaises(CollaborationError) as ctx:
                        compute_file_fingerprint(ext_mock, is_secret=False, repo_root=self.checkout_dir)
                    self.assertEqual(ctx.exception.code, "process_error")

            def os_err_lstat(path_val, *args, **kwargs):
                if "mock_ext_error.txt" in str(path_val):
                    raise OSError(5, "EIO: External lstat I/O failure")
                return original_lstat(path_val, *args, **kwargs)

            with unittest.mock.patch.object(Path, "resolve", new=resolve_to_outside):
                with unittest.mock.patch("os.lstat", side_effect=os_err_lstat):
                    with self.assertRaises(CollaborationError) as ctx:
                        compute_file_fingerprint(ext_mock, is_secret=False, repo_root=self.checkout_dir)
                    self.assertEqual(ctx.exception.code, "process_error")


if __name__ == "__main__":
    unittest.main()
