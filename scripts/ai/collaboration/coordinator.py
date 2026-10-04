"""
CollaborationCoordinator: Unified control seam for offline assignment creation and human approval
(Feature 007, Tasks T012 partial + T014).
"""

import os
import sys
import json
import time
import uuid
import re
import hashlib
import threading
import math
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Any, Tuple, Set, Union

from .state import (
    Work,
    Approval,
    Handoff,
    Turn,
    QuotaNotice,
    QuotaSnapshot,
    WorkStore,
    FileLock,
    CollaborationError,
    SCHEMA_VERSION,
    DEFAULT_TIMEOUT_SECONDS,
    VALID_WORK_STATES,
    VALID_ACTION_KINDS,
    VALID_HUMAN_SOURCES,
    is_valid_uuid,
    validate_uuid,
    validate_strict_int,
    validate_non_empty_str,
    validate_safe_relative_path,
    validate_strict_keys,
    is_secret_path,
    canonical_scope_digest,
    compute_baseline,
    find_git_root,
    sanitize_safe_output,
    is_process_alive,
    get_process_creation_time,
    verify_scope_integrity,
)
from .worker import save_executor_json_schema
from .environment import QuotaAdapter, discover_executable


def write_atomic_json(target_path: Path, data: Dict[str, Any]):
    target_path = Path(target_path)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    safe_data = sanitize_safe_output(data)
    temp_path = target_path.parent / f".tmp_{uuid.uuid4().hex}_{target_path.name}"
    try:
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(safe_data, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, target_path)
    finally:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except Exception:
                pass


def validate_assignment(project_root: Path, assignment: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(assignment, dict):
        raise CollaborationError("protocol_error", "Assignment must be a dictionary")

    validate_strict_keys(assignment, {
        "goal", "artifact_refs", "allowed_files", "allowed_actions",
        "acceptance", "task_ids", "timeout_seconds"
    }, "Assignment")

    for req in ("goal", "artifact_refs", "allowed_files", "allowed_actions", "acceptance", "task_ids"):
        if req not in assignment:
            raise CollaborationError("protocol_error", f"Missing required assignment field: '{req}'")

    goal = validate_non_empty_str(assignment.get("goal"), "goal")

    timeout_seconds = assignment.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
    timeout_seconds = validate_strict_int(timeout_seconds, "timeout_seconds", min_val=1)

    raw_actions = assignment.get("allowed_actions")
    if not isinstance(raw_actions, list) or not raw_actions:
        raise CollaborationError("protocol_error", "allowed_actions must be a non-empty list")
    allowed_actions = []
    for a in raw_actions:
        clean_a = validate_non_empty_str(a, "allowed_actions item")
        if clean_a not in VALID_ACTION_KINDS:
            raise CollaborationError("protocol_error", f"Invalid action kind '{clean_a}' in allowed_actions")
        allowed_actions.append(clean_a)

    raw_acceptance = assignment.get("acceptance")
    if not isinstance(raw_acceptance, list) or not raw_acceptance:
        raise CollaborationError("protocol_error", "acceptance must be a non-empty list")
    acceptance = [sanitize_safe_output(validate_non_empty_str(acc, "acceptance item")) for acc in raw_acceptance]

    raw_artifacts = assignment.get("artifact_refs")
    if not isinstance(raw_artifacts, list) or not raw_artifacts:
        raise CollaborationError("protocol_error", "artifact_refs must be a non-empty list")
    artifact_refs = []
    for ref in raw_artifacts:
        clean_ref = validate_safe_relative_path(ref, "artifact_refs item")
        if is_secret_path(clean_ref):
            raise CollaborationError("scope_violation", f"Artifact ref cannot be a secret file: '{clean_ref}'")
        abs_ref = project_root / clean_ref
        try:
            if not abs_ref.exists():
                raise CollaborationError("scope_violation", f"Referenced artifact does not exist: '{clean_ref}'")
            if abs_ref.is_dir():
                raise CollaborationError("scope_violation", f"Referenced artifact cannot be a directory: '{clean_ref}'")
            resolved_ref = abs_ref.resolve()
        except CollaborationError:
            raise
        except Exception:
            raise CollaborationError("scope_violation", f"Referenced artifact path resolution failed: '{clean_ref}'")

        try:
            if not resolved_ref.is_relative_to(project_root):
                raise CollaborationError("scope_violation", f"Referenced artifact escapes repository root: '{clean_ref}'")
            rel_resolved = str(resolved_ref.relative_to(project_root)).replace("\\", "/")
        except (ValueError, CollaborationError):
            raise CollaborationError("scope_violation", f"Referenced artifact escapes repository root: '{clean_ref}'")

        if not resolved_ref.is_file() or resolved_ref.is_dir():
            raise CollaborationError("scope_violation", f"Referenced artifact must resolve to a regular file: '{clean_ref}'")

        if is_secret_path(rel_resolved):
            raise CollaborationError("scope_violation", f"Referenced artifact resolves to a secret path")

        artifact_refs.append(clean_ref)

    raw_files = assignment.get("allowed_files")
    if not isinstance(raw_files, list) or not raw_files:
        raise CollaborationError("protocol_error", "allowed_files must be a non-empty list")
    allowed_files = []
    for f in raw_files:
        clean_f = validate_safe_relative_path(f, "allowed_files item")
        if is_secret_path(clean_f):
            raise CollaborationError("scope_violation", f"Allowed file cannot be a secret file: '{clean_f}'")
        abs_f = project_root / clean_f
        if abs_f.exists():
            try:
                if abs_f.is_dir():
                    raise CollaborationError("scope_violation", f"Allowed file cannot be a directory: '{clean_f}'")
                resolved_f = abs_f.resolve()
                if not resolved_f.is_relative_to(project_root):
                    raise CollaborationError("scope_violation", f"Allowed file escapes repository root: '{clean_f}'")
                if resolved_f.is_dir():
                    raise CollaborationError("scope_violation", f"Allowed file resolves to a directory: '{clean_f}'")
                rel_resolved = str(resolved_f.relative_to(project_root)).replace("\\", "/")
                if is_secret_path(rel_resolved):
                    raise CollaborationError("scope_violation", f"Allowed file resolves to a secret path")
            except CollaborationError:
                raise
            except Exception:
                raise CollaborationError("scope_violation", f"Allowed file path resolution failed: '{clean_f}'")
        else:
            # Missing new file is permissible if parent is valid and not secret
            parent = abs_f.parent
            try:
                resolved_parent = parent.resolve()
                if not resolved_parent.is_relative_to(project_root):
                    raise CollaborationError("scope_violation", f"Allowed file parent directory escapes root: '{clean_f}'")
                rel_parent = str(resolved_parent.relative_to(project_root)).replace("\\", "/")
                if is_secret_path(rel_parent):
                    raise CollaborationError("scope_violation", f"Allowed file parent directory is a secret path: '{clean_f}'")
            except CollaborationError:
                raise
            except Exception:
                raise CollaborationError("scope_violation", f"Allowed file parent directory resolution failed: '{clean_f}'")
        allowed_files.append(clean_f)

    raw_task_ids = assignment.get("task_ids")
    if not isinstance(raw_task_ids, list) or not raw_task_ids:
        raise CollaborationError("protocol_error", "task_ids must be a non-empty list")
    task_ids = [validate_non_empty_str(t, "task_ids item") for t in raw_task_ids]

    # Validate task_ids against referenced tasks artifact
    known_task_ids: Set[str] = set()
    tasks_artifacts = [r for r in artifact_refs if Path(r).name.lower() in ("tasks.md", "tasks.markdown")]
    if not tasks_artifacts:
        raise CollaborationError("protocol_error", "Assignment artifact_refs must include an explicit tasks artifact ('tasks.md')")

    task_checkbox_pattern = re.compile(r"^[\t ]*- \[(?: |[xX])\][\t ]+(T\d{3,4})\b", re.MULTILINE)

    for art_ref in tasks_artifacts:
        art_path = project_root / art_ref
        try:
            with open(art_path, "r", encoding="utf-8") as af:
                content = af.read()
        except UnicodeDecodeError as e:
            raise CollaborationError("protocol_error", f"Tasks artifact '{art_ref}' is not valid UTF-8: {e}")
        except Exception as e:
            raise CollaborationError("protocol_error", f"Failed to read tasks artifact '{art_ref}': {e}")

        found = set(task_checkbox_pattern.findall(content))
        known_task_ids.update(found)

    for tid in task_ids:
        if tid not in known_task_ids:
            raise CollaborationError("protocol_error", f"Task ID '{tid}' is not defined in referenced tasks artifact")

    return {
        "goal": goal,
        "artifact_refs": sorted(artifact_refs),
        "allowed_files": sorted(allowed_files),
        "allowed_actions": sorted(allowed_actions),
        "acceptance": acceptance,
        "task_ids": sorted(task_ids),
        "timeout_seconds": timeout_seconds,
    }


class CollaborationCoordinator:
    """
    Central coordinator seam for collaboration operations (create, approve, etc.).
    Enforces atomic requests, serialization locks, scope baseline validation,
    and contractual safe responses.
    """
    def __init__(
        self,
        project_root: Optional[Path] = None,
        base_dir: Optional[Path] = None,
    ):
        self.project_root = find_git_root(project_root or Path.cwd())
        self.base_dir = (Path(base_dir).resolve() if base_dir else (self.project_root / "logs" / "ai" / "collaboration")).resolve()
        self.requests_dir = self.base_dir / "requests"
        self._thread_lock = threading.RLock()
        self.ensure_dir()

    def ensure_dir(self):
        self.requests_dir.mkdir(parents=True, exist_ok=True)

    def _get_request_lock(self, request_id: str) -> FileLock:
        lock_file = self.requests_dir / f".lock_{request_id}"
        return FileLock(lock_file)

    def _get_request_file(self, request_id: str) -> Path:
        return self.requests_dir / f"{request_id}.json"

    def _compute_payload_digest(self, payload: Dict[str, Any]) -> str:
        # Hash actual payload directly without redaction masking so different free-text payloads produce different digests
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _load_request_record(self, req_file: Path, clean_req_id: str) -> Optional[Dict[str, Any]]:
        if not req_file.exists():
            return None
        try:
            with open(req_file, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            raise CollaborationError("corruption_detected", f"Corrupted request record for {clean_req_id}")

        if not isinstance(data, dict):
            raise CollaborationError("corruption_detected", f"Request record must be a dict for {clean_req_id}")

        # Strict checks on record fields
        op = data.get("operation")
        if op not in ("create", "approve", "start"):
            raise CollaborationError("corruption_detected", f"Invalid operation in request record for {clean_req_id}: {op}")

        p_digest = data.get("payload_digest")
        if not isinstance(p_digest, str) or len(p_digest) != 64:
            raise CollaborationError("corruption_detected", f"Invalid payload_digest in request record for {clean_req_id}")

        status = data.get("status")
        if not status and "response" in data:
            status = "completed"
            data["status"] = status
        if status not in ("in_progress", "completed"):
            raise CollaborationError("corruption_detected", f"Invalid status in request record for {clean_req_id}: {status}")

        work_id = data.get("work_id")
        if work_id is not None and not is_valid_uuid(work_id):
            raise CollaborationError("corruption_detected", f"Invalid work_id in request record for {clean_req_id}")

        approval_id = data.get("approval_id")
        if approval_id is not None and not is_valid_uuid(approval_id):
            raise CollaborationError("corruption_detected", f"Invalid approval_id in request record for {clean_req_id}")

        turn_id = data.get("turn_id")
        if turn_id is not None and not is_valid_uuid(turn_id):
            raise CollaborationError("corruption_detected", f"Invalid turn_id in request record for {clean_req_id}")

        worker_token = data.get("worker_token")
        if worker_token is not None and not isinstance(worker_token, str):
            raise CollaborationError("corruption_detected", f"Invalid worker_token in request record for {clean_req_id}")

        target_rev = data.get("target_revision")
        if target_rev is not None and (not isinstance(target_rev, int) or isinstance(target_rev, bool) or target_rev < 1):
            raise CollaborationError("corruption_detected", f"Invalid target_revision in request record for {clean_req_id}")

        if status == "completed":
            resp = data.get("response")
            if not isinstance(resp, dict):
                raise CollaborationError("corruption_detected", f"Missing or invalid response in completed request record for {clean_req_id}")
            required_resp_keys = {"schema_version", "ok", "work_id", "revision", "state", "execution_known", "next_action"}
            if not required_resp_keys.issubset(set(resp.keys())):
                raise CollaborationError("corruption_detected", f"Missing required response fields in request record for {clean_req_id}")
            if not is_valid_uuid(resp.get("work_id")):
                raise CollaborationError("corruption_detected", f"Invalid work_id in response for {clean_req_id}")
            if not isinstance(resp.get("revision"), int) or isinstance(resp.get("revision"), bool) or resp.get("revision") < 1:
                raise CollaborationError("corruption_detected", f"Invalid revision in response for {clean_req_id}")
            if resp.get("state") not in VALID_WORK_STATES:
                raise CollaborationError("corruption_detected", f"Invalid state in response for {clean_req_id}")

        return data

    def compute_scope_digest(self, assignment_or_work_id: Any) -> str:
        if isinstance(assignment_or_work_id, str):
            clean_work_id = validate_uuid(assignment_or_work_id, "work_id")
            store = WorkStore(base_dir=self.base_dir, work_id=clean_work_id, checkout_root=self.project_root)
            work = store.load_snapshot()
            return canonical_scope_digest(
                goal=work.goal,
                artifact_refs=work.artifact_refs,
                allowed_files=work.allowed_files,
                allowed_actions=work.allowed_actions,
                acceptance=work.acceptance,
                task_ids=work.task_ids,
                timeout_seconds=work.timeout_seconds,
                root_dir=self.project_root,
            )
        elif isinstance(assignment_or_work_id, dict):
            validated = validate_assignment(self.project_root, assignment_or_work_id)
            return canonical_scope_digest(
                goal=validated["goal"],
                artifact_refs=validated["artifact_refs"],
                allowed_files=validated["allowed_files"],
                allowed_actions=validated["allowed_actions"],
                acceptance=validated["acceptance"],
                task_ids=validated["task_ids"],
                timeout_seconds=validated["timeout_seconds"],
                root_dir=self.project_root,
            )
        else:
            raise CollaborationError("protocol_error", "Argument must be work_id or assignment dict")

    def create(self, request_id: str, assignment: Dict[str, Any]) -> Dict[str, Any]:
        clean_req_id = validate_uuid(request_id, "request_id")
        validated_assignment = validate_assignment(self.project_root, assignment)
        payload_digest = self._compute_payload_digest(validated_assignment)

        with self._thread_lock:
            with self._get_request_lock(clean_req_id):
                req_file = self._get_request_file(clean_req_id)
                saved_record = self._load_request_record(req_file, clean_req_id)

                if saved_record is not None:
                    if saved_record.get("payload_digest") != payload_digest or saved_record.get("operation") != "create":
                        raise CollaborationError(
                            "request_conflict",
                            f"Duplicate request_id '{clean_req_id}' with different payload or operation"
                        )
                    if saved_record["status"] == "completed":
                        return saved_record["response"]

                    work_id = saved_record["work_id"]
                    created_at = saved_record.get("created_at") or datetime.now(timezone.utc).isoformat()
                    # Re-use the trusted baseline saved at intent creation time (never an altered fresh baseline)
                    baseline = saved_record.get("baseline")
                    if not baseline:
                        baseline = compute_baseline(self.project_root, validated_assignment["allowed_files"])
                else:
                    work_id = str(uuid.uuid4())
                    created_at = datetime.now(timezone.utc).isoformat()
                    baseline = compute_baseline(self.project_root, validated_assignment["allowed_files"])
                    intent_record = {
                        "request_id": clean_req_id,
                        "operation": "create",
                        "payload_digest": payload_digest,
                        "work_id": work_id,
                        "target_revision": 1,
                        "baseline": baseline,
                        "status": "in_progress",
                        "created_at": created_at,
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                    }
                    write_atomic_json(req_file, intent_record)

                store = WorkStore(base_dir=self.base_dir, work_id=work_id, checkout_root=self.project_root)
                with store.lock():
                    if store.snapshot_file.exists():
                        work = store.load_snapshot()
                        if work.work_id != work_id:
                            raise CollaborationError("protocol_error", "Snapshot work_id mismatch")
                        seq = store.get_latest_event_seq()
                    else:
                        store.ensure_dir()
                        work = Work(
                            work_id=work_id,
                            goal=validated_assignment["goal"],
                            artifact_refs=validated_assignment["artifact_refs"],
                            allowed_files=validated_assignment["allowed_files"],
                            allowed_actions=validated_assignment["allowed_actions"],
                            acceptance=validated_assignment["acceptance"],
                            task_ids=validated_assignment["task_ids"],
                            timeout_seconds=validated_assignment["timeout_seconds"],
                            schema_version=SCHEMA_VERSION,
                            revision=1,
                            state="awaiting_approval",
                            baseline=baseline,
                            approval_id=None,
                            stop_requested=False,
                            handoff_requested=False,
                        )

                        seq = store.commit_mutation(
                            work=work,
                            event_type="work_created",
                            payload={"assignment": validated_assignment},
                            request_id=clean_req_id,
                        )

                    # Fail-closed check: snapshot file MUST exist after commit_mutation
                    if not store.snapshot_file.exists():
                        raise CollaborationError(
                            "corruption_detected",
                            f"Snapshot missing after mutation commit for work {work_id}"
                        )

                    # Ensure handoff matches revision 1 / awaiting_approval
                    need_create_handoff = True
                    if store.handoff_file.exists():
                        try:
                            cur_h = store.load_handoff()
                            if ("**Revision:** 1" in cur_h or "Revision: 1" in cur_h) and "awaiting_approval" in cur_h:
                                need_create_handoff = False
                        except Exception:
                            need_create_handoff = True

                    if need_create_handoff:
                        handoff = Handoff(
                            work_id=work_id,
                            revision=1,
                            goal=validated_assignment["goal"],
                            relative_artifacts=validated_assignment["artifact_refs"],
                            completed_tasks=[],
                            remaining_tasks=validated_assignment["task_ids"],
                            safe_check_summaries=[],
                            operation_boundary="awaiting_approval",
                            requires_reconcile=False,
                        )
                        store.save_handoff(handoff)

                    response = {
                        "schema_version": SCHEMA_VERSION,
                        "ok": True,
                        "work_id": work_id,
                        "revision": 1,
                        "state": "awaiting_approval",
                        "execution_known": True,
                        "last_event_seq": seq,
                        "next_action": "approve",
                    }

                    record = {
                        "request_id": clean_req_id,
                        "operation": "create",
                        "payload_digest": payload_digest,
                        "work_id": work_id,
                        "target_revision": 1,
                        "baseline": baseline,
                        "status": "completed",
                        "response": response,
                        "created_at": created_at,
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                    }
                    write_atomic_json(req_file, record)

                    return response

    def approve(
        self,
        work_id: str,
        expected_revision: int,
        request_id: str,
        decision: Dict[str, Any],
        scope_digest: str,
    ) -> Dict[str, Any]:
        clean_work_id = validate_uuid(work_id, "work_id")
        clean_exp_rev = validate_strict_int(expected_revision, "expected_revision", min_val=1)
        clean_req_id = validate_uuid(request_id, "request_id")
        clean_digest = validate_non_empty_str(scope_digest, "scope_digest")

        if not isinstance(decision, dict):
            raise CollaborationError("protocol_error", "Decision must be a dictionary")

        validate_strict_keys(decision, {"source", "approved_text", "approved_actions", "timestamp"}, "Decision")
        for req in ("source", "approved_text", "approved_actions", "timestamp"):
            if req not in decision:
                raise CollaborationError("protocol_error", f"Missing required decision field: '{req}'")

        source = validate_non_empty_str(decision.get("source"), "decision.source")
        if source.lower().strip() not in VALID_HUMAN_SOURCES:
            raise CollaborationError("permission_denied", f"Approval source must be human user, cannot be {source}")

        raw_text = decision.get("approved_text")
        clean_text = validate_non_empty_str(raw_text, "decision.approved_text")
        approved_text = sanitize_safe_output(clean_text)

        raw_ts = decision.get("timestamp")
        clean_ts = validate_non_empty_str(raw_ts, "decision.timestamp")
        try:
            parsed_dt = datetime.fromisoformat(clean_ts)
        except Exception:
            raise CollaborationError("protocol_error", f"Invalid ISO 8601 format for decision.timestamp: '{clean_ts}'")
        if parsed_dt.tzinfo is None or parsed_dt.tzinfo.utcoffset(parsed_dt) is None:
            raise CollaborationError("protocol_error", f"decision.timestamp must be timezone-aware (got naive: '{clean_ts}')")

        raw_approved_actions = decision.get("approved_actions")
        if not isinstance(raw_approved_actions, list) or not raw_approved_actions:
            raise CollaborationError("protocol_error", "approved_actions must be a non-empty list")
        approved_actions = []
        for a in raw_approved_actions:
            clean_a = validate_non_empty_str(a, "approved_actions item")
            if clean_a not in VALID_ACTION_KINDS:
                raise CollaborationError("protocol_error", f"Invalid action kind '{clean_a}' in approved_actions")
            approved_actions.append(clean_a)

        payload = {
            "work_id": clean_work_id,
            "expected_revision": clean_exp_rev,
            "decision": decision,
            "scope_digest": clean_digest,
        }
        payload_digest = self._compute_payload_digest(payload)

        with self._thread_lock:
            with self._get_request_lock(clean_req_id):
                req_file = self._get_request_file(clean_req_id)
                saved_record = self._load_request_record(req_file, clean_req_id)

                if saved_record is not None:
                    if saved_record.get("payload_digest") != payload_digest or saved_record.get("operation") != "approve":
                        raise CollaborationError(
                            "request_conflict",
                            f"Duplicate request_id '{clean_req_id}' with different payload or operation"
                        )
                    if saved_record["status"] == "completed":
                        return saved_record["response"]

                    approval_id = saved_record["approval_id"]
                    target_revision = saved_record["target_revision"]
                    created_at = saved_record.get("created_at") or datetime.now(timezone.utc).isoformat()
                else:
                    store = WorkStore(base_dir=self.base_dir, work_id=clean_work_id, checkout_root=self.project_root)
                    if not store.snapshot_file.exists():
                        raise CollaborationError("protocol_error", f"Work not found: {clean_work_id}")

                    approval_id = str(uuid.uuid4())
                    target_revision = clean_exp_rev + 1
                    created_at = datetime.now(timezone.utc).isoformat()

                store = WorkStore(base_dir=self.base_dir, work_id=clean_work_id, checkout_root=self.project_root)
                with store.lock():
                    if not store.snapshot_file.exists():
                        raise CollaborationError("protocol_error", f"Work not found: {clean_work_id}")

                    work = store.load_snapshot()

                    # Check if this exact approval mutation was already committed in a prior crashed attempt
                    if work.revision == target_revision and work.approval_id == approval_id:
                        # Ensure approval record exists and matches
                        need_save_approval = True
                        if store.approval_file.exists():
                            try:
                                loaded_app = store.load_approval()
                                if loaded_app and loaded_app.approval_id == approval_id:
                                    need_save_approval = False
                            except Exception:
                                need_save_approval = True
                        if need_save_approval:
                            approval = Approval(
                                approval_id=approval_id,
                                work_id=clean_work_id,
                                scope_digest=clean_digest,
                                source=source,
                                approved_text=approved_text,
                                approved_actions=approved_actions,
                                created_at=clean_ts,
                            )
                            store.save_approval(approval)

                        # Ensure handoff matches target_revision and approved_awaiting_start
                        # (MUST NOT leave stale revision 1 / awaiting_approval from create!)
                        need_save_handoff = True
                        if store.handoff_file.exists():
                            try:
                                cur_h = store.load_handoff()
                                if (f"**Revision:** {target_revision}" in cur_h or f"Revision: {target_revision}" in cur_h) and "approved_awaiting_start" in cur_h:
                                    need_save_handoff = False
                            except Exception:
                                need_save_handoff = True
                        if need_save_handoff:
                            handoff = Handoff(
                                work_id=clean_work_id,
                                revision=work.revision,
                                goal=work.goal,
                                relative_artifacts=work.artifact_refs,
                                completed_tasks=[],
                                remaining_tasks=work.task_ids,
                                safe_check_summaries=[],
                                operation_boundary="approved_awaiting_start",
                                requires_reconcile=False,
                            )
                            store.save_handoff(handoff)

                        seq = store.get_latest_event_seq()
                    else:
                        if work.revision != clean_exp_rev:
                            raise CollaborationError(
                                "revision_conflict",
                                f"Work revision mismatch: expected {clean_exp_rev}, but work is at revision {work.revision}"
                            )

                        if work.state != "awaiting_approval":
                            raise CollaborationError(
                                "permission_denied",
                                f"Cannot approve work in state '{work.state}'; expected 'awaiting_approval'"
                            )

                        if work.approval_id:
                            raise CollaborationError(
                                "protocol_error",
                                f"Work {clean_work_id} is already approved with approval_id '{work.approval_id}'"
                            )

                        # Decision actions must match bounds of assignment (no undeclared/excess, no missing)
                        excess_actions = set(approved_actions) - set(work.allowed_actions)
                        if excess_actions:
                            raise CollaborationError(
                                "permission_denied",
                                f"Human decision contains excess actions not declared in assignment: {sorted(list(excess_actions))}"
                            )
                        missing_actions = set(work.allowed_actions) - set(approved_actions)
                        if missing_actions:
                            raise CollaborationError(
                                "permission_denied",
                                f"Human decision does not cover all work allowed actions: missing {sorted(list(missing_actions))}"
                            )

                        # Verify actual scope digest matches provided digest
                        actual_digest = canonical_scope_digest(
                            goal=work.goal,
                            artifact_refs=work.artifact_refs,
                            allowed_files=work.allowed_files,
                            allowed_actions=work.allowed_actions,
                            acceptance=work.acceptance,
                            task_ids=work.task_ids,
                            timeout_seconds=work.timeout_seconds,
                            root_dir=self.project_root,
                        )
                        if actual_digest != clean_digest:
                            raise CollaborationError(
                                "scope_violation",
                                f"Scope digest mismatch: expected {clean_digest}, actual {actual_digest}"
                            )

                        # Persist durable intent BEFORE first mutation if not already written
                        if saved_record is None:
                            intent_record = {
                                "request_id": clean_req_id,
                                "operation": "approve",
                                "payload_digest": payload_digest,
                                "work_id": clean_work_id,
                                "expected_revision": clean_exp_rev,
                                "target_revision": target_revision,
                                "approval_id": approval_id,
                                "status": "in_progress",
                                "created_at": created_at,
                                "updated_at": datetime.now(timezone.utc).isoformat(),
                            }
                            write_atomic_json(req_file, intent_record)

                        approval = Approval(
                            approval_id=approval_id,
                            work_id=clean_work_id,
                            scope_digest=clean_digest,
                            source=source,
                            approved_text=approved_text,
                            approved_actions=approved_actions,
                            created_at=clean_ts,
                        )

                        work.approval_id = approval_id
                        work.revision = target_revision
                        work.updated_at = datetime.now(timezone.utc).isoformat()

                        seq = store.commit_mutation(
                            work=work,
                            event_type="work_approved",
                            payload=approval.to_dict(),
                            request_id=clean_req_id,
                        )
                        store.save_approval(approval)

                        handoff = Handoff(
                            work_id=clean_work_id,
                            revision=work.revision,
                            goal=work.goal,
                            relative_artifacts=work.artifact_refs,
                            completed_tasks=[],
                            remaining_tasks=work.task_ids,
                            safe_check_summaries=[],
                            operation_boundary="approved_awaiting_start",
                            requires_reconcile=False,
                        )
                        store.save_handoff(handoff)

                    response = {
                        "schema_version": SCHEMA_VERSION,
                        "ok": True,
                        "work_id": clean_work_id,
                        "revision": work.revision,
                        "state": "awaiting_approval",
                        "execution_known": True,
                        "last_event_seq": seq,
                        "next_action": "start",
                    }

                    record = {
                        "request_id": clean_req_id,
                        "operation": "approve",
                        "payload_digest": payload_digest,
                        "work_id": clean_work_id,
                        "expected_revision": clean_exp_rev,
                        "target_revision": target_revision,
                        "approval_id": approval_id,
                        "status": "completed",
                        "response": response,
                        "created_at": created_at,
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                    }
                    write_atomic_json(req_file, record)

                    return response

    def start(
        self,
        work_id: str,
        expected_revision: int,
        request_id: str,
        model_override: Optional[str] = None,
        agy_override: Optional[Union[str, Path]] = None,
        python_override: Optional[Union[str, Path]] = None,
        quota_adapter: Optional[Any] = None,
        quota_interval: float = 60.0,
        quota_threshold: float = 0.20,
        cli_args_prefix: Optional[List[str]] = None,
        stream_drain_timeout: float = 3.0,
        detached: bool = True,
    ) -> Dict[str, Any]:
        clean_work_id = validate_uuid(work_id, "work_id")
        clean_exp_rev = validate_strict_int(expected_revision, "expected_revision", min_val=1)
        clean_req_id = validate_uuid(request_id, "request_id")

        payload = {
            "work_id": clean_work_id,
            "expected_revision": clean_exp_rev,
            "model_override": model_override,
        }
        payload_digest = self._compute_payload_digest(payload)

        with self._thread_lock:
            with self._get_request_lock(clean_req_id):
                req_file = self._get_request_file(clean_req_id)
                saved_record = self._load_request_record(req_file, clean_req_id)

                if saved_record is not None:
                    if saved_record.get("payload_digest") != payload_digest or saved_record.get("operation") != "start":
                        raise CollaborationError(
                            "request_conflict",
                            f"Duplicate request_id '{clean_req_id}' with different payload or operation"
                        )
                    if saved_record["status"] == "completed":
                        return saved_record["response"]

                    # If status is in_progress, check if already committed
                    turn_id = saved_record.get("turn_id")
                    worker_token = saved_record.get("worker_token")
                    target_revision = saved_record.get("target_revision", clean_exp_rev + 1)
                    created_at = saved_record.get("created_at") or datetime.now(timezone.utc).isoformat()
                else:
                    turn_id = str(uuid.uuid4())
                    worker_token = uuid.uuid4().hex
                    target_revision = clean_exp_rev + 1
                    created_at = datetime.now(timezone.utc).isoformat()

                store = WorkStore(base_dir=self.base_dir, work_id=clean_work_id, checkout_root=self.project_root)
                with store.lock():
                    if not store.snapshot_file.exists():
                        raise CollaborationError("protocol_error", f"Work not found: {clean_work_id}")
                    work = store.load_snapshot()
                    prior_committed = bool(
                        work.current_turn
                        and work.current_turn.turn_id == turn_id
                        and work.current_turn.worker_token == worker_token
                    )

                # Check if this exact start mutation was already committed in a prior attempt
                if prior_committed:
                    with store.ownership_lock():
                        if not store.reservation_file.exists():
                            raise CollaborationError(
                                "execution_unknown",
                                "Previous start attempt did not establish an active reservation; manual recovery required"
                            )
                        try:
                            with open(store.reservation_file, "r", encoding="utf-8") as rf:
                                res_info = json.load(rf)
                        except Exception:
                            raise CollaborationError("corruption_detected", "Corrupted reservation file")

                        if res_info.get("work_id") != clean_work_id or res_info.get("worker_token") != worker_token:
                            raise CollaborationError(
                                "execution_unknown",
                                "Reservation identity or token does not match request worker_token"
                            )

                        if res_info.get("execution_known") is False:
                            raise CollaborationError(
                                "execution_unknown",
                                "Previous start attempt is in execution_unknown state; manual recovery required"
                            )

                        res_pid = res_info.get("actual_observer_pid") or res_info.get("pid")
                        res_start = res_info.get("actual_observer_start_time") or res_info.get("start_time")
                        res_status = res_info.get("status")

                        if res_status == "launching" or not res_pid:
                            store.mark_execution_unknown(worker_token, "Worker crashed before process registration completed")
                            raise CollaborationError(
                                "execution_unknown",
                                "Worker crashed before process registration completed; manual recovery required"
                            )

                        alive = is_process_alive(res_pid, res_start)
                        if alive is False and work.state == "implementing":
                            store.mark_execution_unknown(worker_token, f"Worker process PID {res_pid} is dead while state is implementing")
                            raise CollaborationError(
                                "execution_unknown",
                                f"Worker process (PID {res_pid}) died unexpectedly during turn; manual recovery required"
                            )

                    seq = store.get_latest_event_seq()
                    response = {
                        "schema_version": SCHEMA_VERSION,
                        "ok": True,
                        "work_id": clean_work_id,
                        "turn_id": turn_id,
                        "revision": target_revision,
                        "state": work.state,
                        "execution_known": True,
                        "last_event_seq": seq,
                        "next_action": "read",
                    }
                    record = {
                        "request_id": clean_req_id,
                        "operation": "start",
                        "payload_digest": payload_digest,
                        "work_id": clean_work_id,
                        "expected_revision": clean_exp_rev,
                        "target_revision": target_revision,
                        "turn_id": turn_id,
                        "worker_token": worker_token,
                        "status": "completed",
                        "response": response,
                        "created_at": created_at,
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                    }
                    write_atomic_json(req_file, record)
                    return response

                with store.lock():
                    work = store.load_snapshot()

                    # 1. Revision guard
                    if work.revision != clean_exp_rev:
                        raise CollaborationError(
                            "revision_conflict",
                            f"Work revision mismatch: expected {clean_exp_rev}, but work is at revision {work.revision}"
                        )

                    # 2. Checkout reservation guard under ownership lock FIRST (fail-closed before permitted work state)
                    with store.ownership_lock():
                        if store.reservation_file.exists():
                            try:
                                with open(store.reservation_file, "r", encoding="utf-8") as rf:
                                    res_info = json.load(rf)
                            except Exception:
                                raise CollaborationError("corruption_detected", "Corrupted reservation file")
                            res_work = res_info.get("work_id")
                            if res_work != clean_work_id:
                                raise CollaborationError(
                                    "checkout_busy",
                                    f"Checkout is already reserved by work {res_work}",
                                    details={"existing_work_id": res_work, "checkout_key": store.checkout_key}
                                )
                            if res_info.get("execution_known") is False:
                                raise CollaborationError(
                                    "checkout_busy",
                                    "Checkout has an unverified execution_unknown reservation; manual recovery required"
                                )
                            res_pid = res_info.get("actual_observer_pid") or res_info.get("pid")
                            res_start = res_info.get("actual_observer_start_time") or res_info.get("start_time")
                            if res_pid and is_process_alive(res_pid, res_start) in (True, "unknown"):
                                raise CollaborationError(
                                    "checkout_busy",
                                    f"Active worker process (PID {res_pid}) already running on checkout"
                                )

                    # 3. Permitted work state and approval guards
                    if work.state != "awaiting_approval":
                        raise CollaborationError(
                            "permission_denied",
                            f"Cannot start work in state '{work.state}'; expected 'awaiting_approval'"
                        )

                    if work.stop_requested:
                        raise CollaborationError(
                            "permission_denied",
                            f"Cannot start work {clean_work_id}: stop has been requested"
                        )

                    if not work.approval_id:
                        raise CollaborationError(
                            "approval_required",
                            f"Work {clean_work_id} is not approved; human approval is required before start"
                        )

                    approval = store.load_approval()
                    if not approval or approval.approval_id != work.approval_id:
                        raise CollaborationError(
                            "approval_required",
                            f"Approval record missing or mismatched for work {clean_work_id}"
                        )

                    # Verify actual scope digest matches approval
                    actual_digest = canonical_scope_digest(
                        goal=work.goal,
                        artifact_refs=work.artifact_refs,
                        allowed_files=work.allowed_files,
                        allowed_actions=work.allowed_actions,
                        acceptance=work.acceptance,
                        task_ids=work.task_ids,
                        timeout_seconds=work.timeout_seconds,
                        root_dir=self.project_root,
                    )
                    if actual_digest != approval.scope_digest:
                        raise CollaborationError(
                            "scope_violation",
                            f"Scope digest mismatch: approval expected {approval.scope_digest}, actual {actual_digest}"
                        )

                    # Re-check baseline integrity
                    if work.baseline:
                        ok_scope, violations = verify_scope_integrity(self.project_root, work.baseline, work.allowed_files)
                        if not ok_scope:
                            raise CollaborationError(
                                "scope_violation",
                                f"Scope integrity violated prior to start: {violations}"
                            )

                    # Parameter validation
                    if not (isinstance(quota_interval, (int, float)) and math.isfinite(quota_interval) and quota_interval > 0):
                        raise CollaborationError("protocol_error", f"quota_interval must be a positive finite number, got {quota_interval}")
                    if not (isinstance(quota_threshold, (int, float)) and math.isfinite(quota_threshold) and 0.0 <= quota_threshold <= 1.0):
                        raise CollaborationError("protocol_error", f"quota_threshold must be a finite number between 0.0 and 1.0, got {quota_threshold}")
                    if not (isinstance(stream_drain_timeout, (int, float)) and math.isfinite(stream_drain_timeout) and stream_drain_timeout > 0):
                        raise CollaborationError("protocol_error", f"stream_drain_timeout must be a positive finite number, got {stream_drain_timeout}")

                    # Pre-flight Fresh Applicable Quota Check (T048 Quota Guard)
                    effective_model = model_override
                    adapter = quota_adapter
                    if adapter is None:
                        discovered_agy = discover_executable("agy", arg_override=agy_override)
                        if discovered_agy:
                            adapter = QuotaAdapter(
                                agy_exe=discovered_agy,
                                threshold=quota_threshold,
                                cli_args_prefix=cli_args_prefix,
                            )

                    if adapter is not None:
                        snapshot = adapter.check_quota(model_override=effective_model)
                        if not effective_model:
                            effective_model = snapshot.model_id
                    else:
                        if not effective_model:
                            effective_model = "gemini-3.8-flash-high"
                        snapshot = QuotaSnapshot(
                            snapshot_id=str(uuid.uuid4()),
                            checked_at=datetime.now(timezone.utc).isoformat(),
                            source="agy_cli",
                            model_id=effective_model,
                            group_name="unknown",
                            availability="unknown",
                            buckets=[],
                            threshold=quota_threshold,
                            uncertainty_reason="Quota check unavailable: native agy executable not found",
                        )

                    store.save_quota_snapshot(snapshot)

                    if snapshot.availability in ("low", "exhausted", "unknown"):
                        # Safe QuotaNotice checkpoint without launching prompt (ZERO model calls)
                        notice = QuotaNotice(
                            notice_id=str(uuid.uuid4()),
                            work_id=clean_work_id,
                            snapshot_id=snapshot.snapshot_id,
                            kind=f"quota_{snapshot.availability}",
                            checkpoint_revision=work.revision,
                            task_ids=work.task_ids,
                            last_confirmed_stage=work.state,
                            operation_boundary=work.operation_boundary or work.state,
                            remaining_fraction=min(
                                (b.get("remaining_fraction") for b in snapshot.buckets if b.get("remaining_fraction") is not None),
                                default=None
                            ),
                            reset_time=next((b.get("reset_time") for b in snapshot.buckets if b.get("reset_time")), None),
                        )
                        store.save_quota_notice(notice)
                        store.append_event(
                            event_type="quota_notice",
                            payload=notice.to_dict(),
                            revision=work.revision,
                        )
                        raise CollaborationError(
                            "provider_error",
                            f"Execution blocked by quota: availability={snapshot.availability} ({snapshot.uncertainty_reason or 'threshold cutoff'})",
                            details=notice.to_dict()
                        )

                    # Persist intent record BEFORE launching process
                    if saved_record is None:
                        intent_record = {
                            "request_id": clean_req_id,
                            "operation": "start",
                            "payload_digest": payload_digest,
                            "work_id": clean_work_id,
                            "expected_revision": clean_exp_rev,
                            "target_revision": target_revision,
                            "turn_id": turn_id,
                            "worker_token": worker_token,
                            "status": "in_progress",
                            "created_at": created_at,
                            "updated_at": datetime.now(timezone.utc).isoformat(),
                        }
                        write_atomic_json(req_file, intent_record)

                    # Save schema file and prompt file
                    schema_path = store.work_dir / "schema.json"
                    save_executor_json_schema(schema_path)

                    prompt_lines = [
                        f"# ASSIGNMENT FOR WORK {clean_work_id} (TURN {turn_id})",
                        f"Goal: {work.goal}",
                        f"Tasks: {', '.join(work.task_ids)}",
                        f"Artifacts: {', '.join(work.artifact_refs)}",
                        f"Allowed Files: {', '.join(work.allowed_files)}",
                        f"Allowed Actions: {', '.join(work.allowed_actions)}",
                        f"Acceptance Criteria:",
                    ]
                    for acc in work.acceptance:
                        prompt_lines.append(f"- {acc}")
                    prompt_lines.append(f"Human Approval: source='{approval.source}', approved_actions={approval.approved_actions}")
                    prompt_lines.append(f"Approval Text: {approval.approved_text}")
                    prompt_lines.append("Output your response as strict JSON structured_output adhering to the schema.")
                    prompt_content = "\n".join(prompt_lines)
                    prompt_file = store.work_dir / f"prompt_{turn_id}.txt"
                    prompt_file.write_text(prompt_content, encoding="utf-8")

                    python_exe = discover_executable("python", arg_override=python_override) or Path(sys.executable)
                    agy_exe = discover_executable("agy", arg_override=agy_override)
                    if not agy_exe:
                        raise CollaborationError("tool_unavailable", "Cannot find native agy executable")

                    start_unix = time.time()
                    deadline_unix = start_unix + work.timeout_seconds

                    # 1. Prepare persistent reservation record BEFORE Popen
                    store.prepare_reservation(worker_token=worker_token, start_time=start_unix)

                    # 2. Record Turn and commit turn_started mutation BEFORE Popen
                    turn = Turn(
                        turn_id=turn_id,
                        work_id=clean_work_id,
                        initial_revision=clean_exp_rev,
                        deadline=deadline_unix,
                        conversation_id=None,
                        worker_token=worker_token,
                        pid=None,
                        start_time=start_unix,
                        outcome=None,
                        terminal_received=False,
                        process_exited=False,
                        pending_tools=[],
                    )

                    work.current_turn = turn
                    work.state = "implementing"
                    work.revision = target_revision
                    work.operation_boundary = "implementing"
                    work.updated_at = datetime.now(timezone.utc).isoformat()

                    seq = store.commit_mutation(
                        work=work,
                        event_type="turn_started",
                        payload={
                            "turn_id": turn_id,
                            "worker_token": worker_token,
                            "initial_revision": clean_exp_rev,
                            "target_revision": target_revision,
                            "deadline": deadline_unix,
                            "start_time": start_unix,
                            "model": effective_model,
                        },
                        request_id=clean_req_id,
                    )

                    handoff = Handoff(
                        work_id=clean_work_id,
                        revision=work.revision,
                        goal=work.goal,
                        relative_artifacts=work.artifact_refs,
                        completed_tasks=[],
                        remaining_tasks=work.task_ids,
                        safe_check_summaries=[],
                        operation_boundary="implementing",
                        requires_reconcile=False,
                    )
                    store.save_handoff(handoff)

                    # Fault injection hook: fault before popen
                    if getattr(self, "_fault_before_popen", False):
                        raise RuntimeError("Fault injected before Popen")

                    if detached:
                        # Spawning detached observer process using stable launcher path
                        source_root = Path(__file__).resolve().parents[3]
                        launcher_script = source_root / "scripts" / "ai" / "collaboration" / "worker_main.py"
                        if not launcher_script.exists():
                            raise CollaborationError("tool_unavailable", f"Cannot find observer launcher script: {launcher_script}")

                        observer_args = [
                            str(python_exe),
                            str(launcher_script),
                            "--work-id", clean_work_id,
                            "--turn-id", turn_id,
                            "--target-revision", str(target_revision),
                            "--worker-token", worker_token,
                            "--base-dir", str(self.base_dir),
                            "--project-root", str(self.project_root),
                            "--agy-exe", str(agy_exe),
                            "--prompt-file", str(prompt_file),
                            "--schema-file", str(schema_path),
                            "--timeout", str(work.timeout_seconds),
                            "--quota-interval", str(quota_interval),
                            "--quota-threshold", str(quota_threshold),
                            "--model", effective_model,
                            "--stream-drain-timeout", str(stream_drain_timeout),
                        ]
                        if cli_args_prefix:
                            observer_args.extend(["--cli-args-prefix", json.dumps(cli_args_prefix)])

                        child_env = os.environ.copy()
                        child_env["PYTHONPATH"] = str(source_root)

                        creationflags = 0
                        if sys.platform == "win32":
                            creationflags = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS

                        try:
                            obs_proc = subprocess.Popen(
                                observer_args,
                                cwd=str(self.project_root),
                                env=child_env,
                                stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL,
                                creationflags=creationflags,
                                close_fds=True if sys.platform != "win32" else False,
                                start_new_session=True if sys.platform != "win32" else False,
                            )
                            obs_pid = obs_proc.pid
                        except Exception as e:
                            store.mark_execution_unknown(worker_token, f"Failed to spawn detached observer process: {e}")
                            raise CollaborationError("process_error", f"Failed to spawn detached observer process: {e}")

                        # Reaper thread: retains process handle and calls wait() upon exit to prevent ResourceWarning
                        def _reap_observer(p):
                            p.wait()
                        threading.Thread(target=_reap_observer, args=(obs_proc,), daemon=True).start()

                        # Fault injection hook: fault after Popen BEFORE launch registration
                        if getattr(self, "_fault_after_popen", False):
                            raise RuntimeError("Fault injected after Popen before launch registration")

                        obs_creation_time = None
                        t_deadline = time.monotonic() + 0.2
                        while time.monotonic() < t_deadline:
                            obs_creation_time = get_process_creation_time(obs_pid)
                            if obs_creation_time is not None:
                                break
                            time.sleep(0.01)
                        if obs_creation_time is None:
                            store.mark_execution_unknown(worker_token, f"Failed to verify creation time for process PID {obs_pid}")
                            raise CollaborationError("process_error", f"Failed to get process creation time for PID {obs_pid}")

                        # Update reservation with launcher process PID and creation time
                        store.update_reservation_process(
                            worker_token=worker_token,
                            pid=obs_pid,
                            start_time=obs_creation_time,
                            launcher_pid=obs_pid,
                            launcher_start_time=obs_creation_time,
                        )

                        # Update turn in snapshot with launcher PID if observer hasn't updated it yet
                        with store.lock():
                            w = store.load_snapshot()
                            if w.current_turn:
                                if w.current_turn.pid is None:
                                    w.current_turn.pid = obs_pid
                                    w.current_turn.start_time = obs_creation_time
                                    store.save_snapshot(w)
                    else:
                        # Fault injection hook: fault after popen BEFORE launch registration
                        if getattr(self, "_fault_after_popen", False):
                            raise RuntimeError("Fault injected after Popen before launch registration")

                        obs_pid = os.getpid()
                        obs_creation_time = get_process_creation_time(obs_pid)
                        if obs_creation_time is None:
                            store.mark_execution_unknown(worker_token, f"Failed to verify creation time for process PID {obs_pid}")
                            raise CollaborationError("process_error", f"Failed to get process creation time for PID {obs_pid}")

                        store.update_reservation_process(
                            worker_token=worker_token,
                            pid=obs_pid,
                            start_time=obs_creation_time,
                            launcher_pid=obs_pid,
                            launcher_start_time=obs_creation_time,
                            actual_observer_pid=obs_pid,
                            actual_observer_start_time=obs_creation_time,
                        )
                        with store.lock():
                            w = store.load_snapshot()
                            if w.current_turn:
                                w.current_turn.pid = obs_pid
                                w.current_turn.start_time = obs_creation_time
                                store.save_snapshot(w)

                    if not detached:
                        from .worker_main import run_observer_turn
                        run_observer_turn(
                            work_id=clean_work_id,
                            turn_id=turn_id,
                            target_revision=target_revision,
                            worker_token=worker_token,
                            base_dir=self.base_dir,
                            project_root=self.project_root,
                            agy_exe=agy_exe,
                            prompt_file=prompt_file,
                            schema_file=schema_path,
                            timeout=work.timeout_seconds,
                            quota_interval=quota_interval,
                            quota_threshold=quota_threshold,
                            model=effective_model,
                            cli_args_prefix=cli_args_prefix,
                            stream_drain_timeout=stream_drain_timeout,
                        )

                    response = {
                        "schema_version": SCHEMA_VERSION,
                        "ok": True,
                        "work_id": clean_work_id,
                        "turn_id": turn_id,
                        "revision": target_revision,
                        "state": "implementing",
                        "execution_known": True,
                        "last_event_seq": seq,
                        "next_action": "read",
                    }

                    record = {
                        "request_id": clean_req_id,
                        "operation": "start",
                        "payload_digest": payload_digest,
                        "work_id": clean_work_id,
                        "expected_revision": clean_exp_rev,
                        "target_revision": target_revision,
                        "turn_id": turn_id,
                        "worker_token": worker_token,
                        "status": "completed",
                        "response": response,
                        "created_at": created_at,
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                    }
                    write_atomic_json(req_file, record)

                    return response
