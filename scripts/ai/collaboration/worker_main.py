"""
Standalone Detached Observer Worker Process (Feature 007, Tasks T010, T015, T049).
Runs as an independent OS process detached from MCP/parent CLI (Win32 Hidden/CREATE_NO_WINDOW).
Maintains canonical checkout reservation for the entire writing turn.
Streams normalized allowlisted events into WorkStore, monitors applicable quota periodically during long turns,
persists verified turn checkpoints, and safely records turn completion without leaking reservation.
"""

import os
import sys
import json
import time
import uuid
import math
import argparse
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Any

if __package__ is None or __package__ == "":
    source_root = Path(__file__).resolve().parents[3]
    if str(source_root) not in sys.path:
        sys.path.insert(0, str(source_root))
    from scripts.ai.collaboration.state import (
        WorkStore,
        Work,
        Turn,
        Handoff,
        QuotaNotice,
        QuotaSnapshot,
        CollaborationError,
        SCHEMA_VERSION,
        validate_uuid,
        validate_non_empty_str,
        validate_strict_int,
        validate_strict_float,
        sanitize_safe_output,
        detect_sensitive_content,
        is_process_alive,
        get_process_creation_time,
        verify_process_lineage,
    )
    from scripts.ai.collaboration.worker import (
        AntigravityTurnWorker,
        TurnOutcome,
    )
    from scripts.ai.collaboration.environment import (
        QuotaAdapter,
    )
else:
    from .state import (
        WorkStore,
        Work,
        Turn,
        Handoff,
        QuotaNotice,
        QuotaSnapshot,
        CollaborationError,
        SCHEMA_VERSION,
        validate_uuid,
        validate_non_empty_str,
        validate_strict_int,
        validate_strict_float,
        sanitize_safe_output,
        detect_sensitive_content,
        is_process_alive,
        get_process_creation_time,
        verify_process_lineage,
    )
    from .worker import (
        AntigravityTurnWorker,
        TurnOutcome,
    )
    from .environment import (
        QuotaAdapter,
    )


def _contains_sensitive_content(val: Any) -> bool:
    if isinstance(val, str):
        return detect_sensitive_content(val)
    if isinstance(val, dict):
        return any(_contains_sensitive_content(k) or _contains_sensitive_content(v) for k, v in val.items())
    if isinstance(val, (list, tuple, set)):
        return any(_contains_sensitive_content(x) for x in val)
    return False


def normalize_stream_event(raw_event: Dict[str, Any], work_id: str, turn_id: str) -> Dict[str, Any]:
    """
    Normalizes stream event against an allowlist of permitted fields (Review Point 5).
    Excludes parameters, tool_info, stdout, stderr, raw output, and free response text.
    Rejects sensitive content or canary secret tokens with sensitive_output_blocked.
    """
    if _contains_sensitive_content(raw_event):
        raise CollaborationError(
            "sensitive_output_blocked",
            "Sensitive pattern detected in stream event payload; event dropped without recording"
        )

    evt_type = raw_event.get("event") or "stream_event"
    normalized: Dict[str, Any] = {
        "event": evt_type,
        "work_id": work_id,
        "turn_id": turn_id,
    }

    if evt_type == "init":
        normalized["conversation_id"] = raw_event.get("conversation_id")
    elif evt_type == "step_update":
        step_data = raw_event.get("step_update", {})
        normalized["step_index"] = step_data.get("step_index")
        normalized["step_type"] = step_data.get("step_type")
        normalized["state"] = step_data.get("state")
        tool_info = step_data.get("tool_info")
        if isinstance(tool_info, dict):
            normalized["tool_name"] = tool_info.get("name")
    elif evt_type == "result":
        res_data = raw_event.get("result", {})
        normalized["status"] = res_data.get("status")

    # Anti-leak invariant: verify normalized payload does not contain canary or secret tokens
    for k, v in normalized.items():
        if isinstance(v, str) and detect_sensitive_content(v):
            raise CollaborationError(
                "sensitive_output_blocked",
                f"Sensitive pattern detected in stream event field '{k}'; event dropped without recording"
            )

    return normalized


def run_observer_turn(
    work_id: str,
    turn_id: str,
    target_revision: int,
    worker_token: str,
    base_dir: Path,
    project_root: Path,
    agy_exe: Path,
    prompt_file: Path,
    schema_file: Path,
    timeout: float,
    quota_interval: float = 60.0,
    quota_threshold: float = 0.20,
    model: str = "gemini-3.8-flash-high",
    conversation_id: Optional[str] = None,
    cli_args_prefix: Optional[List[str]] = None,
    stream_drain_timeout: float = 3.0,
) -> TurnOutcome:
    clean_work_id = validate_uuid(work_id, "work_id")
    clean_turn_id = validate_uuid(turn_id, "turn_id")
    clean_rev = validate_strict_int(target_revision, "target_revision", min_val=1)
    clean_token = validate_non_empty_str(worker_token, "worker_token")
    p_root = Path(project_root).resolve()
    b_dir = Path(base_dir).resolve()
    agy = Path(agy_exe).resolve()
    prompt_p = Path(prompt_file).resolve()
    schema_p = Path(schema_file).resolve()
    timeout_val = validate_strict_float(timeout, "timeout", min_val=0.1, max_val=None)
    q_interval = float(quota_interval)
    q_threshold = float(quota_threshold)
    prefix_args = list(cli_args_prefix or [])

    store = WorkStore(base_dir=b_dir, work_id=clean_work_id, checkout_root=p_root)

    def finalize_early_failure(
        error_code: str,
        error_message: str,
        boundary: str = "error",
        notice: Optional[QuotaNotice] = None,
        is_unknown: bool = False,
    ) -> TurnOutcome:
        try:
            with store.lock():
                work = store.load_snapshot()
                status = "execution_unknown" if is_unknown else "ERROR"
                outcome = TurnOutcome(
                    status=status,
                    error_code=error_code,
                    error_message=error_message,
                )
                if is_unknown:
                    work.operation_boundary = "execution_unknown"
                    requires_reconcile = True
                else:
                    work.state = "stopped" if work.stop_requested else "error"
                    work.operation_boundary = boundary
                    requires_reconcile = False

                work.error = {"code": error_code, "message": error_message}
                turn = work.current_turn or Turn(
                    turn_id=clean_turn_id,
                    work_id=clean_work_id,
                    initial_revision=clean_rev - 1,
                    deadline=time.time() + timeout_val,
                    worker_token=clean_token,
                    pid=os.getpid(),
                    start_time=time.time(),
                )
                turn.outcome = status
                turn.terminal_received = False
                turn.process_exited = False
                turn.pending_tools = []
                work.current_turn = turn
                work.revision += 1
                work.updated_at = datetime.now(timezone.utc).isoformat()

                if notice:
                    store.save_quota_notice(notice)
                    store.append_event("quota_notice", notice.to_dict(), work.revision)

                store.commit_mutation(
                    work=work,
                    event_type="turn_failed",
                    payload=outcome.to_dict(),
                )

                handoff = Handoff(
                    work_id=clean_work_id,
                    revision=work.revision,
                    goal=work.goal,
                    relative_artifacts=work.artifact_refs,
                    completed_tasks=[],
                    remaining_tasks=work.task_ids,
                    safe_check_summaries=[],
                    operation_boundary=work.operation_boundary,
                    requires_reconcile=requires_reconcile,
                )
                store.save_handoff(handoff)

            if is_unknown:
                store.mark_execution_unknown(worker_token=clean_token, reason=error_message)
            else:
                store.record_turn_completion(worker_token=clean_token, state=work.state, turn_id=clean_turn_id)

            return outcome
        except Exception as err:
            return TurnOutcome(
                status="execution_unknown" if is_unknown else "ERROR",
                error_code="checkpoint_error",
                error_message=f"Failed to persist failure state: {type(err).__name__}",
            )

    my_pid = os.getpid()
    my_start_time = get_process_creation_time(my_pid)
    if my_start_time is None:
        return finalize_early_failure(
            error_code="execution_unknown",
            error_message="Observer registration failed: cannot determine process creation time",
            is_unknown=True,
        )

    # 1. Observer Handshake Gate: verify coordinator registered reservation and launcher relationship BEFORE generation
    # Gate runs WITHOUT holding execution_lock and WITHOUT holding locks while sleeping!
    gate_deadline = time.monotonic() + 5.0
    registered = False
    res_data: Optional[Dict[str, Any]] = None
    snap_work: Optional[Work] = None

    while time.monotonic() < gate_deadline:
        if store.reservation_file.exists():
            try:
                with store.ownership_lock():
                    with open(store.reservation_file, "r", encoding="utf-8") as f:
                        res_data = json.load(f)
            except Exception:
                res_data = None
        if store.snapshot_file.exists():
            try:
                with store.lock():
                    snap_work = store.load_snapshot()
            except Exception:
                snap_work = None

        if (
            res_data
            and res_data.get("status") in ("launching", "active")
            and res_data.get("work_id") == clean_work_id
            and res_data.get("worker_token") == clean_token
            and snap_work
            and snap_work.work_id == clean_work_id
            and snap_work.revision == clean_rev
            and snap_work.current_turn
            and snap_work.current_turn.turn_id == clean_turn_id
            and snap_work.current_turn.worker_token == clean_token
        ):
            expected_launcher_pid = res_data.get("launcher_pid") or res_data.get("pid")
            expected_launcher_start = res_data.get("launcher_start_time") or res_data.get("start_time")
            if expected_launcher_pid:
                if verify_process_lineage(my_pid, expected_launcher_pid, expected_launcher_start):
                    registered = True
                    break
        time.sleep(0.05)

    if not registered:
        return finalize_early_failure(
            error_code="execution_unknown",
            error_message="Observer registration gate failed: process not verified in active state or launcher lineage mismatch",
            is_unknown=True,
        )

    # 2. Register actual observer identity in reservation and turn under short locks
    store.register_actual_observer(
        worker_token=clean_token,
        actual_pid=my_pid,
        actual_start_time=my_start_time,
    )

    with store.lock():
        snap_work = store.load_snapshot()
        if snap_work.current_turn:
            snap_work.current_turn.pid = my_pid
            snap_work.current_turn.start_time = my_start_time
            store.save_snapshot(snap_work)

    # 3. Acquire execution lock and hold for the entire writing turn
    with store.execution_lock():
        with store.lock():
            snap_work = store.load_snapshot()

        # Pre-generation checks: stop_requested, handoff_requested, approval
        if snap_work.stop_requested:
            return finalize_early_failure(
                error_code="stopped",
                error_message="Stop requested before model generation started",
                boundary="stopped",
            )
        if snap_work.handoff_requested:
            return finalize_early_failure(
                error_code="handoff_requested",
                error_message="Handoff requested before model generation started",
                boundary="stopped",
            )

        with store.lock():
            approval = store.load_approval()
            if not approval or approval.approval_id != snap_work.approval_id:
                return finalize_early_failure(
                    error_code="approval_required",
                    error_message=f"Valid approval record missing for work {clean_work_id}",
                    boundary="error",
                )

        # 2. Pre-generation fresh quota check in observer (Review Point 2 & 7)
        adapter = QuotaAdapter(
            agy_exe=agy,
            threshold=q_threshold,
            cli_args_prefix=prefix_args,
        )
        try:
            pre_snap = adapter.check_quota(model_override=model)
            store.save_quota_snapshot(pre_snap)
        except Exception as e:
            return finalize_early_failure(
                error_code="checkpoint_error",
                error_message=f"Failed to persist pre-generation quota snapshot: {type(e).__name__}",
                boundary="execution_unknown",
                is_unknown=True,
            )

        if pre_snap.availability in ("low", "exhausted", "unknown"):
            notice = QuotaNotice(
                notice_id=str(uuid.uuid4()),
                work_id=clean_work_id,
                snapshot_id=pre_snap.snapshot_id,
                kind=f"quota_{pre_snap.availability}",
                checkpoint_revision=snap_work.revision,
                task_ids=snap_work.task_ids,
                last_confirmed_stage=snap_work.state,
                operation_boundary="quota_blocked",
                remaining_fraction=min(
                    (b.get("remaining_fraction") for b in pre_snap.buckets if b.get("remaining_fraction") is not None),
                    default=None
                ),
                reset_time=next((b.get("reset_time") for b in pre_snap.buckets if b.get("reset_time")), None),
            )
            return finalize_early_failure(
                error_code="provider_error",
                error_message=f"Pre-generation quota check failed: availability={pre_snap.availability}",
                boundary="quota_blocked",
                notice=notice,
            )

        # 3. Read prompt
        try:
            prompt_content = prompt_p.read_text(encoding="utf-8")
        except Exception as e:
            return finalize_early_failure(
                error_code="process_error",
                error_message=f"Failed to read prompt file: {e}",
                boundary="error",
            )

        # 4. Setup allowlisted event callback (Review Point 5 & 7)
        def on_stream_event(raw_evt: Dict[str, Any]):
            try:
                norm = normalize_stream_event(raw_evt, clean_work_id, clean_turn_id)
                with store.lock():
                    curr = store.load_snapshot()
                    store.append_event(
                        event_type=f"stream_{norm.get('event')}",
                        payload=norm,
                        revision=curr.revision,
                    )
            except CollaborationError:
                raise
            except Exception as e:
                raise CollaborationError("checkpoint_error", f"Event stream checkpoint failed: {e}")

        # 5. Quota monitoring thread during long turn (Review Point 3 & 8)
        stop_quota_event = threading.Event()
        monitor_checkpoint_errors: List[Exception] = []

        def quota_monitor_loop():
            if q_interval <= 0:
                return
            while not stop_quota_event.wait(q_interval):
                if stop_quota_event.is_set():
                    break
                try:
                    snap = adapter.check_quota(model_override=model)
                except Exception as e:
                    snap = QuotaSnapshot(
                        snapshot_id=str(uuid.uuid4()),
                        checked_at=datetime.now(timezone.utc).isoformat(),
                        source="quota_monitor",
                        model_id=model,
                        group_name="default",
                        availability="unknown",
                        buckets=[],
                        threshold=q_threshold,
                        uncertainty_reason="quota_read_error",
                    )

                if stop_quota_event.is_set():
                    break

                try:
                    store.save_quota_snapshot(snap)
                    if snap.availability in ("low", "exhausted", "unknown"):
                        with store.lock():
                            if stop_quota_event.is_set():
                                return
                            curr_w = store.load_snapshot()
                            notice_kind = f"quota_{snap.availability}"
                            reset_t = next((b.get("reset_time") for b in snap.buckets if b.get("reset_time")), None)

                            # Deduplicate existing notice
                            existing = store.load_quota_notice()
                            if existing and existing.kind == notice_kind and existing.reset_time == reset_t:
                                continue

                            # Request stop of next turns, but do not stop active writer prematurely
                            curr_w.stop_requested = True
                            store.save_snapshot(curr_w)

                            notice = QuotaNotice(
                                notice_id=str(uuid.uuid4()),
                                work_id=clean_work_id,
                                snapshot_id=snap.snapshot_id,
                                kind=notice_kind,
                                checkpoint_revision=curr_w.revision,
                                task_ids=curr_w.task_ids,
                                last_confirmed_stage=curr_w.state,
                                operation_boundary=curr_w.operation_boundary or curr_w.state,
                                remaining_fraction=min(
                                    (b.get("remaining_fraction") for b in snap.buckets if b.get("remaining_fraction") is not None),
                                    default=None
                                ),
                                reset_time=reset_t,
                            )
                            store.save_quota_notice(notice)
                            store.append_event(
                                event_type="quota_notice",
                                payload=notice.to_dict(),
                                revision=curr_w.revision,
                            )
                except Exception as e:
                    monitor_checkpoint_errors.append(e)

        # 6. Instantiate and execute worker
        worker = AntigravityTurnWorker(
            agy_exe=agy,
            root_dir=p_root,
            work_id=clean_work_id,
            turn_id=clean_turn_id,
            prompt=prompt_content,
            conversation_id=conversation_id,
            timeout_seconds=timeout_val,
            cli_args_prefix=prefix_args,
            on_event_callback=on_stream_event,
            stream_drain_timeout=stream_drain_timeout,
            model=model,
        )

        q_thread = threading.Thread(target=quota_monitor_loop, daemon=True)
        q_thread.start()

        is_late = False
        outcome = None

        try:
            outcome = worker.execute(schema_p)

            # 7. Late observation on timeout / execution_unknown
            if outcome.status == "execution_unknown":
                with store.lock():
                    work = store.load_snapshot()
                    turn = work.current_turn or Turn(
                        turn_id=clean_turn_id,
                        work_id=clean_work_id,
                        initial_revision=clean_rev - 1,
                        deadline=time.time() + timeout_val,
                        worker_token=clean_token,
                        pid=os.getpid(),
                        start_time=time.time(),
                    )
                    turn.outcome = "execution_unknown"
                    turn.terminal_received = False
                    turn.process_exited = (worker.proc.poll() is not None) if worker.proc else False
                    turn.pending_tools = list(worker.pending_tool_steps.values()) if worker.pending_tool_steps else []
                    work.current_turn = turn
                    work.operation_boundary = "execution_unknown"
                    work.updated_at = datetime.now(timezone.utc).isoformat()
                    store.save_snapshot(work)

                    handoff = Handoff(
                        work_id=clean_work_id,
                        revision=work.revision,
                        goal=work.goal,
                        relative_artifacts=work.artifact_refs,
                        completed_tasks=[],
                        remaining_tasks=work.task_ids,
                        safe_check_summaries=[],
                        operation_boundary="execution_unknown",
                        requires_reconcile=True,
                    )
                    store.save_handoff(handoff)

                store.mark_execution_unknown(
                    worker_token=clean_token,
                    reason=outcome.error_message or "Execution timeout reached; observer beginning late observation"
                )

                # Continue short polls while CLI is alive or reader has not reached EOF, without retry/kill/release
                while worker.proc and (worker.proc.poll() is None or (worker.t_out and worker.t_out.is_alive()) or (worker.t_err and worker.t_err.is_alive())):
                    if worker.proc.poll() is not None and not (worker.t_out and worker.t_out.is_alive()) and not (worker.t_err and worker.t_err.is_alive()):
                        break
                    time.sleep(0.05)

                if worker.proc and worker.proc.poll() is not None:
                    late_outcome = worker.observe_late_exit(timeout_seconds=max(0.5, stream_drain_timeout))
                    # After exit + EOF, if pending_tools remain, leave unknown
                    if late_outcome and late_outcome.status in ("SUCCESS", "ERROR"):
                        if worker.pending_tool_steps:
                            outcome = TurnOutcome(
                                status="execution_unknown",
                                error_code="pending_tools",
                                error_message="Execution exited with unfinalized pending tools",
                                process_identity=worker.get_process_identity(),
                            )
                        else:
                            outcome = late_outcome
                            is_late = True
                            outcome.process_identity["late"] = True
        finally:
            # Stop and fully join quota monitor after actual operations and BEFORE final checkpoint
            stop_quota_event.set()
            q_thread.join()

        # Monitor checkpoint_error takes strict precedence over normal or late SUCCESS
        if monitor_checkpoint_errors:
            outcome = TurnOutcome(
                status="execution_unknown",
                error_code="checkpoint_error",
                error_message=f"Monitor checkpoint failed: {type(monitor_checkpoint_errors[0]).__name__}",
                process_identity=worker.get_process_identity() if worker else {},
            )

        # 8. Finalize state under lock based on outcome (Review Point 4, 5 & 6)
        with store.lock():
            work = store.load_snapshot()
            turn = work.current_turn or Turn(
                turn_id=clean_turn_id,
                work_id=clean_work_id,
                initial_revision=clean_rev - 1,
                deadline=time.time() + timeout_val,
                worker_token=clean_token,
                pid=os.getpid(),
                start_time=time.time(),
            )
            turn.outcome = outcome.status
            turn.conversation_id = outcome.conversation_id or turn.conversation_id
            turn.pid = os.getpid()

            if outcome.status == "SUCCESS":
                if work.stop_requested:
                    work.state = "stopped"
                    work.operation_boundary = "stopped"
                else:
                    work.state = "in_review"
                    work.operation_boundary = "late_success" if is_late else "in_review"

                work.last_result = outcome.result
                turn.terminal_received = True
                turn.process_exited = True
                turn.pending_tools = []
                work.current_turn = turn
                work.revision = work.revision + 1
                work.updated_at = datetime.now(timezone.utc).isoformat()

                store.commit_mutation(
                    work=work,
                    event_type="turn_completed",
                    payload={"late": is_late, **outcome.to_dict()},
                )

                handoff = Handoff(
                    work_id=clean_work_id,
                    revision=work.revision,
                    goal=work.goal,
                    relative_artifacts=work.artifact_refs,
                    completed_tasks=[],
                    remaining_tasks=work.task_ids,
                    safe_check_summaries=[c.command_or_description for c in outcome.result.checks] if outcome.result else [],
                    operation_boundary=work.operation_boundary,
                    requires_reconcile=is_late,
                )
                store.save_handoff(handoff)

            elif outcome.status == "execution_unknown":
                turn.terminal_received = False
                turn.process_exited = (worker.proc.poll() is not None) if worker.proc else False
                turn.pending_tools = list(worker.pending_tool_steps.values()) if worker.pending_tool_steps else []
                work.current_turn = turn
                work.operation_boundary = "execution_unknown"
                work.updated_at = datetime.now(timezone.utc).isoformat()
                store.save_snapshot(work)

                handoff = Handoff(
                    work_id=clean_work_id,
                    revision=work.revision,
                    goal=work.goal,
                    relative_artifacts=work.artifact_refs,
                    completed_tasks=[],
                    remaining_tasks=work.task_ids,
                    safe_check_summaries=[],
                    operation_boundary="execution_unknown",
                    requires_reconcile=True,
                )
                store.save_handoff(handoff)

            else:
                # ERROR outcome
                cli_exited = bool(worker.proc and worker.proc.poll() is not None)
                cli_terminal = bool(worker.seen_result)
                pending_tools = list(worker.pending_tool_steps.values()) if worker.pending_tool_steps else []

                has_unknown_boundary = (
                    bool(pending_tools)
                    or bool(worker.reader_error)
                    or bool(worker.buffer_overflow)
                    or (outcome.error_code == "checkpoint_error")
                )

                if has_unknown_boundary:
                    work.state = "error"
                    work.operation_boundary = "execution_unknown"
                    outcome.status = "execution_unknown"
                    turn.terminal_received = cli_terminal
                    turn.process_exited = cli_exited
                    turn.pending_tools = pending_tools
                    work.current_turn = turn
                    work.revision += 1
                    work.updated_at = datetime.now(timezone.utc).isoformat()
                    store.commit_mutation(
                        work=work,
                        event_type="turn_failed",
                        payload={"late": is_late, **outcome.to_dict()},
                    )
                    handoff = Handoff(
                        work_id=clean_work_id,
                        revision=work.revision,
                        goal=work.goal,
                        relative_artifacts=work.artifact_refs,
                        completed_tasks=[],
                        remaining_tasks=work.task_ids,
                        safe_check_summaries=[],
                        operation_boundary="execution_unknown",
                        requires_reconcile=True,
                    )
                    store.save_handoff(handoff)
                else:
                    if work.stop_requested:
                        work.state = "stopped"
                    else:
                        work.state = "error"
                    work.operation_boundary = "late_error" if is_late else work.state
                    work.error = {
                        "code": outcome.error_code or "process_error",
                        "message": outcome.error_message or "Execution failed"
                    }
                    turn.terminal_received = cli_terminal
                    turn.process_exited = cli_exited
                    turn.pending_tools = []
                    work.current_turn = turn
                    work.revision += 1
                    work.updated_at = datetime.now(timezone.utc).isoformat()

                    store.commit_mutation(
                        work=work,
                        event_type="turn_failed",
                        payload={"late": is_late, **outcome.to_dict()},
                    )

                    handoff = Handoff(
                        work_id=clean_work_id,
                        revision=work.revision,
                        goal=work.goal,
                        relative_artifacts=work.artifact_refs,
                        completed_tasks=[],
                        remaining_tasks=work.task_ids,
                        safe_check_summaries=[],
                        operation_boundary=work.operation_boundary,
                        requires_reconcile=is_late,
                    )
                    store.save_handoff(handoff)

        # Outside store.lock():
        if outcome.status == "SUCCESS":
            store.record_turn_completion(
                worker_token=clean_token,
                state=work.state,
                turn_id=clean_turn_id,
            )
        elif outcome.status == "execution_unknown":
            store.mark_execution_unknown(
                worker_token=clean_token,
                reason=outcome.error_message or "Execution unknown due to timeout or active background operations"
            )
        else:
            if has_unknown_boundary:
                store.mark_execution_unknown(
                    worker_token=clean_token,
                    reason=f"Error exit had unknown boundary: pending_tools={pending_tools}, reader_error={worker.reader_error}"
                )
            else:
                store.record_turn_completion(
                    worker_token=clean_token,
                    state=work.state,
                    turn_id=clean_turn_id,
                )

        return outcome


def main():
    parser = argparse.ArgumentParser(description="Standalone detached observer process for tinker collaboration")
    parser.add_argument("--work-id", required=True, help="Canonical work assignment UUID")
    parser.add_argument("--turn-id", required=True, help="Canonical turn UUID")
    parser.add_argument("--target-revision", type=int, required=True, help="Target revision for mutation")
    parser.add_argument("--worker-token", required=True, help="Worker authorization token")
    parser.add_argument("--base-dir", required=True, help="Base collaboration storage directory")
    parser.add_argument("--project-root", required=True, help="Repository checkout root directory")
    parser.add_argument("--agy-exe", required=True, help="Path to native agy executable")
    parser.add_argument("--prompt-file", required=True, help="Path to prompt text file")
    parser.add_argument("--schema-file", required=True, help="Path to json schema file")
    parser.add_argument("--timeout", type=float, default=600.0, help="Monotonic deadline timeout in seconds")
    parser.add_argument("--quota-interval", type=float, default=60.0, help="Quota polling interval in seconds")
    parser.add_argument("--quota-threshold", type=float, default=0.20, help="Quota low threshold fraction")
    parser.add_argument("--model", default="gemini-3.8-flash-high", help="Pinned model identifier")
    parser.add_argument("--conversation", default=None, help="Existing local conversation UUID")
    parser.add_argument("--cli-args-prefix", default=None, help="JSON-encoded prefix arguments for agy executable")
    parser.add_argument("--stream-drain-timeout", type=float, default=3.0, help="Drain timeout in seconds")

    args = parser.parse_args()

    cli_args_prefix = None
    if args.cli_args_prefix:
        try:
            cli_args_prefix = json.loads(args.cli_args_prefix)
        except Exception as e:
            parser.error(f"Invalid JSON for --cli-args-prefix: {e}")
        if not isinstance(cli_args_prefix, list) or not all(isinstance(x, str) for x in cli_args_prefix):
            parser.error("Field --cli-args-prefix must decode to a JSON list of strings")

    outcome = run_observer_turn(
        work_id=args.work_id,
        turn_id=args.turn_id,
        target_revision=args.target_revision,
        worker_token=args.worker_token,
        base_dir=Path(args.base_dir),
        project_root=Path(args.project_root),
        agy_exe=Path(args.agy_exe),
        prompt_file=Path(args.prompt_file),
        schema_file=Path(args.schema_file),
        timeout=args.timeout,
        quota_interval=args.quota_interval,
        quota_threshold=args.quota_threshold,
        model=args.model,
        conversation_id=args.conversation,
        cli_args_prefix=cli_args_prefix,
        stream_drain_timeout=args.stream_drain_timeout,
    )

    sys.exit(0 if outcome.status == "SUCCESS" else 1)


if __name__ == "__main__":
    main()
