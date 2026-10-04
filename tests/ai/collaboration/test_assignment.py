"""
Unit and integration tests for offline assignment creation and approval seam (T012 part + T014).
Validates:
- Actual temporary Git fixture with Cyrillic characters and spaces
- create / awaiting_approval and durable checkpoint/handoff before provider prompt
- Strict inputs, schema invariants, and rejection of unknown task IDs
- Path traversal, secret files, and symlink/junction escape rejection
- Scope digest invalidation upon artifact modification
- Strict human approval decision, non-imitation guard against AI/Codex source
- Idempotency with exact same request_id and payload
- Conflict detection on duplicate request_id with different payload
- Multi-work isolation and assertion of ZERO provider/worker calls
- External dirty symlink canary target never read
"""

import os
import sys
import json
import uuid
import time
import shutil
import tempfile
import subprocess
import unittest
import unittest.mock
from datetime import datetime, timezone
from pathlib import Path

# Add project root to sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.ai.collaboration.coordinator import (
    CollaborationCoordinator,
    validate_assignment,
)
from scripts.ai.collaboration.state import (
    Work,
    Approval,
    Handoff,
    WorkStore,
    CollaborationError,
    SCHEMA_VERSION,
    DEFAULT_TIMEOUT_SECONDS,
    canonical_scope_digest,
    compute_baseline,
    verify_scope_integrity,
    compute_file_fingerprint,
    find_git_root,
    is_valid_uuid,
)


class TestCollaborationAssignment(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="ai_collab_test_")
        # Actual temporary Git fixture with Cyrillic characters and spaces
        self.project_root = Path(self.temp_dir) / "Тестовый репозиторий с пробелами"
        self.project_root.mkdir(parents=True, exist_ok=True)
        self.base_dir = self.project_root / "logs" / "ai" / "collaboration"

        # Initialize Git repository
        subprocess.run(["git", "init"], cwd=str(self.project_root), capture_output=True, check=True)
        subprocess.run(["git", "config", "user.name", "Tester User"], cwd=str(self.project_root), check=True)
        subprocess.run(["git", "config", "user.email", "tester@example.com"], cwd=str(self.project_root), check=True)

        # Create specs and task artifacts
        specs_dir = self.project_root / "specs" / "001-feature"
        specs_dir.mkdir(parents=True, exist_ok=True)

        self.spec_file = specs_dir / "spec.md"
        self.spec_file.write_text("# Feature 001 Specification\nValid initial spec.\n", encoding="utf-8")

        self.tasks_file = specs_dir / "tasks.md"
        self.tasks_file.write_text(
            "# Feature 001 Tasks\n"
            "- [ ] T001 Initialize foundation and core models\n"
            "- [ ] T002 Implement business logic and service seams\n"
            "- [ ] T003 Verify tests and acceptance criteria\n",
            encoding="utf-8"
        )

        # Create source and outside files
        src_dir = self.project_root / "src"
        src_dir.mkdir(parents=True, exist_ok=True)
        self.allowed_file = src_dir / "allowed_app.py"
        self.allowed_file.write_text("print('allowed initial app')\n", encoding="utf-8")

        self.outside_file = self.project_root / "outside_untouched.txt"
        self.outside_file.write_text("outside scope initial content\n", encoding="utf-8")

        # .gitignore matching real project repository behavior (logs/ ignored)
        gitignore_file = self.project_root / ".gitignore"
        gitignore_file.write_text("logs/\n", encoding="utf-8")

        # Initial commit
        subprocess.run(["git", "add", "."], cwd=str(self.project_root), capture_output=True, check=True)
        subprocess.run(["git", "commit", "-m", "Initial commit with specs"], cwd=str(self.project_root), capture_output=True, check=True)

        self.coordinator = CollaborationCoordinator(
            project_root=self.project_root,
            base_dir=self.base_dir
        )

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _sample_assignment(self, **kwargs):
        base = {
            "goal": "Implement feature 001 foundation",
            "artifact_refs": ["specs/001-feature/spec.md", "specs/001-feature/tasks.md"],
            "allowed_files": ["src/allowed_app.py"],
            "allowed_actions": ["file_edit"],
            "acceptance": ["Tests pass without regression", "Output conforms to contract"],
            "task_ids": ["T001", "T002"],
            "timeout_seconds": 600,
        }
        base.update(kwargs)
        return base

    # -------------------------------------------------------------------------
    # 1. Create awaiting_approval & Checkpoint Verification
    # -------------------------------------------------------------------------
    def test_create_awaiting_approval_and_checkpoint_created(self):
        req_id = str(uuid.uuid4())
        assignment = self._sample_assignment()

        resp = self.coordinator.create(req_id, assignment)

        self.assertEqual(resp["schema_version"], SCHEMA_VERSION)
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["state"], "awaiting_approval")
        self.assertEqual(resp["next_action"], "approve")
        self.assertEqual(resp["revision"], 1)
        self.assertTrue(resp["execution_known"])
        self.assertEqual(resp["last_event_seq"], 1)

        work_id = resp["work_id"]
        store = WorkStore(base_dir=self.base_dir, work_id=work_id, checkout_root=self.project_root)

        # Checkpoint files must exist before any prompt
        self.assertTrue(store.snapshot_file.exists())
        self.assertTrue(store.events_file.exists())
        self.assertTrue(store.handoff_file.exists())

        work = store.load_snapshot()
        self.assertEqual(work.work_id, work_id)
        self.assertEqual(work.state, "awaiting_approval")
        self.assertIsNone(work.approval_id)
        self.assertEqual(work.revision, 1)
        self.assertEqual(work.goal, assignment["goal"])
        self.assertIsNotNone(work.baseline)

        # Handoff markdown rendered and stored
        handoff_md = store.load_handoff()
        self.assertIn("Implement feature 001 foundation", handoff_md)
        self.assertIn("awaiting_approval", handoff_md)

        # Reservation file must NOT be acquired on create
        self.assertFalse(store.reservation_file.exists())

    # -------------------------------------------------------------------------
    # 2. Strict Inputs and Unknown Task ID Rejected
    # -------------------------------------------------------------------------
    def test_strict_inputs_and_unknown_task_id_rejected(self):
        # 1. Unknown task ID T999 not in tasks.md
        req_id = str(uuid.uuid4())
        bad_task_assignment = self._sample_assignment(task_ids=["T001", "T999"])
        with self.assertRaises(CollaborationError) as ctx:
            self.coordinator.create(req_id, bad_task_assignment)
        self.assertEqual(ctx.exception.code, "protocol_error")
        self.assertIn("T999", ctx.exception.message)

        # 2. Empty goal
        with self.assertRaises(CollaborationError) as ctx:
            self.coordinator.create(str(uuid.uuid4()), self._sample_assignment(goal="   "))
        self.assertEqual(ctx.exception.code, "protocol_error")

        # 3. Invalid action kind
        with self.assertRaises(CollaborationError) as ctx:
            self.coordinator.create(str(uuid.uuid4()), self._sample_assignment(allowed_actions=["unauthorized_shell"]))
        self.assertEqual(ctx.exception.code, "protocol_error")

        # 4. Empty acceptance criteria
        with self.assertRaises(CollaborationError) as ctx:
            self.coordinator.create(str(uuid.uuid4()), self._sample_assignment(acceptance=[]))
        self.assertEqual(ctx.exception.code, "protocol_error")

        # 5. Invalid timeout
        with self.assertRaises(CollaborationError) as ctx:
            self.coordinator.create(str(uuid.uuid4()), self._sample_assignment(timeout_seconds=-10))
        self.assertEqual(ctx.exception.code, "protocol_error")

        # 6. Unknown extraneous fields in assignment
        extra_assignment = self._sample_assignment()
        extra_assignment["extra_forbidden_key"] = 123
        with self.assertRaises(CollaborationError) as ctx:
            self.coordinator.create(str(uuid.uuid4()), extra_assignment)
        self.assertEqual(ctx.exception.code, "protocol_error")

    # -------------------------------------------------------------------------
    # 3. Path Traversal, Secret Files, and Symlink Escape Rejection
    # -------------------------------------------------------------------------
    def test_path_traversal_secret_and_symlink_escape_rejected(self):
        # 1. Path traversal in artifact_refs
        with self.assertRaises(CollaborationError) as ctx:
            self.coordinator.create(str(uuid.uuid4()), self._sample_assignment(
                artifact_refs=["../outside.txt"]
            ))
        self.assertEqual(ctx.exception.code, "scope_violation")

        # 2. Absolute path in allowed_files
        with self.assertRaises(CollaborationError) as ctx:
            self.coordinator.create(str(uuid.uuid4()), self._sample_assignment(
                allowed_files=["/etc/shadow" if sys.platform != "win32" else "C:/Windows/system32/cmd.exe"]
            ))
        self.assertEqual(ctx.exception.code, "scope_violation")

        # 3. Secret file in artifact_refs (.env)
        secret_file = self.project_root / ".env"
        secret_file.write_text("SECRET=123", encoding="utf-8")
        with self.assertRaises(CollaborationError) as ctx:
            self.coordinator.create(str(uuid.uuid4()), self._sample_assignment(
                artifact_refs=[".env"]
            ))
        self.assertEqual(ctx.exception.code, "scope_violation")

        # 4. Secret file in allowed_files (pfx)
        with self.assertRaises(CollaborationError) as ctx:
            self.coordinator.create(str(uuid.uuid4()), self._sample_assignment(
                allowed_files=["certs/dev.pfx"]
            ))
        self.assertEqual(ctx.exception.code, "scope_violation")

        # 5. Non-existent artifact ref
        with self.assertRaises(CollaborationError) as ctx:
            self.coordinator.create(str(uuid.uuid4()), self._sample_assignment(
                artifact_refs=["specs/001-feature/non_existent.md"]
            ))
        self.assertEqual(ctx.exception.code, "scope_violation")

    # -------------------------------------------------------------------------
    # 4. External Dirty Symlink Canary Target NEVER Read
    # -------------------------------------------------------------------------
    def test_external_dirty_symlink_canary_target_never_read(self):
        """
        Ensures baseline computation on untracked/dirty symlinks/junctions
        records only link identity/metadata and NEVER reads external file content.
        """
        canary_secret = "CANARY_SECRET_NEVER_READ_987654321"
        external_canary_file = Path(self.temp_dir) / "external_canary.txt"
        external_canary_file.write_text(canary_secret, encoding="utf-8")

        symlink_path = self.project_root / "link_to_external.txt"
        symlink_created = False

        try:
            os.symlink(external_canary_file, symlink_path)
            symlink_created = True
        except (OSError, NotImplementedError):
            pass

        if symlink_created:
            # Real OS symlink succeeded
            fp = compute_file_fingerprint(symlink_path, is_secret=False, repo_root=self.project_root)
            self.assertEqual(fp["status"], "symlink")
            self.assertIn("link_digest", fp)
            self.assertNotIn("sha256", fp)

            # Compute full baseline and ensure canary content is NEVER present anywhere
            baseline = compute_baseline(self.project_root, ["src/allowed_app.py"])
            baseline_str = json.dumps(baseline)
            self.assertNotIn(canary_secret, baseline_str)

            # verify_scope_integrity must use the exact same policy
            ok, viols = verify_scope_integrity(self.project_root, baseline, ["src/allowed_app.py"])
            self.assertTrue(ok)
        else:
            # Fallback mock guard when OS privileges forbid unprivileged symlink creation (e.g. Windows without Dev Mode)
            mock_lstat_res = os.stat_result((0o120000, 1, 1, 1, 1000, 1000, 42, 100.0, 100.0, 100.0))
            with unittest.mock.patch("os.path.islink", return_value=True), \
                 unittest.mock.patch("os.lstat", return_value=mock_lstat_res), \
                 unittest.mock.patch("os.readlink", return_value=str(external_canary_file)):
                fp = compute_file_fingerprint(self.project_root / "mock_link.txt", is_secret=False, repo_root=self.project_root)
                self.assertEqual(fp["status"], "symlink")
                self.assertIn("link_digest", fp)
                self.assertNotIn("sha256", fp)

    # -------------------------------------------------------------------------
    # 5. Approve Success & next_action=start
    # -------------------------------------------------------------------------
    def test_approve_success_and_next_action_start(self):
        req_id = str(uuid.uuid4())
        assignment = self._sample_assignment()
        create_resp = self.coordinator.create(req_id, assignment)
        work_id = create_resp["work_id"]

        scope_digest = self.coordinator.compute_scope_digest(work_id)
        self.assertIsInstance(scope_digest, str)
        self.assertEqual(len(scope_digest), 64)

        decision = {
            "source": "human user",
            "approved_text": "Proceed with T001 and T002 implementation",
            "approved_actions": ["file_edit"],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        app_req_id = str(uuid.uuid4())
        app_resp = self.coordinator.approve(
            work_id=work_id,
            expected_revision=1,
            request_id=app_req_id,
            decision=decision,
            scope_digest=scope_digest,
        )

        self.assertEqual(app_resp["schema_version"], SCHEMA_VERSION)
        self.assertTrue(app_resp["ok"])
        self.assertEqual(app_resp["work_id"], work_id)
        self.assertEqual(app_resp["revision"], 2)
        self.assertEqual(app_resp["state"], "awaiting_approval")  # Still awaiting_approval until start
        self.assertEqual(app_resp["next_action"], "start")
        self.assertEqual(app_resp["last_event_seq"], 2)

        store = WorkStore(base_dir=self.base_dir, work_id=work_id, checkout_root=self.project_root)
        work = store.load_snapshot()
        self.assertEqual(work.revision, 2)
        self.assertIsNotNone(work.approval_id)

        # Approval record persisted
        approval = store.load_approval()
        self.assertIsNotNone(approval)
        self.assertEqual(approval.approval_id, work.approval_id)
        self.assertEqual(approval.scope_digest, scope_digest)
        self.assertEqual(approval.source, "human user")
        self.assertEqual(approval.approved_actions, ["file_edit"])

        # Handoff boundary updated
        handoff_md = store.load_handoff()
        self.assertIn("approved_awaiting_start", handoff_md)

    # -------------------------------------------------------------------------
    # 6. Scope Digest Invalidated When Artifact Modified on Disk
    # -------------------------------------------------------------------------
    def test_approved_digest_changed_artifact_invalidates(self):
        req_id = str(uuid.uuid4())
        assignment = self._sample_assignment()
        create_resp = self.coordinator.create(req_id, assignment)
        work_id = create_resp["work_id"]

        old_digest = self.coordinator.compute_scope_digest(work_id)

        # Alter tasks.md artifact on disk after creation
        time.sleep(0.01)
        self.tasks_file.write_text(
            self.tasks_file.read_text(encoding="utf-8") + "- [ ] T004 Sneak in unapproved task\n",
            encoding="utf-8"
        )

        decision = {
            "source": "human user",
            "approved_text": "Approved",
            "approved_actions": ["file_edit"],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        # Attempting to approve with old scope digest MUST fail
        with self.assertRaises(CollaborationError) as ctx:
            self.coordinator.approve(
                work_id=work_id,
                expected_revision=1,
                request_id=str(uuid.uuid4()),
                decision=decision,
                scope_digest=old_digest,
            )
        self.assertEqual(ctx.exception.code, "scope_violation")
        self.assertIn("digest mismatch", ctx.exception.message.lower())

        # Work remains unapproved and at revision 1
        store = WorkStore(base_dir=self.base_dir, work_id=work_id, checkout_root=self.project_root)
        work = store.load_snapshot()
        self.assertEqual(work.revision, 1)
        self.assertIsNone(work.approval_id)

    # -------------------------------------------------------------------------
    # 7. Wrong Work, Revision, and Automated AI Decision Rejected
    # -------------------------------------------------------------------------
    def test_wrong_work_revision_and_decision_rejected(self):
        req_id = str(uuid.uuid4())
        assignment = self._sample_assignment()
        create_resp = self.coordinator.create(req_id, assignment)
        work_id = create_resp["work_id"]
        scope_digest = self.coordinator.compute_scope_digest(work_id)

        valid_decision = {
            "source": "human user",
            "approved_text": "Proceed",
            "approved_actions": ["file_edit"],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        # 1. Non-existent work_id
        with self.assertRaises(CollaborationError) as ctx:
            self.coordinator.approve(
                work_id=str(uuid.uuid4()),
                expected_revision=1,
                request_id=str(uuid.uuid4()),
                decision=valid_decision,
                scope_digest=scope_digest,
            )
        self.assertEqual(ctx.exception.code, "protocol_error")

        # 2. Revision conflict (expected 5, actual 1)
        with self.assertRaises(CollaborationError) as ctx:
            self.coordinator.approve(
                work_id=work_id,
                expected_revision=5,
                request_id=str(uuid.uuid4()),
                decision=valid_decision,
                scope_digest=scope_digest,
            )
        self.assertEqual(ctx.exception.code, "revision_conflict")

        # 3. AI / Codex automated decision imitation is STRICTLY FORBIDDEN
        ai_decision = {
            "source": "Codex",
            "approved_text": "Model generated approval simulation",
            "approved_actions": ["file_edit"],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        with self.assertRaises(CollaborationError) as ctx:
            self.coordinator.approve(
                work_id=work_id,
                expected_revision=1,
                request_id=str(uuid.uuid4()),
                decision=ai_decision,
                scope_digest=scope_digest,
            )
        self.assertEqual(ctx.exception.code, "permission_denied")
        self.assertIn("cannot be codex", ctx.exception.message.lower())

        # 4. Decision approved_actions does not cover work allowed_actions
        insufficient_decision = {
            "source": "human user",
            "approved_text": "Approved partial",
            "approved_actions": ["build_run"],  # Missing file_edit
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        with self.assertRaises(CollaborationError) as ctx:
            self.coordinator.approve(
                work_id=work_id,
                expected_revision=1,
                request_id=str(uuid.uuid4()),
                decision=insufficient_decision,
                scope_digest=scope_digest,
            )
        self.assertEqual(ctx.exception.code, "permission_denied")

    # -------------------------------------------------------------------------
    # 8. Repeated Create and Approve Idempotency
    # -------------------------------------------------------------------------
    def test_repeated_create_and_approve_same_outcome_idempotent(self):
        req_id = str(uuid.uuid4())
        assignment = self._sample_assignment()

        # First create
        resp1 = self.coordinator.create(req_id, assignment)
        # Repeated create with exact same request_id and payload
        resp2 = self.coordinator.create(req_id, assignment)

        self.assertEqual(resp1, resp2)
        self.assertEqual(resp1["work_id"], resp2["work_id"])

        work_id = resp1["work_id"]
        scope_digest = self.coordinator.compute_scope_digest(work_id)
        decision = {
            "source": "human user",
            "approved_text": "Approved",
            "approved_actions": ["file_edit"],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        app_req_id = str(uuid.uuid4())
        # First approve
        app_resp1 = self.coordinator.approve(work_id, 1, app_req_id, decision, scope_digest)
        # Repeated approve with exact same request_id and payload
        app_resp2 = self.coordinator.approve(work_id, 1, app_req_id, decision, scope_digest)

        self.assertEqual(app_resp1, app_resp2)
        self.assertEqual(app_resp1["revision"], 2)

    # -------------------------------------------------------------------------
    # 9. Different Payload Same ID Conflict
    # -------------------------------------------------------------------------
    def test_different_payload_same_id_conflict(self):
        same_req_id = str(uuid.uuid4())
        assignment_a = self._sample_assignment(goal="Goal A")
        assignment_b = self._sample_assignment(goal="Goal B (Conflicting)")

        self.coordinator.create(same_req_id, assignment_a)

        # Attempting create with same request_id but different assignment must raise request_conflict
        with self.assertRaises(CollaborationError) as ctx:
            self.coordinator.create(same_req_id, assignment_b)
        self.assertEqual(ctx.exception.code, "request_conflict")

    # -------------------------------------------------------------------------
    # 10. Multi-Work Isolation, Outside Files Preserved, and Zero Provider Calls
    # -------------------------------------------------------------------------
    def test_two_works_isolation_and_zero_provider_calls(self):
        # Work 1
        req1 = str(uuid.uuid4())
        resp1 = self.coordinator.create(req1, self._sample_assignment(goal="Work 1 Goal"))
        work_id_1 = resp1["work_id"]

        # Work 2
        req2 = str(uuid.uuid4())
        resp2 = self.coordinator.create(req2, self._sample_assignment(goal="Work 2 Goal"))
        work_id_2 = resp2["work_id"]

        self.assertNotEqual(work_id_1, work_id_2)

        store1 = WorkStore(base_dir=self.base_dir, work_id=work_id_1, checkout_root=self.project_root)
        store2 = WorkStore(base_dir=self.base_dir, work_id=work_id_2, checkout_root=self.project_root)

        # Snapshots are isolated in distinct directories
        self.assertTrue(store1.snapshot_file.exists())
        self.assertTrue(store2.snapshot_file.exists())
        self.assertEqual(store1.load_snapshot().goal, "Work 1 Goal")
        self.assertEqual(store2.load_snapshot().goal, "Work 2 Goal")

        # Approve Work 1 only
        digest1 = self.coordinator.compute_scope_digest(work_id_1)
        decision = {
            "source": "human user",
            "approved_text": "Approve Work 1",
            "approved_actions": ["file_edit"],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        self.coordinator.approve(work_id_1, 1, str(uuid.uuid4()), decision, digest1)

        # Work 1 is at revision 2, Work 2 remains untouched at revision 1 and unapproved
        self.assertEqual(store1.load_snapshot().revision, 2)
        self.assertIsNotNone(store1.load_snapshot().approval_id)
        self.assertEqual(store2.load_snapshot().revision, 1)
        self.assertIsNone(store2.load_snapshot().approval_id)

        # Outside files remain completely untouched
        self.assertEqual(self.outside_file.read_text(encoding="utf-8"), "outside scope initial content\n")

        # ZERO provider or worker calls:
        # Neither store acquired reservation or spawned any process
        self.assertFalse(store1.reservation_file.exists())
        self.assertFalse(store2.reservation_file.exists())

    # -------------------------------------------------------------------------
    # 11. Git Root Resolution Roundtrip (Unicode + Spaces) & Negative Non-Repo
    # -------------------------------------------------------------------------
    def test_find_git_root_unicode_spaces_roundtrip_and_negative_non_repo(self):
        # 1. Project root roundtrip with Cyrillic characters and spaces
        resolved_root = find_git_root(self.project_root)
        self.assertEqual(resolved_root, self.project_root.resolve())
        self.assertTrue((resolved_root / ".git").exists())

        # 2. Subdirectory inside Unicode repository resolves back to repository root
        subdir = self.project_root / "поддиректория с пробелами" / "вложенная папка"
        subdir.mkdir(parents=True, exist_ok=True)
        sub_resolved = find_git_root(subdir)
        self.assertEqual(sub_resolved, self.project_root.resolve())

        # 3. File inside Unicode repository resolves back to repository root
        test_file = subdir / "файл_тест.txt"
        test_file.write_text("проверка", encoding="utf-8")
        file_resolved = find_git_root(test_file)
        self.assertEqual(file_resolved, self.project_root.resolve())

        # 4. Non-repository directory raises CollaborationError with code tool_unavailable
        non_repo_dir = Path(self.temp_dir) / "не репозиторий без git"
        non_repo_dir.mkdir(parents=True, exist_ok=True)
        with self.assertRaises(CollaborationError) as ctx:
            find_git_root(non_repo_dir)
        self.assertEqual(ctx.exception.code, "tool_unavailable")

    # -------------------------------------------------------------------------
    # 12. Symlink/Alias to Secret, Directories, and Path Escapes Rejected Without Reading
    # -------------------------------------------------------------------------
    def test_symlink_alias_to_secret_or_escape_rejected_without_reading(self):
        canary_secret = "CANARY_SECRET_NEVER_READ_TEST_998877"
        secret_file = self.project_root / ".env"
        secret_file.write_text(f"CANARY_SECRET_KEY={canary_secret}\n", encoding="utf-8")

        external_canary = Path(self.temp_dir) / "external_canary_file.txt"
        external_canary.write_text(f"CANARY_EXTERNAL={canary_secret}\n", encoding="utf-8")

        opened_paths = []
        original_open = open

        def tracking_open(f, *args, **kwargs):
            try:
                p_str = str(Path(f).resolve())
                opened_paths.append(p_str)
            except Exception:
                pass
            return original_open(f, *args, **kwargs)

        # 1. Directory rejected as artifact_ref or allowed_file
        with self.assertRaises(CollaborationError) as ctx:
            self.coordinator.create(str(uuid.uuid4()), self._sample_assignment(
                artifact_refs=["specs/001-feature"]
            ))
        self.assertEqual(ctx.exception.code, "scope_violation")

        with self.assertRaises(CollaborationError) as ctx:
            self.coordinator.create(str(uuid.uuid4()), self._sample_assignment(
                allowed_files=["src"]
            ))
        self.assertEqual(ctx.exception.code, "scope_violation")

        # 2. Symlinks: real OS symlink or deterministic mock guard
        symlinks_supported = False
        alias_artifact = self.project_root / "specs" / "001-feature" / "alias_secret.md"
        alias_allowed = self.project_root / "src" / "alias_secret.py"
        alias_escape = self.project_root / "specs" / "001-feature" / "alias_escape.md"

        try:
            os.symlink(secret_file, alias_artifact)
            os.symlink(secret_file, alias_allowed)
            os.symlink(external_canary, alias_escape)
            symlinks_supported = True
        except (OSError, NotImplementedError):
            symlinks_supported = False

        with unittest.mock.patch("builtins.open", side_effect=tracking_open):
            if symlinks_supported:
                # 2a. In-root artifact symlink alias to .env rejected
                with self.assertRaises(CollaborationError) as ctx:
                    self.coordinator.create(str(uuid.uuid4()), self._sample_assignment(
                        artifact_refs=["specs/001-feature/alias_secret.md"]
                    ))
                self.assertEqual(ctx.exception.code, "scope_violation")

                # 2b. In-root allowed_files symlink alias to .env rejected
                with self.assertRaises(CollaborationError) as ctx:
                    self.coordinator.create(str(uuid.uuid4()), self._sample_assignment(
                        allowed_files=["src/alias_secret.py"]
                    ))
                self.assertEqual(ctx.exception.code, "scope_violation")

                # 2c. Symlink escaping root in artifact_refs rejected
                with self.assertRaises(CollaborationError) as ctx:
                    self.coordinator.create(str(uuid.uuid4()), self._sample_assignment(
                        artifact_refs=["specs/001-feature/alias_escape.md"]
                    ))
                self.assertEqual(ctx.exception.code, "scope_violation")

                # 2d. canonical_scope_digest rejects secret symlink artifact without reading
                with self.assertRaises(CollaborationError) as ctx:
                    canonical_scope_digest(
                        goal="Test",
                        artifact_refs=["specs/001-feature/alias_secret.md"],
                        allowed_files=["src/allowed_app.py"],
                        allowed_actions=["file_edit"],
                        acceptance=["test"],
                        task_ids=["T001"],
                        timeout_seconds=600,
                        root_dir=self.project_root,
                    )
                self.assertEqual(ctx.exception.code, "scope_violation")

                # 2e. canonical_scope_digest rejects secret symlink allowed_files without reading
                with self.assertRaises(CollaborationError) as ctx:
                    canonical_scope_digest(
                        goal="Test",
                        artifact_refs=["specs/001-feature/spec.md"],
                        allowed_files=["src/alias_secret.py"],
                        allowed_actions=["file_edit"],
                        acceptance=["test"],
                        task_ids=["T001"],
                        timeout_seconds=600,
                        root_dir=self.project_root,
                    )
                self.assertEqual(ctx.exception.code, "scope_violation")
            else:
                # Deterministic mock guard for systems without unprivileged symlink support
                alias_artifact.write_text("# Placeholder\n- [ ] T001 Task\n", encoding="utf-8")
                alias_allowed.write_text("print('placeholder')", encoding="utf-8")
                alias_escape.write_text("placeholder escape", encoding="utf-8")

                original_resolve = Path.resolve
                target_secret_res = secret_file.resolve()
                target_external_res = external_canary.resolve()

                def selective_resolve(self, *args, **kwargs):
                    p_str = str(self).replace("\\", "/")
                    if p_str.endswith("specs/001-feature/alias_secret.md"):
                        return target_secret_res
                    if p_str.endswith("src/alias_secret.py"):
                        return target_secret_res
                    if p_str.endswith("specs/001-feature/alias_escape.md"):
                        return target_external_res
                    return original_resolve(self, *args, **kwargs)

                with unittest.mock.patch.object(Path, "resolve", new=selective_resolve):
                    # 2a. In-root artifact symlink alias to .env rejected
                    with self.assertRaises(CollaborationError) as ctx:
                        self.coordinator.create(str(uuid.uuid4()), self._sample_assignment(
                            artifact_refs=["specs/001-feature/alias_secret.md"]
                        ))
                    self.assertEqual(ctx.exception.code, "scope_violation")

                    # 2b. In-root allowed_files symlink alias to .env rejected
                    with self.assertRaises(CollaborationError) as ctx:
                        self.coordinator.create(str(uuid.uuid4()), self._sample_assignment(
                            allowed_files=["src/alias_secret.py"]
                        ))
                    self.assertEqual(ctx.exception.code, "scope_violation")

                    # 2c. Symlink escaping root in artifact_refs rejected
                    with self.assertRaises(CollaborationError) as ctx:
                        self.coordinator.create(str(uuid.uuid4()), self._sample_assignment(
                            artifact_refs=["specs/001-feature/alias_escape.md"]
                        ))
                    self.assertEqual(ctx.exception.code, "scope_violation")

                    # 2d. canonical_scope_digest rejects secret symlink artifact without reading
                    with self.assertRaises(CollaborationError) as ctx:
                        canonical_scope_digest(
                            goal="Test",
                            artifact_refs=["specs/001-feature/alias_secret.md"],
                            allowed_files=["src/allowed_app.py"],
                            allowed_actions=["file_edit"],
                            acceptance=["test"],
                            task_ids=["T001"],
                            timeout_seconds=600,
                            root_dir=self.project_root,
                        )
                    self.assertEqual(ctx.exception.code, "scope_violation")

                    # 2e. canonical_scope_digest rejects secret symlink allowed_files without reading
                    with self.assertRaises(CollaborationError) as ctx:
                        canonical_scope_digest(
                            goal="Test",
                            artifact_refs=["specs/001-feature/spec.md"],
                            allowed_files=["src/alias_secret.py"],
                            allowed_actions=["file_edit"],
                            acceptance=["test"],
                            task_ids=["T001"],
                            timeout_seconds=600,
                            root_dir=self.project_root,
                        )
                    self.assertEqual(ctx.exception.code, "scope_violation")

            # CRUCIAL ASSERTION: Neither secret_file nor external_canary was EVER opened!
            resolved_secret = str(secret_file.resolve())
            resolved_external = str(external_canary.resolve())
            self.assertNotIn(resolved_secret, opened_paths)
            self.assertNotIn(resolved_external, opened_paths)

    # -------------------------------------------------------------------------
    # 13. Durable Request Intent & Crash Recovery for Create (T014)
    # -------------------------------------------------------------------------
    def test_create_durable_intent_crash_before_commit_recovers_same_work_id(self):
        req_id = str(uuid.uuid4())
        assignment = self._sample_assignment()

        # Simulate crash before commit_mutation
        orig_commit = WorkStore.commit_mutation
        called_once = False

        def failing_commit(store_self, *args, **kwargs):
            nonlocal called_once
            if not called_once:
                called_once = True
                raise RuntimeError("Simulated crash right before commit_mutation")
            return orig_commit(store_self, *args, **kwargs)

        with unittest.mock.patch.object(WorkStore, "commit_mutation", new=failing_commit):
            with self.assertRaises(RuntimeError):
                self.coordinator.create(req_id, assignment)

        # In-progress intent file exists on disk
        req_file = self.coordinator._get_request_file(req_id)
        self.assertTrue(req_file.exists())
        with open(req_file, "r", encoding="utf-8") as f:
            intent_data = json.load(f)
        self.assertEqual(intent_data["status"], "in_progress")
        fixed_work_id = intent_data["work_id"]
        self.assertTrue(is_valid_uuid(fixed_work_id))

        # Fresh coordinator instance restarts / retries the same request
        fresh_coord = CollaborationCoordinator(project_root=self.project_root, base_dir=self.base_dir)
        resp = fresh_coord.create(req_id, assignment)

        # Succeeded with the exact same work_id!
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["work_id"], fixed_work_id)
        self.assertEqual(resp["revision"], 1)

        # Only ONE work directory was ever created
        works_dir = self.base_dir / "works"
        work_dirs = [d.name for d in works_dir.iterdir() if d.is_dir()]
        self.assertEqual(work_dirs, [fixed_work_id])

        # Request record is now completed
        with open(req_file, "r", encoding="utf-8") as f:
            completed_data = json.load(f)
        self.assertEqual(completed_data["status"], "completed")
        self.assertEqual(completed_data["response"]["work_id"], fixed_work_id)

    def test_create_durable_intent_crash_after_commit_recovers_missing_handoff(self):
        req_id = str(uuid.uuid4())
        assignment = self._sample_assignment()

        # Simulate crash during save_handoff (after commit_mutation succeeded)
        orig_save_handoff = WorkStore.save_handoff
        called_once = False

        def failing_save_handoff(store_self, *args, **kwargs):
            nonlocal called_once
            if not called_once:
                called_once = True
                raise RuntimeError("Simulated crash during save_handoff")
            return orig_save_handoff(store_self, *args, **kwargs)

        with unittest.mock.patch.object(WorkStore, "save_handoff", new=failing_save_handoff):
            with self.assertRaises(RuntimeError):
                self.coordinator.create(req_id, assignment)

        # Snapshot was written, but handoff.md is missing, request is in_progress
        req_file = self.coordinator._get_request_file(req_id)
        with open(req_file, "r", encoding="utf-8") as f:
            intent_data = json.load(f)
        self.assertEqual(intent_data["status"], "in_progress")
        fixed_work_id = intent_data["work_id"]

        store = WorkStore(base_dir=self.base_dir, work_id=fixed_work_id, checkout_root=self.project_root)
        self.assertTrue(store.snapshot_file.exists())
        self.assertFalse(store.handoff_file.exists())

        # Retry with fresh coordinator
        fresh_coord = CollaborationCoordinator(project_root=self.project_root, base_dir=self.base_dir)
        resp = fresh_coord.create(req_id, assignment)

        self.assertTrue(resp["ok"])
        self.assertEqual(resp["work_id"], fixed_work_id)
        # Handoff was recovered and exists!
        self.assertTrue(store.handoff_file.exists())
        self.assertIn("awaiting_approval", store.load_handoff())

    def test_create_durable_intent_crash_after_event_append_before_snapshot_recovers_snapshot(self):
        """
        Codex P1 fault scenario:
        Simulate crash right after event append, but before _save_snapshot_unlocked writes snapshot.json.
        The initial attempt records event in events.jsonl, but fails before snapshot.json is created.
        The second attempt with exact same request_id must NOT return false success with missing snapshot;
        it must reconstruct and save the missing snapshot.json from the trusted mutation and intent!
        """
        req_id = str(uuid.uuid4())
        assignment = self._sample_assignment()

        orig_save_snapshot = WorkStore._save_snapshot_unlocked
        called_once = False

        def failing_save_snapshot(store_self, *args, **kwargs):
            nonlocal called_once
            if not called_once:
                called_once = True
                raise RuntimeError("Simulated crash right after event append before snapshot save")
            return orig_save_snapshot(store_self, *args, **kwargs)

        with unittest.mock.patch.object(WorkStore, "_save_snapshot_unlocked", new=failing_save_snapshot):
            with self.assertRaises(RuntimeError):
                self.coordinator.create(req_id, assignment)

        # Work directory and event exist, but snapshot file is MISSING
        req_file = self.coordinator._get_request_file(req_id)
        with open(req_file, "r", encoding="utf-8") as f:
            intent_data = json.load(f)
        fixed_work_id = intent_data["work_id"]
        store = WorkStore(base_dir=self.base_dir, work_id=fixed_work_id, checkout_root=self.project_root)

        self.assertTrue(store.events_file.exists())
        self.assertFalse(store.snapshot_file.exists())

        # Retry create with same request_id
        fresh_coord = CollaborationCoordinator(project_root=self.project_root, base_dir=self.base_dir)
        resp = fresh_coord.create(req_id, assignment)

        self.assertTrue(resp["ok"])
        self.assertEqual(resp["work_id"], fixed_work_id)

        # CRITICAL ASSERTIONS: snapshot file MUST exist and be valid!
        self.assertTrue(store.snapshot_file.exists())
        self.assertTrue(store.handoff_file.exists())
        loaded_work = store.load_snapshot()
        self.assertEqual(loaded_work.work_id, fixed_work_id)
        self.assertEqual(loaded_work.revision, 1)
        self.assertEqual(loaded_work.state, "awaiting_approval")

    # -------------------------------------------------------------------------
    # 14. Durable Request Intent & Crash Recovery for Approve (T014)
    # -------------------------------------------------------------------------
    def test_approve_durable_intent_crash_before_commit_recovers_same_approval_id(self):
        req_id = str(uuid.uuid4())
        assignment = self._sample_assignment()
        create_resp = self.coordinator.create(req_id, assignment)
        work_id = create_resp["work_id"]
        scope_digest = self.coordinator.compute_scope_digest(work_id)

        decision = {
            "source": "human user",
            "approved_text": "Proceed",
            "approved_actions": ["file_edit"],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        app_req_id = str(uuid.uuid4())
        orig_commit = WorkStore.commit_mutation
        called_once = False

        def failing_commit(store_self, *args, **kwargs):
            nonlocal called_once
            if not called_once:
                called_once = True
                raise RuntimeError("Simulated crash during commit_mutation in approve")
            return orig_commit(store_self, *args, **kwargs)

        with unittest.mock.patch.object(WorkStore, "commit_mutation", new=failing_commit):
            with self.assertRaises(RuntimeError):
                self.coordinator.approve(work_id, 1, app_req_id, decision, scope_digest)

        # In-progress intent on disk
        req_file = self.coordinator._get_request_file(app_req_id)
        with open(req_file, "r", encoding="utf-8") as f:
            intent_data = json.load(f)
        self.assertEqual(intent_data["status"], "in_progress")
        fixed_app_id = intent_data["approval_id"]
        self.assertEqual(intent_data["target_revision"], 2)

        # Work snapshot is still at revision 1
        store = WorkStore(base_dir=self.base_dir, work_id=work_id, checkout_root=self.project_root)
        self.assertEqual(store.load_snapshot().revision, 1)

        # Retry approve
        fresh_coord = CollaborationCoordinator(project_root=self.project_root, base_dir=self.base_dir)
        resp = fresh_coord.approve(work_id, 1, app_req_id, decision, scope_digest)

        self.assertTrue(resp["ok"])
        self.assertEqual(resp["revision"], 2)
        work = store.load_snapshot()
        self.assertEqual(work.revision, 2)
        self.assertEqual(work.approval_id, fixed_app_id)
        self.assertEqual(store.load_approval().approval_id, fixed_app_id)

    def test_approve_durable_intent_crash_after_commit_recovers_without_revision_conflict(self):
        req_id = str(uuid.uuid4())
        assignment = self._sample_assignment()
        create_resp = self.coordinator.create(req_id, assignment)
        work_id = create_resp["work_id"]
        scope_digest = self.coordinator.compute_scope_digest(work_id)

        decision = {
            "source": "human user",
            "approved_text": "Proceed",
            "approved_actions": ["file_edit"],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        app_req_id = str(uuid.uuid4())
        orig_save_approval = WorkStore.save_approval
        called_once = False

        def failing_save_approval(store_self, *args, **kwargs):
            nonlocal called_once
            if not called_once:
                called_once = True
                raise RuntimeError("Simulated crash right after commit_mutation, before save_approval")
            return orig_save_approval(store_self, *args, **kwargs)

        with unittest.mock.patch.object(WorkStore, "save_approval", new=failing_save_approval):
            with self.assertRaises(RuntimeError):
                self.coordinator.approve(work_id, 1, app_req_id, decision, scope_digest)

        store = WorkStore(base_dir=self.base_dir, work_id=work_id, checkout_root=self.project_root)
        # Mutation was committed in snapshot (revision 2), but approval.json is missing!
        self.assertEqual(store.load_snapshot().revision, 2)
        self.assertFalse(store.approval_file.exists())

        # CRITICAL TEST: Caller retries with expected_revision=1!
        # In vulnerable code, this failed with revision_conflict (expected 1, actual 2)
        # and handoff was left stale at Revision 1 awaiting_approval.
        # With durable request intent, it recovers, saves missing approval.json, updates handoff
        # to Revision 2 approved_awaiting_start, and returns revision 2!
        fresh_coord = CollaborationCoordinator(project_root=self.project_root, base_dir=self.base_dir)
        resp = fresh_coord.approve(work_id, 1, app_req_id, decision, scope_digest)

        self.assertTrue(resp["ok"])
        self.assertEqual(resp["revision"], 2)
        self.assertTrue(store.approval_file.exists())
        self.assertTrue(store.handoff_file.exists())
        handoff_md = store.load_handoff()
        self.assertIn("**Revision:** 2", handoff_md)
        self.assertIn("approved_awaiting_start", handoff_md)

    def test_replay_completed_approve_when_work_progressed(self):
        req_id = str(uuid.uuid4())
        assignment = self._sample_assignment()
        create_resp = self.coordinator.create(req_id, assignment)
        work_id = create_resp["work_id"]
        scope_digest = self.coordinator.compute_scope_digest(work_id)

        decision = {
            "source": "human user",
            "approved_text": "Proceed",
            "approved_actions": ["file_edit"],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        app_req_id = str(uuid.uuid4())
        orig_resp = self.coordinator.approve(work_id, 1, app_req_id, decision, scope_digest)
        self.assertEqual(orig_resp["revision"], 2)

        # Simulate subsequent work progress: snapshot advanced to revision 3
        store = WorkStore(base_dir=self.base_dir, work_id=work_id, checkout_root=self.project_root)
        work = store.load_snapshot()
        work.revision = 3
        store.commit_mutation(work=work, event_type="work_started", payload={"status": "in_progress"})
        self.assertEqual(store.load_snapshot().revision, 3)

        # Replaying the completed approve request with expected_revision=1 MUST succeed
        # and return the original response without raising revision_conflict
        replay_resp = self.coordinator.approve(work_id, 1, app_req_id, decision, scope_digest)
        self.assertEqual(replay_resp, orig_resp)

    def test_request_conflict_cross_operation_and_payload_under_intent(self):
        # 1. Same request_id across create and approve
        req_id = str(uuid.uuid4())
        assignment = self._sample_assignment()
        create_resp = self.coordinator.create(req_id, assignment)
        work_id = create_resp["work_id"]
        scope_digest = self.coordinator.compute_scope_digest(work_id)

        decision = {
            "source": "human user",
            "approved_text": "Proceed",
            "approved_actions": ["file_edit"],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        # Attempting approve using the create's request_id must raise request_conflict
        with self.assertRaises(CollaborationError) as ctx:
            self.coordinator.approve(work_id, 1, req_id, decision, scope_digest)
        self.assertEqual(ctx.exception.code, "request_conflict")

        # 2. Same approve request_id with different decision or expected_revision
        app_req_id = str(uuid.uuid4())
        self.coordinator.approve(work_id, 1, app_req_id, decision, scope_digest)

        diff_decision = dict(decision)
        diff_decision["approved_text"] = "Different text"
        with self.assertRaises(CollaborationError) as ctx:
            self.coordinator.approve(work_id, 1, app_req_id, diff_decision, scope_digest)
        self.assertEqual(ctx.exception.code, "request_conflict")

    # -------------------------------------------------------------------------
    # 15. Event-Tail / Snapshot Revision Mismatch & Malformed Records Failsafe (T014)
    # -------------------------------------------------------------------------
    def test_event_tail_snapshot_revision_mismatch_failsafe_explicit(self):
        req_id = str(uuid.uuid4())
        assignment = self._sample_assignment()
        create_resp = self.coordinator.create(req_id, assignment)
        work_id = create_resp["work_id"]
        store = WorkStore(base_dir=self.base_dir, work_id=work_id, checkout_root=self.project_root)

        # Inject event without snapshot save (e.g. event appended at revision 2, but snapshot at rev 1)
        store.append_event("test_event", {"info": "tail event"}, revision=2)

        # load_snapshot() MUST raise corruption_detected failsafe explicit
        with self.assertRaises(CollaborationError) as ctx:
            store.load_snapshot()
        self.assertEqual(ctx.exception.code, "corruption_detected")
        self.assertIn("does not match latest event revision", ctx.exception.message)

    def test_malformed_request_records_rejected_safely(self):
        req_id = str(uuid.uuid4())
        req_file = self.coordinator._get_request_file(req_id)

        # 1. Corrupted non-JSON
        req_file.write_text("{not-valid-json", encoding="utf-8")
        with self.assertRaises(CollaborationError) as ctx:
            self.coordinator.create(req_id, self._sample_assignment())
        self.assertEqual(ctx.exception.code, "corruption_detected")

        # 2. JSON array instead of dict
        req_file.write_text("[]", encoding="utf-8")
        with self.assertRaises(CollaborationError) as ctx:
            self.coordinator.create(req_id, self._sample_assignment())
        self.assertEqual(ctx.exception.code, "corruption_detected")

        # 3. Invalid operation
        req_file.write_text(json.dumps({
            "operation": "unknown_op",
            "payload_digest": "a" * 64,
            "status": "in_progress",
            "work_id": str(uuid.uuid4()),
        }), encoding="utf-8")
        with self.assertRaises(CollaborationError) as ctx:
            self.coordinator.create(req_id, self._sample_assignment())
        self.assertEqual(ctx.exception.code, "corruption_detected")

        # 4. Completed status but missing response dict (must raise corruption_detected, NEVER KeyError!)
        req_file.write_text(json.dumps({
            "operation": "create",
            "payload_digest": "a" * 64,
            "status": "completed",
            "work_id": str(uuid.uuid4()),
            # "response" key is missing
        }), encoding="utf-8")
        with self.assertRaises(CollaborationError) as ctx:
            self.coordinator.create(req_id, self._sample_assignment())
        self.assertEqual(ctx.exception.code, "corruption_detected")
        self.assertIn("response", ctx.exception.message.lower())

        # 5. Invalid work_id (not UUID)
        req_file.write_text(json.dumps({
            "operation": "create",
            "payload_digest": "a" * 64,
            "status": "in_progress",
            "work_id": "not-a-valid-uuid",
        }), encoding="utf-8")
        with self.assertRaises(CollaborationError) as ctx:
            self.coordinator.create(req_id, self._sample_assignment())
        self.assertEqual(ctx.exception.code, "corruption_detected")

    # -------------------------------------------------------------------------
    # 16. Concurrent Requests Thread Serialization (T014)
    # -------------------------------------------------------------------------
    def test_concurrent_same_create_and_approve_requests_serialized(self):
        import threading

        same_create_req_id = str(uuid.uuid4())
        assignment = self._sample_assignment()

        results = []
        errors = []

        def worker_create():
            try:
                # Each thread gets its own coordinator instance pointing to the same repository
                thread_coord = CollaborationCoordinator(project_root=self.project_root, base_dir=self.base_dir)
                resp = thread_coord.create(same_create_req_id, assignment)
                results.append(resp)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker_create) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(errors), 0, f"Errors in concurrent create: {errors}")
        self.assertEqual(len(results), 5)

        # All threads must receive identical responses and the exact same work_id
        first_resp = results[0]
        work_id = first_resp["work_id"]
        for r in results[1:]:
            self.assertEqual(r, first_resp)

        # Exactly 1 work directory and 1 event seq
        store = WorkStore(base_dir=self.base_dir, work_id=work_id, checkout_root=self.project_root)
        self.assertEqual(store.get_latest_event_seq(), 1)
        works_dir = self.base_dir / "works"
        work_dirs = [d.name for d in works_dir.iterdir() if d.is_dir()]
        self.assertEqual(work_dirs, [work_id])

        # Concurrent approve
        scope_digest = self.coordinator.compute_scope_digest(work_id)
        decision = {
            "source": "human user",
            "approved_text": "Proceed concurrent",
            "approved_actions": ["file_edit"],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        same_app_req_id = str(uuid.uuid4())

        app_results = []
        app_errors = []

        def worker_approve():
            try:
                thread_coord = CollaborationCoordinator(project_root=self.project_root, base_dir=self.base_dir)
                resp = thread_coord.approve(work_id, 1, same_app_req_id, decision, scope_digest)
                app_results.append(resp)
            except Exception as e:
                app_errors.append(e)

        app_threads = [threading.Thread(target=worker_approve) for _ in range(5)]
        for t in app_threads:
            t.start()
        for t in app_threads:
            t.join()

        self.assertEqual(len(app_errors), 0, f"Errors in concurrent approve: {app_errors}")
        self.assertEqual(len(app_results), 5)

        first_app_resp = app_results[0]
        self.assertEqual(first_app_resp["revision"], 2)
        for r in app_results[1:]:
            self.assertEqual(r, first_app_resp)

        # Exactly 2 events in events.jsonl
        self.assertEqual(store.get_latest_event_seq(), 2)
        work = store.load_snapshot()
        self.assertEqual(work.revision, 2)
        self.assertIsNotNone(work.approval_id)

    # -------------------------------------------------------------------------
    # 17. Strict Task Definitions in Explicit Tasks Artifact (P2)
    # -------------------------------------------------------------------------
    def test_assignment_missing_explicit_tasks_artifact_rejected(self):
        req_id = str(uuid.uuid4())
        assignment = self._sample_assignment(artifact_refs=["specs/001-feature/spec.md"])
        with self.assertRaises(CollaborationError) as ctx:
            self.coordinator.create(req_id, assignment)
        self.assertEqual(ctx.exception.code, "protocol_error")
        self.assertIn("explicit tasks artifact", ctx.exception.message.lower())

    def test_assignment_reference_only_task_id_rejected(self):
        # tasks.md only defines T001 and T002 with checkboxes; T999 is only referenced in free text
        self.tasks_file.write_text(
            "# Feature 001 Tasks\n"
            "- [ ] T001 First task checkbox\n"
            "- [ ] T002 Second task checkbox\n"
            "Note: future T999 is a planned task but has no checkbox line.\n",
            encoding="utf-8"
        )
        req_id = str(uuid.uuid4())
        bad_assignment = self._sample_assignment(task_ids=["T001", "T999"])
        with self.assertRaises(CollaborationError) as ctx:
            self.coordinator.create(req_id, bad_assignment)
        self.assertEqual(ctx.exception.code, "protocol_error")
        self.assertIn("T999", ctx.exception.message)
        self.assertIn("not defined in referenced tasks artifact", ctx.exception.message)

    def test_assignment_tasks_artifact_read_failure_or_invalid_utf8_rejected(self):
        # Write invalid UTF-8 bytes to tasks.md
        with open(self.tasks_file, "wb") as f:
            f.write(b"# Corrupt tasks\n\xff\xfe\x00\x00\n- [ ] T001 task\n")
        req_id = str(uuid.uuid4())
        with self.assertRaises(CollaborationError) as ctx:
            self.coordinator.create(req_id, self._sample_assignment())
        self.assertEqual(ctx.exception.code, "protocol_error")
        self.assertIn("utf-8", ctx.exception.message.lower())

    def test_assignment_real_checkbox_definitions_valid(self):
        # Both unchecked [ ] and checked [x] / [X] SpecKit checkbox lines count as defined
        self.tasks_file.write_text(
            "# Feature 001 Tasks\n"
            "- [ ] T001 Open task\n"
            "- [x] T002 Checked task lower\n"
            "- [X] T003 Checked task upper\n",
            encoding="utf-8"
        )
        req_id = str(uuid.uuid4())
        assignment = self._sample_assignment(task_ids=["T001", "T002", "T003"])
        resp = self.coordinator.create(req_id, assignment)
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["revision"], 1)

    # -------------------------------------------------------------------------
    # 18. Strict Human Approval Decision & Unredacted Payload Hashing (P2)
    # -------------------------------------------------------------------------
    def test_approve_missing_timestamp_rejected(self):
        req_id = str(uuid.uuid4())
        create_resp = self.coordinator.create(req_id, self._sample_assignment())
        work_id = create_resp["work_id"]
        scope_digest = self.coordinator.compute_scope_digest(work_id)

        decision = {
            "source": "human user",
            "approved_text": "Proceed",
            "approved_actions": ["file_edit"],
            # Missing timestamp
        }
        with self.assertRaises(CollaborationError) as ctx:
            self.coordinator.approve(work_id, 1, str(uuid.uuid4()), decision, scope_digest)
        self.assertEqual(ctx.exception.code, "protocol_error")
        self.assertIn("timestamp", ctx.exception.message.lower())

    def test_approve_malformed_timestamp_rejected(self):
        req_id = str(uuid.uuid4())
        create_resp = self.coordinator.create(req_id, self._sample_assignment())
        work_id = create_resp["work_id"]
        scope_digest = self.coordinator.compute_scope_digest(work_id)

        decision = {
            "source": "human user",
            "approved_text": "Proceed",
            "approved_actions": ["file_edit"],
            "timestamp": "not-an-iso-8601-timestamp",
        }
        with self.assertRaises(CollaborationError) as ctx:
            self.coordinator.approve(work_id, 1, str(uuid.uuid4()), decision, scope_digest)
        self.assertEqual(ctx.exception.code, "protocol_error")
        self.assertIn("iso 8601", ctx.exception.message.lower())

    def test_approve_naive_timestamp_rejected(self):
        req_id = str(uuid.uuid4())
        create_resp = self.coordinator.create(req_id, self._sample_assignment())
        work_id = create_resp["work_id"]
        scope_digest = self.coordinator.compute_scope_digest(work_id)

        decision = {
            "source": "human user",
            "approved_text": "Proceed",
            "approved_actions": ["file_edit"],
            "timestamp": "2026-10-04T02:30:00",  # Naive datetime without timezone
        }
        with self.assertRaises(CollaborationError) as ctx:
            self.coordinator.approve(work_id, 1, str(uuid.uuid4()), decision, scope_digest)
        self.assertEqual(ctx.exception.code, "protocol_error")
        self.assertIn("timezone-aware", ctx.exception.message.lower())

    def test_approve_bot_source_rejected(self):
        req_id = str(uuid.uuid4())
        create_resp = self.coordinator.create(req_id, self._sample_assignment())
        work_id = create_resp["work_id"]
        scope_digest = self.coordinator.compute_scope_digest(work_id)

        decision = {
            "source": "arbitrary_custom_bot",
            "approved_text": "Proceed",
            "approved_actions": ["file_edit"],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        with self.assertRaises(CollaborationError) as ctx:
            self.coordinator.approve(work_id, 1, str(uuid.uuid4()), decision, scope_digest)
        self.assertEqual(ctx.exception.code, "permission_denied")
        self.assertIn("cannot be arbitrary_custom_bot", ctx.exception.message.lower())

    def test_approve_excess_actions_rejected(self):
        req_id = str(uuid.uuid4())
        # Assignment only allows ["file_edit"]
        create_resp = self.coordinator.create(req_id, self._sample_assignment(allowed_actions=["file_edit"]))
        work_id = create_resp["work_id"]
        scope_digest = self.coordinator.compute_scope_digest(work_id)

        # 1. Excess action from VALID_ACTION_KINDS ('command_run' is a valid action kind, but not in work's allowed_actions)
        decision = {
            "source": "human user",
            "approved_text": "Proceed with expanded scope",
            "approved_actions": ["file_edit", "command_run"],  # command_run is excess beyond assignment
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        with self.assertRaises(CollaborationError) as ctx:
            self.coordinator.approve(work_id, 1, str(uuid.uuid4()), decision, scope_digest)
        self.assertEqual(ctx.exception.code, "permission_denied")
        self.assertIn("excess", ctx.exception.message.lower())

        # 2. Invalid action kind not in VALID_ACTION_KINDS raises protocol_error
        invalid_kind_decision = {
            "source": "human user",
            "approved_text": "Proceed with invalid kind",
            "approved_actions": ["file_edit", "invalid_custom_action_kind"],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        with self.assertRaises(CollaborationError) as ctx:
            self.coordinator.approve(work_id, 1, str(uuid.uuid4()), invalid_kind_decision, scope_digest)
        self.assertEqual(ctx.exception.code, "protocol_error")

    def test_approve_secret_in_decision_rejected_and_never_leaked_or_approved(self):
        req_id = str(uuid.uuid4())
        create_resp = self.coordinator.create(req_id, self._sample_assignment())
        work_id = create_resp["work_id"]
        scope_digest = self.coordinator.compute_scope_digest(work_id)

        app_req_id = str(uuid.uuid4())
        ts = datetime.now(timezone.utc).isoformat()
        canary_secret_1 = "CANARY_SECRET_DECISION_112233"
        canary_secret_2 = "CANARY_SECRET_DECISION_998877"

        # Readonly unit assertion: actual unmasked payload hashing yields distinct digests
        p1 = {"approved_text": f"Proceed {canary_secret_1}"}
        p2 = {"approved_text": f"Proceed {canary_secret_2}"}
        self.assertNotEqual(self.coordinator._compute_payload_digest(p1), self.coordinator._compute_payload_digest(p2))

        decision_sec_1 = {
            "source": "human user",
            "approved_text": f"Proceed with {canary_secret_1}",
            "approved_actions": ["file_edit"],
            "timestamp": ts,
        }
        decision_sec_2 = {
            "source": "human user",
            "approved_text": f"Proceed with {canary_secret_2}",
            "approved_actions": ["file_edit"],
            "timestamp": ts,
        }

        # 1. Both secret decisions are safely REJECTED with sensitive_output_blocked
        with self.assertRaises(CollaborationError) as ctx1:
            self.coordinator.approve(work_id, 1, app_req_id, decision_sec_1, scope_digest)
        self.assertEqual(ctx1.exception.code, "sensitive_output_blocked")

        with self.assertRaises(CollaborationError) as ctx2:
            self.coordinator.approve(work_id, 1, app_req_id, decision_sec_2, scope_digest)
        self.assertEqual(ctx2.exception.code, "sensitive_output_blocked")

        # 2. Work remains completely unapproved: revision 1, approval_id is None
        store = WorkStore(base_dir=self.base_dir, work_id=work_id, checkout_root=self.project_root)
        work = store.load_snapshot()
        self.assertEqual(work.revision, 1)
        self.assertIsNone(work.approval_id)
        self.assertEqual(work.state, "awaiting_approval")
        self.assertFalse(store.approval_file.exists())

        # 3. Request record was never marked completed
        req_file = self.coordinator._get_request_file(app_req_id)
        if req_file.exists():
            with open(req_file, "r", encoding="utf-8") as f:
                req_data = json.load(f)
            self.assertNotEqual(req_data.get("status"), "completed")

        # 4. Zero secret content leaked in logs, request files, or events
        requests_dir = self.coordinator.requests_dir
        for p in requests_dir.glob("*"):
            if p.is_file():
                content = p.read_text(encoding="utf-8", errors="replace")
                self.assertNotIn(canary_secret_1, content)
                self.assertNotIn(canary_secret_2, content)

        if store.events_file.exists():
            content = store.events_file.read_text(encoding="utf-8", errors="replace")
            self.assertNotIn(canary_secret_1, content)
            self.assertNotIn(canary_secret_2, content)

    def test_approve_payload_conflict_on_different_safe_text_same_request_id(self):
        req_id = str(uuid.uuid4())
        create_resp = self.coordinator.create(req_id, self._sample_assignment())
        work_id = create_resp["work_id"]
        scope_digest = self.coordinator.compute_scope_digest(work_id)

        app_req_id = str(uuid.uuid4())
        ts = datetime.now(timezone.utc).isoformat()
        decision_safe_1 = {
            "source": "human user",
            "approved_text": "Proceed with initial task implementation",
            "approved_actions": ["file_edit"],
            "timestamp": ts,
        }
        # First approval succeeds
        resp_1 = self.coordinator.approve(work_id, 1, app_req_id, decision_safe_1, scope_digest)
        self.assertTrue(resp_1["ok"])
        self.assertEqual(resp_1["revision"], 2)

        # Second approval: same app_req_id, but semantically different SAFE approved_text
        decision_safe_2 = {
            "source": "human user",
            "approved_text": "Proceed with alternative scope implementation",
            "approved_actions": ["file_edit"],
            "timestamp": ts,
        }
        # Conflicting payload under same request_id raises request_conflict
        with self.assertRaises(CollaborationError) as ctx:
            self.coordinator.approve(work_id, 1, app_req_id, decision_safe_2, scope_digest)
        self.assertEqual(ctx.exception.code, "request_conflict")

        # Exactly one approval was persisted
        store = WorkStore(base_dir=self.base_dir, work_id=work_id, checkout_root=self.project_root)
        work = store.load_snapshot()
        self.assertEqual(work.revision, 2)
        approval = store.load_approval()
        self.assertEqual(approval.approved_text, "Proceed with initial task implementation")


if __name__ == "__main__":
    unittest.main()
