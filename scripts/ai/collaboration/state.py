"""
State management, data models, file locking, Git baseline integrity,
and durable WorkStore for Codex and Antigravity collaboration (Feature 007).
"""

import os
import sys
import json
import time
import math
import uuid
import re
import hashlib
import threading
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Any, Tuple, Union

# Try platform-specific locking primitives
HAS_MSVCRT = False
HAS_FCNTL = False
try:
    import msvcrt
    HAS_MSVCRT = True
except ImportError:
    try:
        import fcntl
        HAS_FCNTL = True
    except ImportError:
        pass


def get_process_creation_time(pid: Optional[int]) -> Optional[float]:
    """
    Returns the process creation time as Unix timestamp (float seconds) using Win32 GetProcessTimes.
    Returns None if process is not found, inaccessible, or on non-Windows without psutil.
    """
    if pid is None or pid <= 0:
        return None
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes
            kernel32 = ctypes.windll.kernel32
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not handle:
                return None
            try:
                creation_time = wintypes.FILETIME()
                exit_time = wintypes.FILETIME()
                kernel_time = wintypes.FILETIME()
                user_time = wintypes.FILETIME()
                kernel32.GetProcessTimes.argtypes = [
                    wintypes.HANDLE,
                    ctypes.POINTER(wintypes.FILETIME),
                    ctypes.POINTER(wintypes.FILETIME),
                    ctypes.POINTER(wintypes.FILETIME),
                    ctypes.POINTER(wintypes.FILETIME),
                ]
                if kernel32.GetProcessTimes(
                    handle,
                    ctypes.byref(creation_time),
                    ctypes.byref(exit_time),
                    ctypes.byref(kernel_time),
                    ctypes.byref(user_time)
                ):
                    ft = (creation_time.dwHighDateTime << 32) | creation_time.dwLowDateTime
                    proc_unix_time = (ft - 116444736000000000) / 10000000.0
                    return proc_unix_time
                return None
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return None
    else:
        try:
            import psutil
            p = psutil.Process(pid)
            return float(p.create_time())
        except Exception:
            return None


def verify_process_lineage(
    child_pid: int,
    expected_launcher_pid: int,
    expected_launcher_start_time: Optional[float] = None,
) -> bool:
    """
    Verifies that child_pid was launched by or directly matches expected_launcher_pid.
    Handles direct match and parent relationship (e.g. Windows Python venv redirector stub).
    Strictly verifies process creation time to fail closed against PID reuse or unavailable identity.
    """
    if not child_pid or child_pid <= 0 or not expected_launcher_pid or expected_launcher_pid <= 0:
        return False

    if expected_launcher_start_time is None:
        return False

    # 1. Direct match (e.g. non-redirector Python, Unix, or same process)
    if child_pid == expected_launcher_pid:
        creation = get_process_creation_time(child_pid)
        if creation is None:
            return False
        if abs(creation - expected_launcher_start_time) > 0.001:
            return False
        return True

    # 2. Check direct parent relationship (e.g. Windows venv redirector stub)
    parent_pid: Optional[int] = None
    if child_pid == os.getpid():
        try:
            parent_pid = os.getppid()
        except Exception:
            parent_pid = None
    else:
        try:
            import psutil
            parent_pid = psutil.Process(child_pid).ppid()
        except Exception:
            parent_pid = None

    if parent_pid is not None and parent_pid == expected_launcher_pid:
        launcher_creation = get_process_creation_time(parent_pid)
        if launcher_creation is None:
            return False
        if abs(launcher_creation - expected_launcher_start_time) > 0.001:
            return False
        return True

    return False



def is_process_alive(pid: Optional[int], expected_start_time: Optional[float] = None) -> Union[bool, str]:
    """
    Checks whether a process with given PID is currently alive on Windows or Unix.
    Returns True if confirmed alive, False if confirmed dead, or 'unknown' if uncertain
    (e.g., AccessDenied, PID reuse mismatch, or OS error).
    """
    if pid is None or pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes
            kernel32 = ctypes.windll.kernel32
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]

            SYNCHRONIZE = 0x00100000
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE, False, pid)
            if not handle:
                err = kernel32.GetLastError()
                ERROR_ACCESS_DENIED = 5
                ERROR_INVALID_PARAMETER = 87
                if err == ERROR_ACCESS_DENIED:
                    return "unknown"
                if err == ERROR_INVALID_PARAMETER:
                    return False
                return "unknown"
            try:
                exit_code = wintypes.DWORD()
                if kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                    STILL_ACTIVE = 259
                    if exit_code.value == STILL_ACTIVE:
                        if expected_start_time is not None:
                            proc_time = get_process_creation_time(pid)
                            if proc_time is not None:
                                if abs(proc_time - expected_start_time) > 10.0:
                                    return "unknown"
                        return True
                    return False
                return "unknown"
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return "unknown"
    else:
        try:
            os.kill(pid, 0)
            return True
        except PermissionError:
            return "unknown"
        except (OSError, ProcessLookupError):
            return False


def find_git_root(
    override: Optional[Any] = None,
    script_path: Optional[Any] = None,
    allow_outside: bool = True
) -> Path:
    """Finds the root of the Git working tree, validating via git rev-parse or .git directory."""
    target = Path(override).resolve() if override else Path.cwd().resolve()
    root = None

    # Check target and its parents for .git
    curr = target if target.is_dir() else target.parent
    for p in [curr] + list(curr.parents):
        if (p / ".git").exists():
            root = p
            break

    # If .git found, verify with git rev-parse if git is available
    if root:
        try:
            r = subprocess.run(
                ["git", "rev-parse", "--show-toplevel"],
                cwd=str(root),
                capture_output=True,
                check=False
            )
            if r.returncode == 0 and r.stdout:
                out_str = r.stdout.decode("utf-8", errors="replace").strip()
                if out_str:
                    candidate = Path(out_str).resolve()
                    if (candidate / ".git").exists():
                        root = candidate
        except Exception:
            pass

    if not root or not (root / ".git").exists():
        raise CollaborationError("tool_unavailable", f"Directory {target} is not inside a valid Git repository")

    if not allow_outside and script_path:
        s_path = Path(script_path).resolve()
        try:
            s_path.relative_to(root)
        except ValueError:
            raise CollaborationError("tool_unavailable", f"Script path is outside repository root")

    return root


SCHEMA_VERSION = 1
DEFAULT_TIMEOUT_SECONDS = 600

SECRET_FILE_PATTERNS = [
    re.compile(r"(^|[/\\])\.env($|\..*)", re.IGNORECASE),
    re.compile(r".*\.pfx$", re.IGNORECASE),
    re.compile(r".*\.p12$", re.IGNORECASE),
    re.compile(r".*\.keystore$", re.IGNORECASE),
    re.compile(r".*\.jks$", re.IGNORECASE),
    re.compile(r".*id_rsa.*", re.IGNORECASE),
    re.compile(r".*id_ed25519.*", re.IGNORECASE),
    re.compile(r".*auth.*\.json$", re.IGNORECASE),
    re.compile(r".*credentials.*\.json$", re.IGNORECASE),
    re.compile(r".*token.*\.json$", re.IGNORECASE),
]

SENSITIVE_CONTENT_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"ghp_[A-Za-z0-9_]{30,}"),
    re.compile(r"gho_[A-Za-z0-9_]{30,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{40,}"),
    re.compile(r"AIza[0-9A-Za-z-_]{35}"),
    re.compile(r"xox[baprs]-[0-9A-Za-z-]{20,}"),
    re.compile(r"(?i)CANARY_SECRET_[A-Za-z0-9_]+"),
    re.compile(r"(?i)CANARY_TOOL_[A-Za-z0-9_]+"),
]

CREDENTIAL_DICT_KEYS = {
    "password", "secret", "token", "api_key", "apikey",
    "private_key", "privatekey", "access_token", "refresh_token",
    "credentials", "credential", "auth_token"
}


def redact_sensitive_strings(text: str) -> str:
    cleaned = text
    cleaned = re.sub(r"ghp_[A-Za-z0-9_]{30,}", "[REDACTED_GH_TOKEN]", cleaned)
    cleaned = re.sub(r"gho_[A-Za-z0-9_]{30,}", "[REDACTED_GH_TOKEN]", cleaned)
    cleaned = re.sub(r"github_pat_[A-Za-z0-9_]{40,}", "[REDACTED_GH_PAT]", cleaned)
    cleaned = re.sub(r"(?i)CANARY_SECRET_[A-Za-z0-9_]+", "[REDACTED_CANARY]", cleaned)
    cleaned = re.sub(r"(?i)CANARY_TOOL_[A-Za-z0-9_]+", "[REDACTED_CANARY]", cleaned)
    return cleaned


class CollaborationError(Exception):
    """Base exception for collaboration bridge with explicit error code and safe details."""
    def __init__(self, code: str, message: str, details: Optional[Dict[str, Any]] = None):
        safe_msg = redact_sensitive_strings(str(message))
        super().__init__(f"[{code}] {safe_msg}")
        self.code = code
        self.message = safe_msg
        self.details = details or {}

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": False,
            "error_code": self.code,
            "message": sanitize_safe_output(self.message),
            "details": sanitize_safe_output(self.details)
        }


def is_valid_uuid(val: Any) -> bool:
    if type(val) is not str:
        return False
    try:
        u = uuid.UUID(val)
        return str(u).lower() == val.lower()
    except (ValueError, AttributeError, TypeError):
        return False


def validate_uuid(val: Any, field_name: str) -> str:
    if not is_valid_uuid(val):
        raise CollaborationError(
            code="protocol_error",
            message=f"Field '{field_name}' must be a valid UUID",
            details={"field": field_name}
        )
    return str(val).lower()


def validate_strict_int(val: Any, field_name: str, min_val: Optional[int] = 1) -> int:
    if type(val) is not int or isinstance(val, bool):
        raise CollaborationError(
            code="protocol_error",
            message=f"Field '{field_name}' must be an integer (not bool)",
            details={"field": field_name}
        )
    if min_val is not None and val < min_val:
        raise CollaborationError(
            code="protocol_error",
            message=f"Field '{field_name}' must be >= {min_val}",
            details={"field": field_name}
        )
    return val


def validate_strict_bool(val: Any, field_name: str) -> bool:
    if type(val) is not bool:
        raise CollaborationError(
            code="protocol_error",
            message=f"Field '{field_name}' must be a boolean",
            details={"field": field_name}
        )
    return val


def validate_strict_float(val: Any, field_name: str, min_val: Optional[float] = 0.0, max_val: Optional[float] = 1.0) -> float:
    if isinstance(val, bool) or type(val) not in (int, float):
        raise CollaborationError(
            code="protocol_error",
            message=f"Field '{field_name}' must be a numeric float",
            details={"field": field_name}
        )
    f_val = float(val)
    if math.isnan(f_val) or math.isinf(f_val):
        raise CollaborationError(
            code="protocol_error",
            message=f"Field '{field_name}' must be a finite number",
            details={"field": field_name}
        )
    if min_val is not None and f_val < min_val:
        raise CollaborationError(
            code="protocol_error",
            message=f"Field '{field_name}' must be >= {min_val}",
            details={"field": field_name}
        )
    if max_val is not None and f_val > max_val:
        raise CollaborationError(
            code="protocol_error",
            message=f"Field '{field_name}' must be <= {max_val}",
            details={"field": field_name}
        )
    return f_val


def validate_non_empty_str(val: Any, field_name: str) -> str:
    if type(val) is not str or not val.strip():
        raise CollaborationError(
            code="protocol_error",
            message=f"Field '{field_name}' must be a non-empty string",
            details={"field": field_name}
        )
    return val.strip()


def validate_strict_keys(data: Dict[str, Any], allowed_keys: set, entity_name: str):
    unknown = set(data.keys()) - allowed_keys
    if unknown:
        # Check if any unknown key contains credentials or sensitive patterns
        for k in unknown:
            if str(k).lower() in CREDENTIAL_DICT_KEYS or detect_sensitive_content(str(k)):
                raise CollaborationError(
                    code="sensitive_output_blocked",
                    message="Sensitive credential dictionary key detected in input"
                )
        raise CollaborationError(
            code="protocol_error",
            message=f"Unknown fields present in {entity_name} (count={len(unknown)})",
            details={"entity": entity_name, "unknown_count": len(unknown)}
        )


def is_secret_path(rel_path: str) -> bool:
    normalized = rel_path.replace("\\", "/")
    for pattern in SECRET_FILE_PATTERNS:
        if pattern.search(normalized):
            return True
    return False


def validate_safe_relative_path(path_str: str, field_name: str) -> str:
    if type(path_str) is not str or not path_str.strip():
        raise CollaborationError(
            code="protocol_error",
            message=f"Field '{field_name}' must be a non-empty path string",
            details={"field": field_name}
        )
    cleaned = path_str.strip().replace("\\", "/")
    if cleaned.startswith("/") or re.match(r"^[a-zA-Z]:", cleaned):
        raise CollaborationError(
            code="scope_violation",
            message=f"Absolute paths are forbidden in '{field_name}': {path_str}"
        )
    parts = cleaned.split("/")
    if ".." in parts or "." in parts:
        raise CollaborationError(
            code="scope_violation",
            message=f"Path traversal ('..' or '.') is forbidden in '{field_name}': {path_str}"
        )
    if is_secret_path(cleaned):
        raise CollaborationError(
            code="scope_violation",
            message=f"Secret file path is forbidden in '{field_name}': {path_str}"
        )
    return cleaned


def detect_sensitive_content(text: str) -> bool:
    for pattern in SENSITIVE_CONTENT_PATTERNS:
        if pattern.search(text):
            return True
    return False


def sanitize_safe_output(content: Any) -> Any:
    """
    Recursively validates content against credential keys and sensitive token patterns.
    Blocks secrets without publishing secret values in error details.
    """
    if isinstance(content, str):
        if detect_sensitive_content(content):
            raise CollaborationError(
                code="sensitive_output_blocked",
                message="Sensitive token, private key, or credential pattern detected in output"
            )
        return content
    elif isinstance(content, dict):
        sanitized = {}
        for k, v in content.items():
            k_str = str(k).lower()
            if k_str in CREDENTIAL_DICT_KEYS:
                raise CollaborationError(
                    code="sensitive_output_blocked",
                    message="Sensitive credential dictionary key detected in output"
                )
            if detect_sensitive_content(str(k)):
                raise CollaborationError(
                    code="sensitive_output_blocked",
                    message="Sensitive pattern detected in output dictionary key"
                )
            sanitized[k] = sanitize_safe_output(v)
        return sanitized
    elif isinstance(content, list):
        return [sanitize_safe_output(item) for item in content]
    return content


def canonical_scope_digest(
    goal: str,
    artifact_refs: List[str],
    allowed_files: List[str],
    allowed_actions: List[str],
    acceptance: List[str],
    task_ids: List[str],
    timeout_seconds: int,
    root_dir: Path,
) -> str:
    """
    Computes scope digest including content fingerprints of existing referenced artifacts.
    root_dir is mandatory and verified. Any modification to the content of spec.md, plan.md,
    tasks.md invalidates the approval digest.
    """
    root = Path(root_dir).resolve()
    if not root.is_dir():
        raise CollaborationError("protocol_error", f"Validated project root directory required for scope digest: {root_dir}")

    # Validate allowed_files paths before computing digest
    clean_allowed_files = []
    for f in allowed_files:
        clean_f = validate_safe_relative_path(f, "allowed_files")
        if is_secret_path(clean_f):
            raise CollaborationError("scope_violation", f"Allowed file cannot be a secret file: {clean_f}")
        abs_f = root / clean_f
        if abs_f.exists():
            try:
                if abs_f.is_dir():
                    raise CollaborationError("scope_violation", f"Allowed file cannot be a directory: {clean_f}")
                resolved_f = abs_f.resolve()
                if not resolved_f.is_relative_to(root):
                    raise CollaborationError("scope_violation", f"Allowed file escapes repository root: {clean_f}")
                if resolved_f.is_dir():
                    raise CollaborationError("scope_violation", f"Allowed file resolves to a directory: {clean_f}")
                rel_f = str(resolved_f.relative_to(root)).replace("\\", "/")
                if is_secret_path(rel_f):
                    raise CollaborationError("scope_violation", f"Allowed file resolves to a secret path")
            except CollaborationError:
                raise
            except Exception:
                raise CollaborationError("scope_violation", f"Allowed file path resolution failed: {clean_f}")
        clean_allowed_files.append(clean_f)

    clean_artifact_refs = []
    artifact_fingerprints = {}
    for ref in sorted(artifact_refs):
        clean_ref = validate_safe_relative_path(ref, "artifact_refs")
        if is_secret_path(clean_ref):
            raise CollaborationError("scope_violation", f"Artifact ref cannot be a secret file: {clean_ref}")
        ref_path = root / clean_ref
        try:
            if not ref_path.exists():
                raise CollaborationError("scope_violation", f"Referenced artifact not found on disk during digest: {clean_ref}")
            if ref_path.is_dir():
                raise CollaborationError("scope_violation", f"Referenced artifact cannot be a directory: {clean_ref}")
            resolved_ref = ref_path.resolve()
        except CollaborationError:
            raise
        except Exception:
            raise CollaborationError("scope_violation", f"Referenced artifact path resolution failed: {clean_ref}")

        try:
            if not resolved_ref.is_relative_to(root):
                raise CollaborationError("scope_violation", f"Referenced artifact escapes repository root: {clean_ref}")
            rel_resolved = str(resolved_ref.relative_to(root)).replace("\\", "/")
        except (ValueError, CollaborationError):
            raise CollaborationError("scope_violation", f"Referenced artifact escapes repository root: {clean_ref}")

        if not resolved_ref.is_file() or resolved_ref.is_dir():
            raise CollaborationError("scope_violation", f"Referenced artifact must resolve to a regular file: {clean_ref}")

        if is_secret_path(rel_resolved):
            raise CollaborationError("scope_violation", f"Referenced artifact resolves to a secret path")

        try:
            with open(ref_path, "rb") as f:
                artifact_fingerprints[clean_ref] = hashlib.sha256(f.read()).hexdigest()
        except Exception:
            raise CollaborationError("scope_violation", f"Referenced artifact unreadable on disk: {clean_ref}")
        clean_artifact_refs.append(clean_ref)

    payload = {
        "goal": goal.strip(),
        "artifact_refs": sorted(clean_artifact_refs),
        "artifact_fingerprints": artifact_fingerprints,
        "allowed_files": sorted(clean_allowed_files),
        "allowed_actions": sorted(allowed_actions),
        "acceptance": sorted(acceptance),
        "task_ids": sorted(task_ids),
        "timeout_seconds": timeout_seconds,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


# -----------------------------------------------------------------------------
# Data Models (T004)
# -----------------------------------------------------------------------------

VALID_WORK_STATES = {
    "awaiting_approval",
    "implementing",
    "needs_answer",
    "in_review",
    "complete",
    "error",
    "stopped",
    "handed_off_to_user",
}

VALID_ACTION_KINDS = {
    "file_edit",
    "file_create",
    "test_run",
    "build_run",
    "command_run",
}

# Documented trusted human/user UI identifiers.
# Note: Declared source enum restricts against arbitrary bot/automation sources
# but does not independently prove cryptographic human identity; API callers
# must ensure the decision originates from an actual human user interaction.
VALID_HUMAN_SOURCES = {
    "human user",
    "human_user",
    "user_ui",
    "web_ui",
    "terminal_user",
    "cli_user",
}

VALID_CHECK_STATUSES = {
    "passed",
    "failed",
    "not_run",
}

VALID_DECISION_KINDS = {
    "technical",
    "scope",
    "permission",
}

VALID_REVIEW_VERDICTS = {
    "accepted",
    "changes_requested",
    "blocked",
}

VALID_QUOTA_AVAILABILITIES = {
    "available",
    "low",
    "exhausted",
    "unknown",
}


class QuotaSnapshot:
    def __init__(
        self,
        snapshot_id: str,
        checked_at: str,
        source: str,
        model_id: str,
        group_name: str,
        availability: str,
        buckets: List[Dict[str, Any]],
        threshold: float,
        uncertainty_reason: Optional[str] = None,
    ):
        self.snapshot_id = validate_uuid(snapshot_id, "snapshot_id")
        self.checked_at = validate_non_empty_str(checked_at, "checked_at")
        self.source = validate_non_empty_str(source, "source")
        self.model_id = validate_non_empty_str(model_id, "model_id")
        self.group_name = validate_non_empty_str(group_name, "group_name")
        if availability not in VALID_QUOTA_AVAILABILITIES:
            raise CollaborationError("protocol_error", f"Invalid quota availability: {availability}")
        self.availability = availability
        self.threshold = validate_strict_float(threshold, "threshold", min_val=0.0, max_val=1.0)
        self.uncertainty_reason = sanitize_safe_output(uncertainty_reason) if uncertainty_reason else None

        normalized_buckets = []
        for b in buckets:
            if not isinstance(b, dict):
                raise CollaborationError("protocol_error", "Quota bucket must be a dictionary")
            validate_strict_keys(b, {"id", "name", "window", "remaining_fraction", "reset_time"}, "QuotaBucket")
            rf = b.get("remaining_fraction")
            if rf is not None:
                rf = validate_strict_float(rf, "remaining_fraction", min_val=0.0, max_val=1.0)
            normalized_buckets.append({
                "id": validate_non_empty_str(b.get("id"), "bucket.id"),
                "name": validate_non_empty_str(b.get("name"), "bucket.name"),
                "window": validate_non_empty_str(b.get("window"), "bucket.window"),
                "remaining_fraction": rf,
                "reset_time": b.get("reset_time")
            })
        self.buckets = normalized_buckets

    def to_dict(self) -> Dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "checked_at": self.checked_at,
            "source": self.source,
            "model_id": self.model_id,
            "group_name": self.group_name,
            "availability": self.availability,
            "buckets": self.buckets,
            "threshold": self.threshold,
            "uncertainty_reason": self.uncertainty_reason,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "QuotaSnapshot":
        if type(data) is not dict:
            raise CollaborationError("protocol_error", "QuotaSnapshot data must be a dictionary")
        validate_strict_keys(data, {
            "snapshot_id", "checked_at", "source", "model_id", "group_name",
            "availability", "buckets", "threshold", "uncertainty_reason"
        }, "QuotaSnapshot")
        for req in ("snapshot_id", "checked_at", "source", "model_id", "group_name", "availability", "buckets", "threshold"):
            if req not in data:
                raise CollaborationError("protocol_error", f"Missing required field in QuotaSnapshot: {req}")
        return cls(
            snapshot_id=data["snapshot_id"],
            checked_at=data["checked_at"],
            source=data["source"],
            model_id=data["model_id"],
            group_name=data["group_name"],
            availability=data["availability"],
            buckets=data["buckets"],
            threshold=data["threshold"],
            uncertainty_reason=data.get("uncertainty_reason"),
        )


VALID_QUOTA_NOTICE_KINDS = {
    "quota_low",
    "quota_exhausted",
    "quota_unknown",
}


class QuotaNotice:
    def __init__(
        self,
        notice_id: str,
        work_id: str,
        snapshot_id: str,
        kind: str,
        checkpoint_revision: int,
        task_ids: List[str],
        last_confirmed_stage: str,
        operation_boundary: str,
        remaining_fraction: Optional[float] = None,
        reset_time: Optional[str] = None,
        delivered_event_seq: Optional[int] = None,
        created_at: Optional[str] = None,
    ):
        self.notice_id = validate_uuid(notice_id, "notice_id")
        self.work_id = validate_uuid(work_id, "work_id")
        self.snapshot_id = validate_uuid(snapshot_id, "snapshot_id")
        if kind not in VALID_QUOTA_NOTICE_KINDS:
            raise CollaborationError("protocol_error", f"Invalid quota notice kind: {kind}")
        self.kind = kind
        self.checkpoint_revision = validate_strict_int(checkpoint_revision, "checkpoint_revision", min_val=1)
        if not isinstance(task_ids, list):
            raise CollaborationError("protocol_error", "task_ids must be a list")
        self.task_ids = [validate_non_empty_str(t, "task_ids item") for t in task_ids]
        self.last_confirmed_stage = validate_non_empty_str(last_confirmed_stage, "last_confirmed_stage")
        self.operation_boundary = sanitize_safe_output(validate_non_empty_str(operation_boundary, "operation_boundary"))
        if remaining_fraction is not None:
            self.remaining_fraction = validate_strict_float(remaining_fraction, "remaining_fraction", min_val=0.0, max_val=1.0)
        else:
            self.remaining_fraction = None
        self.reset_time = sanitize_safe_output(reset_time) if reset_time else None
        if delivered_event_seq is not None:
            self.delivered_event_seq = validate_strict_int(delivered_event_seq, "delivered_event_seq", min_val=1)
        else:
            self.delivered_event_seq = None
        self.created_at = created_at or datetime.now(timezone.utc).isoformat()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "notice_id": self.notice_id,
            "work_id": self.work_id,
            "snapshot_id": self.snapshot_id,
            "kind": self.kind,
            "checkpoint_revision": self.checkpoint_revision,
            "task_ids": self.task_ids,
            "last_confirmed_stage": self.last_confirmed_stage,
            "operation_boundary": self.operation_boundary,
            "remaining_fraction": self.remaining_fraction,
            "reset_time": self.reset_time,
            "delivered_event_seq": self.delivered_event_seq,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "QuotaNotice":
        if type(data) is not dict:
            raise CollaborationError("protocol_error", "QuotaNotice data must be a dictionary")
        validate_strict_keys(data, {
            "notice_id", "work_id", "snapshot_id", "kind", "checkpoint_revision",
            "task_ids", "last_confirmed_stage", "operation_boundary",
            "remaining_fraction", "reset_time", "delivered_event_seq", "created_at"
        }, "QuotaNotice")
        for req in (
            "notice_id", "work_id", "snapshot_id", "kind", "checkpoint_revision",
            "task_ids", "last_confirmed_stage", "operation_boundary"
        ):
            if req not in data:
                raise CollaborationError("protocol_error", f"Missing required field in QuotaNotice: {req}")
        return cls(
            notice_id=data["notice_id"],
            work_id=data["work_id"],
            snapshot_id=data["snapshot_id"],
            kind=data["kind"],
            checkpoint_revision=data["checkpoint_revision"],
            task_ids=data["task_ids"],
            last_confirmed_stage=data["last_confirmed_stage"],
            operation_boundary=data["operation_boundary"],
            remaining_fraction=data.get("remaining_fraction"),
            reset_time=data.get("reset_time"),
            delivered_event_seq=data.get("delivered_event_seq"),
            created_at=data.get("created_at"),
        )


class CheckEvidence:
    def __init__(
        self,
        criterion_id: str,
        command_or_description: str,
        status: str,
        task_id: Optional[str] = None,
        exit_code: Optional[int] = None,
        evidence: Optional[str] = None,
        limitation: Optional[str] = None,
    ):
        self.criterion_id = validate_non_empty_str(criterion_id, "criterion_id")
        self.command_or_description = validate_non_empty_str(command_or_description, "command_or_description")
        if status not in VALID_CHECK_STATUSES:
            raise CollaborationError("protocol_error", f"Invalid check status: {status}")
        self.status = status
        self.task_id = task_id.strip() if (type(task_id) is str and task_id.strip()) else None

        if exit_code is not None:
            self.exit_code = validate_strict_int(exit_code, "exit_code", min_val=None)
        else:
            self.exit_code = None

        if self.status in ("passed", "failed"):
            if not evidence or type(evidence) is not str or not evidence.strip():
                raise CollaborationError(
                    code="protocol_error",
                    message=f"Check '{criterion_id}' status '{self.status}' requires actual evidence string"
                )
            self.evidence = sanitize_safe_output(evidence.strip())
            self.limitation = limitation.strip() if (type(limitation) is str and limitation.strip()) else None
        else:  # not_run
            if not limitation or type(limitation) is not str or not limitation.strip():
                raise CollaborationError(
                    code="protocol_error",
                    message=f"Check '{criterion_id}' status 'not_run' requires limitation reason string"
                )
            self.limitation = sanitize_safe_output(limitation.strip())
            self.evidence = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "criterion_id": self.criterion_id,
            "task_id": self.task_id,
            "status": self.status,
            "command_or_description": self.command_or_description,
            "exit_code": self.exit_code,
            "evidence": self.evidence,
            "limitation": self.limitation,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CheckEvidence":
        if type(data) is not dict:
            raise CollaborationError("protocol_error", "CheckEvidence data must be a dictionary")
        validate_strict_keys(data, {
            "criterion_id", "task_id", "status", "command_or_description",
            "exit_code", "evidence", "limitation"
        }, "CheckEvidence")
        for req in ("criterion_id", "status", "command_or_description"):
            if req not in data:
                raise CollaborationError("protocol_error", f"Missing required field in CheckEvidence: {req}")
        return cls(
            criterion_id=data["criterion_id"],
            command_or_description=data["command_or_description"],
            status=data["status"],
            task_id=data.get("task_id"),
            exit_code=data.get("exit_code"),
            evidence=data.get("evidence"),
            limitation=data.get("limitation"),
        )


class Question:
    def __init__(self, question_id: str, text: str, decision_kind: str):
        self.question_id = validate_non_empty_str(question_id, "question_id")
        self.text = sanitize_safe_output(validate_non_empty_str(text, "text"))
        if decision_kind not in VALID_DECISION_KINDS:
            raise CollaborationError("protocol_error", f"Invalid decision kind: {decision_kind}")
        self.decision_kind = decision_kind

    def to_dict(self) -> Dict[str, Any]:
        return {
            "question_id": self.question_id,
            "text": self.text,
            "decision_kind": self.decision_kind,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Question":
        if type(data) is not dict:
            raise CollaborationError("protocol_error", "Question data must be a dictionary")
        validate_strict_keys(data, {"question_id", "text", "decision_kind"}, "Question")
        for req in ("question_id", "text", "decision_kind"):
            if req not in data:
                raise CollaborationError("protocol_error", f"Missing required field in Question: {req}")
        return cls(
            question_id=data["question_id"],
            text=data["text"],
            decision_kind=data["decision_kind"],
        )


class BlockReason:
    def __init__(self, code: str, needed_action: str):
        self.code = validate_non_empty_str(code, "code")
        self.needed_action = sanitize_safe_output(validate_non_empty_str(needed_action, "needed_action"))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "code": self.code,
            "needed_action": self.needed_action,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "BlockReason":
        if type(data) is not dict:
            raise CollaborationError("protocol_error", "BlockReason data must be a dictionary")
        validate_strict_keys(data, {"code", "needed_action"}, "BlockReason")
        for req in ("code", "needed_action"):
            if req not in data:
                raise CollaborationError("protocol_error", f"Missing required field in BlockReason: {req}")
        return cls(
            code=data["code"],
            needed_action=data["needed_action"],
        )


class Turn:
    def __init__(
        self,
        turn_id: str,
        work_id: str,
        initial_revision: int,
        deadline: float,
        conversation_id: Optional[str] = None,
        worker_token: Optional[str] = None,
        pid: Optional[int] = None,
        start_time: Optional[float] = None,
        outcome: Optional[str] = None,
        terminal_received: bool = False,
        process_exited: bool = False,
        pending_tools: Optional[List[str]] = None,
    ):
        self.turn_id = validate_uuid(turn_id, "turn_id")
        self.work_id = validate_uuid(work_id, "work_id")
        self.initial_revision = validate_strict_int(initial_revision, "initial_revision", min_val=1)
        self.deadline = validate_strict_float(deadline, "deadline", min_val=0.001, max_val=None)
        self.conversation_id = validate_uuid(conversation_id, "conversation_id") if conversation_id else None
        self.worker_token = validate_non_empty_str(worker_token or str(uuid.uuid4()), "worker_token")
        self.pid = validate_strict_int(pid, "pid", min_val=1) if pid is not None else None
        self.start_time = validate_strict_float(start_time, "start_time", min_val=0.0, max_val=None) if start_time is not None else None

        if outcome is not None and outcome not in ("SUCCESS", "ERROR", "execution_unknown"):
            raise CollaborationError("protocol_error", f"Invalid turn outcome: {outcome}")
        self.outcome = outcome
        self.terminal_received = validate_strict_bool(terminal_received, "terminal_received")
        self.process_exited = validate_strict_bool(process_exited, "process_exited")

        raw_tools = pending_tools or []
        if not isinstance(raw_tools, list):
            raise CollaborationError("protocol_error", "pending_tools must be a list")
        self.pending_tools = [validate_non_empty_str(t, "pending_tools") for t in raw_tools]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "turn_id": self.turn_id,
            "work_id": self.work_id,
            "initial_revision": self.initial_revision,
            "deadline": self.deadline,
            "conversation_id": self.conversation_id,
            "worker_token": self.worker_token,
            "pid": self.pid,
            "start_time": self.start_time,
            "outcome": self.outcome,
            "terminal_received": self.terminal_received,
            "process_exited": self.process_exited,
            "pending_tools": self.pending_tools,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Turn":
        if type(data) is not dict:
            raise CollaborationError("protocol_error", "Turn data must be a dictionary")
        validate_strict_keys(data, {
            "turn_id", "work_id", "initial_revision", "deadline",
            "conversation_id", "worker_token", "pid", "start_time",
            "outcome", "terminal_received", "process_exited", "pending_tools"
        }, "Turn")
        for req in ("turn_id", "work_id", "initial_revision", "deadline"):
            if req not in data:
                raise CollaborationError("protocol_error", f"Missing required field in Turn: {req}")
        return cls(
            turn_id=data["turn_id"],
            work_id=data["work_id"],
            initial_revision=data["initial_revision"],
            deadline=data["deadline"],
            conversation_id=data.get("conversation_id"),
            worker_token=data.get("worker_token"),
            pid=data.get("pid"),
            start_time=data.get("start_time"),
            outcome=data.get("outcome"),
            terminal_received=data.get("terminal_received", False),
            process_exited=data.get("process_exited", False),
            pending_tools=data.get("pending_tools", []),
        )


class ExecutorResult:
    def __init__(
        self,
        work_id: str,
        turn_id: str,
        kind: str,
        summary: str,
        claimed_files: List[str],
        checks: List[CheckEvidence],
        remaining_actions: List[str],
        question: Optional[Question] = None,
        block_reason: Optional[BlockReason] = None,
        schema_version: int = SCHEMA_VERSION,
    ):
        self.schema_version = validate_strict_int(schema_version, "schema_version", min_val=1)
        self.work_id = validate_uuid(work_id, "work_id")
        self.turn_id = validate_uuid(turn_id, "turn_id")
        if kind not in ("question", "implementation_result", "blocked"):
            raise CollaborationError("protocol_error", f"Invalid ExecutorResult kind: {kind}")
        self.kind = kind
        self.summary = sanitize_safe_output(validate_non_empty_str(summary, "summary"))
        self.claimed_files = [validate_safe_relative_path(p, "claimed_files") for p in claimed_files]
        self.checks = list(checks)
        self.remaining_actions = [sanitize_safe_output(validate_non_empty_str(a, "remaining_actions")) for a in remaining_actions]

        # Invariant: question and block_reason mutual exclusivity
        if self.kind == "question":
            if question is None:
                raise CollaborationError("protocol_error", "Kind 'question' requires question object")
            if block_reason is not None:
                raise CollaborationError("protocol_error", "Kind 'question' cannot have block_reason")
            self.question = question if isinstance(question, Question) else Question.from_dict(question)
            self.block_reason = None
        elif self.kind == "blocked":
            if block_reason is None:
                raise CollaborationError("protocol_error", "Kind 'blocked' requires block_reason object")
            if question is not None:
                raise CollaborationError("protocol_error", "Kind 'blocked' cannot have question")
            self.block_reason = block_reason if isinstance(block_reason, BlockReason) else BlockReason.from_dict(block_reason)
            self.question = None
        elif self.kind == "implementation_result":
            if question is not None:
                raise CollaborationError("protocol_error", "Kind 'implementation_result' cannot have question")
            if block_reason is not None:
                raise CollaborationError("protocol_error", "Kind 'implementation_result' cannot have block_reason")
            self.question = None
            self.block_reason = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "work_id": self.work_id,
            "turn_id": self.turn_id,
            "kind": self.kind,
            "summary": self.summary,
            "claimed_files": self.claimed_files,
            "checks": [c.to_dict() for c in self.checks],
            "remaining_actions": self.remaining_actions,
            "question": self.question.to_dict() if self.question else None,
            "block_reason": self.block_reason.to_dict() if self.block_reason else None,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ExecutorResult":
        if type(data) is not dict:
            raise CollaborationError("protocol_error", "ExecutorResult data must be a dictionary")
        validate_strict_keys(data, {
            "schema_version", "work_id", "turn_id", "kind", "summary",
            "claimed_files", "checks", "remaining_actions", "question", "block_reason"
        }, "ExecutorResult")
        for req in (
            "schema_version", "work_id", "turn_id", "kind", "summary",
            "claimed_files", "checks", "remaining_actions", "question", "block_reason"
        ):
            if req not in data:
                raise CollaborationError("protocol_error", f"Missing required field in ExecutorResult: {req}")

        raw_checks = data.get("checks", [])
        if type(raw_checks) is not list:
            raise CollaborationError("protocol_error", "checks must be a list")
        checks = [CheckEvidence.from_dict(c) for c in raw_checks]

        return cls(
            work_id=data["work_id"],
            turn_id=data["turn_id"],
            kind=data["kind"],
            summary=data["summary"],
            claimed_files=data["claimed_files"],
            checks=checks,
            remaining_actions=data["remaining_actions"],
            question=data.get("question"),
            block_reason=data.get("block_reason"),
            schema_version=data["schema_version"],
        )


class Review:
    def __init__(
        self,
        review_id: str,
        work_id: str,
        verdict: str,
        checked_fingerprint: str,
        reviewer: str = "Codex",
        remarks: Optional[str] = None,
        scope_violations: Optional[List[str]] = None,
        criteria_results: Optional[Dict[str, str]] = None,
        created_at: Optional[str] = None,
    ):
        self.review_id = validate_uuid(review_id, "review_id")
        self.work_id = validate_uuid(work_id, "work_id")
        if verdict not in VALID_REVIEW_VERDICTS:
            raise CollaborationError("protocol_error", f"Invalid review verdict: {verdict}")
        self.verdict = verdict
        self.checked_fingerprint = validate_non_empty_str(checked_fingerprint, "checked_fingerprint")

        # Strict reviewer constraint
        if reviewer != "Codex":
            raise CollaborationError("protocol_error", f"Reviewer must be strictly 'Codex', got: {reviewer}")
        self.reviewer = "Codex"

        self.remarks = sanitize_safe_output(remarks) if remarks else None
        self.scope_violations = list(scope_violations or [])
        self.criteria_results = dict(criteria_results or {})
        self.created_at = created_at or datetime.now(timezone.utc).isoformat()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "review_id": self.review_id,
            "work_id": self.work_id,
            "verdict": self.verdict,
            "checked_fingerprint": self.checked_fingerprint,
            "reviewer": self.reviewer,
            "remarks": self.remarks,
            "scope_violations": self.scope_violations,
            "criteria_results": self.criteria_results,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Review":
        if type(data) is not dict:
            raise CollaborationError("protocol_error", "Review data must be a dictionary")
        validate_strict_keys(data, {
            "review_id", "work_id", "verdict", "checked_fingerprint",
            "reviewer", "remarks", "scope_violations", "criteria_results", "created_at"
        }, "Review")
        for req in ("review_id", "work_id", "verdict", "checked_fingerprint", "reviewer"):
            if req not in data:
                raise CollaborationError("protocol_error", f"Missing required field in Review: {req}")
        return cls(
            review_id=data["review_id"],
            work_id=data["work_id"],
            verdict=data["verdict"],
            checked_fingerprint=data["checked_fingerprint"],
            reviewer=data["reviewer"],
            remarks=data.get("remarks"),
            scope_violations=data.get("scope_violations"),
            criteria_results=data.get("criteria_results"),
            created_at=data.get("created_at"),
        )


class Approval:
    """
    Represents an explicit human decision to approve a work assignment scope.
    Automated generation or simulation by models/agents is strictly forbidden.
    Note: Declared source enum restricts against arbitrary bot/automation sources
    but does not independently prove cryptographic human identity; API callers
    must ensure the decision originates from an actual human user interaction.
    """
    def __init__(
        self,
        approval_id: str,
        work_id: str,
        scope_digest: str,
        source: str,
        approved_text: str,
        approved_actions: List[str],
        created_at: str,
    ):
        self.approval_id = validate_uuid(approval_id, "approval_id")
        self.work_id = validate_uuid(work_id, "work_id")
        self.scope_digest = validate_non_empty_str(scope_digest, "scope_digest")

        clean_source = validate_non_empty_str(source, "source")
        if clean_source.lower().strip() not in VALID_HUMAN_SOURCES:
            raise CollaborationError("permission_denied", f"Approval source must be human user, cannot be {source}")
        self.source = clean_source

        self.approved_text = sanitize_safe_output(validate_non_empty_str(approved_text, "approved_text"))

        if not isinstance(approved_actions, list) or not approved_actions:
            raise CollaborationError("protocol_error", "approved_actions must be a non-empty list")
        for a in approved_actions:
            if a not in VALID_ACTION_KINDS:
                raise CollaborationError("protocol_error", f"Invalid approved action: {a}")
        self.approved_actions = list(approved_actions)

        clean_ts = validate_non_empty_str(created_at, "created_at") if created_at else None
        if not clean_ts:
            raise CollaborationError("protocol_error", "Approval requires explicit timezone-aware created_at timestamp")
        try:
            parsed_dt = datetime.fromisoformat(clean_ts)
        except Exception:
            raise CollaborationError("protocol_error", f"Invalid ISO 8601 format for Approval.created_at: '{clean_ts}'")
        if parsed_dt.tzinfo is None or parsed_dt.tzinfo.utcoffset(parsed_dt) is None:
            raise CollaborationError("protocol_error", f"Approval.created_at must be timezone-aware (got naive: '{clean_ts}')")
        self.created_at = clean_ts

    def to_dict(self) -> Dict[str, Any]:
        return {
            "approval_id": self.approval_id,
            "work_id": self.work_id,
            "scope_digest": self.scope_digest,
            "source": self.source,
            "approved_text": self.approved_text,
            "approved_actions": self.approved_actions,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Approval":
        if type(data) is not dict:
            raise CollaborationError("protocol_error", "Approval data must be a dictionary")
        validate_strict_keys(data, {
            "approval_id", "work_id", "scope_digest", "source",
            "approved_text", "approved_actions", "created_at"
        }, "Approval")
        for req in ("approval_id", "work_id", "scope_digest", "source", "approved_text", "approved_actions", "created_at"):
            if req not in data:
                raise CollaborationError("protocol_error", f"Missing required field in Approval: {req}")
        return cls(
            approval_id=data["approval_id"],
            work_id=data["work_id"],
            scope_digest=data["scope_digest"],
            source=data["source"],
            approved_text=data["approved_text"],
            approved_actions=data["approved_actions"],
            created_at=data["created_at"],
        )


class Handoff:
    def __init__(
        self,
        work_id: str,
        revision: int,
        goal: str,
        relative_artifacts: List[str],
        completed_tasks: List[str],
        remaining_tasks: List[str],
        safe_check_summaries: List[str],
        operation_boundary: Optional[str] = None,
        stop_release_evidence: Optional[str] = None,
        requires_reconcile: bool = False,
    ):
        self.work_id = validate_uuid(work_id, "work_id")
        self.revision = validate_strict_int(revision, "revision", min_val=1)
        self.goal = sanitize_safe_output(validate_non_empty_str(goal, "goal"))
        self.relative_artifacts = [validate_safe_relative_path(p, "relative_artifacts") for p in relative_artifacts]

        if not isinstance(completed_tasks, list):
            raise CollaborationError("protocol_error", "completed_tasks must be a list")
        self.completed_tasks = [sanitize_safe_output(validate_non_empty_str(t, "completed_tasks")) for t in completed_tasks]

        if not isinstance(remaining_tasks, list):
            raise CollaborationError("protocol_error", "remaining_tasks must be a list")
        self.remaining_tasks = [sanitize_safe_output(validate_non_empty_str(t, "remaining_tasks")) for t in remaining_tasks]

        if not isinstance(safe_check_summaries, list):
            raise CollaborationError("protocol_error", "safe_check_summaries must be a list")
        self.safe_check_summaries = [sanitize_safe_output(validate_non_empty_str(s, "safe_check_summaries")) for s in safe_check_summaries]

        self.operation_boundary = sanitize_safe_output(operation_boundary) if operation_boundary else None
        self.stop_release_evidence = sanitize_safe_output(stop_release_evidence) if stop_release_evidence else None
        self.requires_reconcile = validate_strict_bool(requires_reconcile, "requires_reconcile")

    def to_markdown(self) -> str:
        lines = [
            "# tinker Handoff Context",
            "",
            f"**Revision:** {self.revision}",
            f"**Goal:** {self.goal}",
            f"**Requires Reconcile:** {'Yes' if self.requires_reconcile else 'No'}",
            f"**Current Boundary:** {self.operation_boundary or 'None'}",
            "",
            "## Relative Artifacts",
        ]
        for a in self.relative_artifacts:
            lines.append(f"- [{a}]({a})")
        lines.extend(["", "## Completed Tasks"])
        for t in self.completed_tasks:
            lines.append(f"- [x] {t}")
        lines.extend(["", "## Remaining Tasks"])
        for t in self.remaining_tasks:
            lines.append(f"- [ ] {t}")
        lines.extend(["", "## Safe Verification Summaries"])
        for s in self.safe_check_summaries:
            lines.append(f"- {s}")
        if self.stop_release_evidence:
            lines.extend(["", "## Stop / Release Evidence", self.stop_release_evidence])

        rendered = "\n".join(lines) + "\n"
        # Validate entire rendered document against sensitive content leaks
        return sanitize_safe_output(rendered)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "work_id": self.work_id,
            "revision": self.revision,
            "goal": self.goal,
            "relative_artifacts": self.relative_artifacts,
            "completed_tasks": self.completed_tasks,
            "remaining_tasks": self.remaining_tasks,
            "safe_check_summaries": self.safe_check_summaries,
            "operation_boundary": self.operation_boundary,
            "stop_release_evidence": self.stop_release_evidence,
            "requires_reconcile": self.requires_reconcile,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Handoff":
        if type(data) is not dict:
            raise CollaborationError("protocol_error", "Handoff data must be a dictionary")
        validate_strict_keys(data, {
            "work_id", "revision", "goal", "relative_artifacts",
            "completed_tasks", "remaining_tasks", "safe_check_summaries",
            "operation_boundary", "stop_release_evidence", "requires_reconcile"
        }, "Handoff")
        for req in ("work_id", "revision", "goal", "relative_artifacts", "completed_tasks", "remaining_tasks", "safe_check_summaries"):
            if req not in data:
                raise CollaborationError("protocol_error", f"Missing required field in Handoff: {req}")
        return cls(
            work_id=data["work_id"],
            revision=data["revision"],
            goal=data["goal"],
            relative_artifacts=data["relative_artifacts"],
            completed_tasks=data["completed_tasks"],
            remaining_tasks=data["remaining_tasks"],
            safe_check_summaries=data["safe_check_summaries"],
            operation_boundary=data.get("operation_boundary"),
            stop_release_evidence=data.get("stop_release_evidence"),
            requires_reconcile=data.get("requires_reconcile", False),
        )


class Work:
    def __init__(
        self,
        work_id: str,
        goal: str,
        artifact_refs: List[str],
        allowed_files: List[str],
        allowed_actions: List[str],
        acceptance: List[str],
        task_ids: List[str],
        schema_version: int = SCHEMA_VERSION,
        revision: int = 1,
        state: str = "awaiting_approval",
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        created_at: Optional[str] = None,
        updated_at: Optional[str] = None,
        approval_id: Optional[str] = None,
        baseline: Optional[Dict[str, Any]] = None,
        current_turn: Optional[Turn] = None,
        last_result: Optional[ExecutorResult] = None,
        last_review: Optional[Review] = None,
        stop_requested: bool = False,
        handoff_requested: bool = False,
        operation_boundary: Optional[str] = None,
        error: Optional[Dict[str, Any]] = None,
    ):
        self.schema_version = validate_strict_int(schema_version, "schema_version", min_val=1)
        self.work_id = validate_uuid(work_id, "work_id")
        self.revision = validate_strict_int(revision, "revision", min_val=1)
        self.goal = sanitize_safe_output(validate_non_empty_str(goal, "goal"))
        self.artifact_refs = [validate_safe_relative_path(p, "artifact_refs") for p in artifact_refs]
        self.allowed_files = [validate_safe_relative_path(p, "allowed_files") for p in allowed_files]

        for a in allowed_actions:
            if a not in VALID_ACTION_KINDS:
                raise CollaborationError("protocol_error", f"Invalid action kind: {a}")
        self.allowed_actions = list(allowed_actions)

        self.acceptance = [sanitize_safe_output(validate_non_empty_str(acc, "acceptance")) for acc in acceptance]
        self.task_ids = [validate_non_empty_str(t, "task_ids") for t in task_ids]

        if state not in VALID_WORK_STATES:
            raise CollaborationError("protocol_error", f"Invalid work state: {state}")
        self.state = state

        self.timeout_seconds = validate_strict_int(timeout_seconds, "timeout_seconds", min_val=1)
        self.created_at = created_at or datetime.now(timezone.utc).isoformat()
        self.updated_at = updated_at or datetime.now(timezone.utc).isoformat()
        self.approval_id = validate_uuid(approval_id, "approval_id") if approval_id else None
        self.baseline = baseline
        self.current_turn = current_turn if isinstance(current_turn, Turn) else (Turn.from_dict(current_turn) if current_turn else None)
        self.last_result = last_result if isinstance(last_result, ExecutorResult) else (ExecutorResult.from_dict(last_result) if last_result else None)
        self.last_review = last_review if isinstance(last_review, Review) else (Review.from_dict(last_review) if last_review else None)
        self.stop_requested = validate_strict_bool(stop_requested, "stop_requested")
        self.handoff_requested = validate_strict_bool(handoff_requested, "handoff_requested")
        self.operation_boundary = sanitize_safe_output(operation_boundary) if operation_boundary else None
        self.error = sanitize_safe_output(error) if error else None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "work_id": self.work_id,
            "revision": self.revision,
            "goal": self.goal,
            "artifact_refs": self.artifact_refs,
            "allowed_files": self.allowed_files,
            "allowed_actions": self.allowed_actions,
            "acceptance": self.acceptance,
            "task_ids": self.task_ids,
            "state": self.state,
            "timeout_seconds": self.timeout_seconds,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "approval_id": self.approval_id,
            "baseline": self.baseline,
            "current_turn": self.current_turn.to_dict() if self.current_turn else None,
            "last_result": self.last_result.to_dict() if self.last_result else None,
            "last_review": self.last_review.to_dict() if self.last_review else None,
            "stop_requested": self.stop_requested,
            "handoff_requested": self.handoff_requested,
            "operation_boundary": self.operation_boundary,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Work":
        if type(data) is not dict:
            raise CollaborationError("protocol_error", "Work data must be a dictionary")
        validate_strict_keys(data, {
            "schema_version", "work_id", "revision", "goal", "artifact_refs",
            "allowed_files", "allowed_actions", "acceptance", "task_ids", "state",
            "timeout_seconds", "created_at", "updated_at", "approval_id",
            "baseline", "current_turn", "last_result", "last_review",
            "stop_requested", "handoff_requested", "operation_boundary", "error"
        }, "Work")
        for req in ("schema_version", "work_id", "revision", "goal", "artifact_refs", "allowed_files", "allowed_actions", "acceptance", "task_ids", "state"):
            if req not in data:
                raise CollaborationError("protocol_error", f"Missing required field in Work: {req}")
        return cls(
            work_id=data["work_id"],
            goal=data["goal"],
            artifact_refs=data["artifact_refs"],
            allowed_files=data["allowed_files"],
            allowed_actions=data["allowed_actions"],
            acceptance=data["acceptance"],
            task_ids=data["task_ids"],
            schema_version=data["schema_version"],
            revision=data["revision"],
            state=data["state"],
            timeout_seconds=data.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS),
            created_at=data.get("created_at"),
            updated_at=data.get("updated_at"),
            approval_id=data.get("approval_id"),
            baseline=data.get("baseline"),
            current_turn=data.get("current_turn"),
            last_result=data.get("last_result"),
            last_review=data.get("last_review"),
            stop_requested=data.get("stop_requested", False),
            handoff_requested=data.get("handoff_requested", False),
            operation_boundary=data.get("operation_boundary"),
            error=data.get("error"),
        )


# -----------------------------------------------------------------------------
# File Lock and Durable Storage (T005, T006)
# -----------------------------------------------------------------------------

_thread_file_locks = threading.local()


class FileLock:
    """Advisory file lock using msvcrt (Windows) or fcntl (Unix) with thread reentrancy."""
    def __init__(self, lock_file_path: Path):
        self.lock_file_path = Path(lock_file_path)
        self.file_obj = None

    def acquire(self, timeout: float = 5.0, poll_interval: float = 0.05) -> bool:
        self.lock_file_path.parent.mkdir(parents=True, exist_ok=True)
        can_path = str(self.lock_file_path.resolve())
        if not hasattr(_thread_file_locks, "locks"):
            _thread_file_locks.locks = {}

        if can_path in _thread_file_locks.locks:
            entry = _thread_file_locks.locks[can_path]
            entry["count"] += 1
            self.file_obj = entry["file_obj"]
            return True

        start_time = time.monotonic()
        while True:
            try:
                self.file_obj = open(self.lock_file_path, "a+b")
                if HAS_MSVCRT:
                    self.file_obj.seek(0)
                    msvcrt.locking(self.file_obj.fileno(), msvcrt.LK_NBLCK, 1)
                elif HAS_FCNTL:
                    fcntl.flock(self.file_obj.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                _thread_file_locks.locks[can_path] = {"file_obj": self.file_obj, "count": 1}
                return True
            except (IOError, OSError):
                if self.file_obj:
                    try:
                        self.file_obj.close()
                    except Exception:
                        pass
                    self.file_obj = None
                if time.monotonic() - start_time >= timeout:
                    return False
                time.sleep(poll_interval)

    def release(self):
        can_path = str(self.lock_file_path.resolve())
        if hasattr(_thread_file_locks, "locks") and can_path in _thread_file_locks.locks:
            entry = _thread_file_locks.locks[can_path]
            entry["count"] -= 1
            if entry["count"] > 0:
                self.file_obj = None
                return
            del _thread_file_locks.locks[can_path]

        if self.file_obj:
            try:
                if HAS_MSVCRT:
                    self.file_obj.seek(0)
                    msvcrt.locking(self.file_obj.fileno(), msvcrt.LK_UNLCK, 1)
                elif HAS_FCNTL:
                    fcntl.flock(self.file_obj.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
            finally:
                try:
                    self.file_obj.close()
                except Exception:
                    pass
                self.file_obj = None

    def __enter__(self):
        if not self.acquire():
            raise CollaborationError("lock_timeout", f"Failed to acquire file lock: {self.lock_file_path}")
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.release()


# -----------------------------------------------------------------------------
# Git Baseline and Scope Integrity (T007)
# -----------------------------------------------------------------------------

def compute_file_fingerprint(abs_path: Path, is_secret: bool, repo_root: Optional[Path] = None) -> Dict[str, Any]:
    p = Path(abs_path)

    # 1. Symlink / Junction handling: NEVER open or read target content
    try:
        is_link = os.path.islink(p) or p.is_symlink()
    except FileNotFoundError:
        return {"status": "missing"}
    except (PermissionError, OSError) as e:
        raise CollaborationError(
            code="process_error",
            message=f"Failed to check symlink status during fingerprint computation: {type(e).__name__}"
        )
    except Exception:
        is_link = False

    if is_link:
        try:
            st = os.lstat(p)
        except FileNotFoundError:
            return {"status": "missing"}
        except Exception as e:
            raise CollaborationError(
                code="process_error",
                message=f"Failed to lstat symlink during fingerprint computation: {type(e).__name__}"
            )
        try:
            target = os.readlink(p)
        except FileNotFoundError:
            return {"status": "missing"}
        except (PermissionError, OSError) as e:
            raise CollaborationError(
                code="process_error",
                message=f"Failed to read symlink target during fingerprint computation: {type(e).__name__}"
            )
        except Exception:
            try:
                target = str(p.resolve())
            except FileNotFoundError:
                return {"status": "missing"}
            except Exception as e:
                raise CollaborationError(
                    code="process_error",
                    message=f"Failed to resolve symlink target during fingerprint computation: {type(e).__name__}"
                )

        target_str = str(target)
        link_digest = hashlib.sha256(target_str.encode("utf-8")).hexdigest()
        return {
            "status": "symlink",
            "link_digest": link_digest,
            "mode": st.st_mode,
            "size": st.st_size,
            "mtime": st.st_mtime,
        }

    # 2. Check existence of non-link
    try:
        if not p.exists():
            return {"status": "missing"}
    except FileNotFoundError:
        return {"status": "missing"}
    except Exception as e:
        raise CollaborationError(
            code="process_error",
            message=f"Failed to check existence during fingerprint computation: {type(e).__name__}"
        )

    # 3. Reparse point or external resolved path outside repository root
    if repo_root is not None:
        resolved_root = Path(repo_root).resolve()
        try:
            resolved = p.resolve()
        except FileNotFoundError:
            return {"status": "missing"}
        except Exception as e:
            raise CollaborationError(
                code="process_error",
                message=f"Failed to resolve path during fingerprint computation: {type(e).__name__}"
            )

        try:
            is_outside = not resolved.is_relative_to(resolved_root)
        except (ValueError, Exception):
            is_outside = True

        if is_outside:
            try:
                st = os.lstat(p) if hasattr(os, "lstat") else p.stat()
            except FileNotFoundError:
                return {"status": "missing"}
            except Exception as e:
                raise CollaborationError(
                    code="process_error",
                    message=f"Failed to lstat external path during fingerprint computation: {type(e).__name__}"
                )
            return {
                "status": "external_path",
                "resolved_path_digest": hashlib.sha256(str(resolved).encode("utf-8")).hexdigest(),
                "mode": st.st_mode,
                "size": st.st_size,
                "mtime": st.st_mtime,
            }

        # In-repo resolution: check if resolved target path is secret
        try:
            rel_resolved = str(resolved.relative_to(resolved_root)).replace("\\", "/")
            if is_secret_path(rel_resolved):
                is_secret = True
        except ValueError:
            pass
        except Exception as e:
            raise CollaborationError(
                code="process_error",
                message=f"Failed to check resolved secret path during fingerprint computation: {type(e).__name__}"
            )

    try:
        st = p.stat()
    except FileNotFoundError:
        return {"status": "missing"}
    except Exception as e:
        raise CollaborationError(
            code="process_error",
            message=f"Failed to stat file during fingerprint computation: {type(e).__name__}"
        )

    file_size = st.st_size
    file_mode = st.st_mode

    if is_secret:
        # Strictly metadata only for secret files; never read secret contents
        return {
            "status": "redacted_secret",
            "mtime": st.st_mtime,
            "size": file_size,
            "mode": file_mode,
        }

    try:
        hasher = hashlib.sha256()
        with open(p, "rb") as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                hasher.update(chunk)
        h = hasher.hexdigest()
    except Exception as e:
        raise CollaborationError(
            code="process_error",
            message=f"Failed to read file during fingerprint computation: {type(e).__name__}"
        )

    return {
        "status": "tracked",
        "sha256": h,
        "size": file_size,
        "mode": file_mode,
    }


def canonical_checkout_key(root_path: Path) -> str:
    resolved = Path(root_path).resolve()
    return hashlib.sha256(str(resolved).lower().encode("utf-8")).hexdigest()


def compute_baseline(repo_root: Path, allowed_files: Optional[List[str]] = None) -> Dict[str, Any]:
    """
    Computes baseline snapshot using git status -z and git ls-files -s -z.
    Strictly parses destination and source for renames and records full index entries.
    """
    root = Path(repo_root).resolve()
    if not (root / ".git").exists():
        raise CollaborationError("protocol_error", f"Not a git repository: {root}")

    try:
        # 1. git rev-parse HEAD
        r = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(root),
            capture_output=True,
            check=False
        )
        head_commit = r.stdout.decode("utf-8", errors="replace").strip() if r.returncode == 0 else "UNKNOWN_NO_COMMITS"

        # 2. git status --porcelain=v1 -z --untracked-files=all
        r = subprocess.run(
            ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
            cwd=str(root),
            capture_output=True,
            check=False
        )
        if r.returncode != 0:
            raise CollaborationError("process_error", f"Git status failed: {r.stderr.decode('utf-8', errors='replace').strip()}")

        raw_status = r.stdout
        staged_paths = []
        unstaged_paths = []
        untracked_paths = []
        worktree_fingerprints = {}
        initial_status_map = {}

        parts = raw_status.split(b"\x00")
        i = 0
        while i < len(parts):
            entry = parts[i]
            if not entry:
                i += 1
                continue
            entry_str = entry.decode("utf-8", errors="replace")
            if len(entry_str) >= 3:
                x = entry_str[0]
                y = entry_str[1]
                code = entry_str[:2]
                path = entry_str[3:]

                if x == "R" or y == "R":
                    dest_path = path
                    i += 1
                    src_path = parts[i].decode("utf-8", errors="replace") if i < len(parts) else dest_path
                    staged_paths.append(dest_path)
                    staged_paths.append(src_path)
                    fp_dest = compute_file_fingerprint(root / dest_path, is_secret_path(dest_path), repo_root=root)
                    fp_src = compute_file_fingerprint(root / src_path, is_secret_path(src_path), repo_root=root)
                    worktree_fingerprints[dest_path] = fp_dest
                    worktree_fingerprints[src_path] = fp_src
                    initial_status_map[dest_path] = {"code": code, "extra": src_path, "fingerprint": fp_dest}
                    initial_status_map[src_path] = {"code": code, "extra": dest_path, "fingerprint": fp_src}
                else:
                    if x != " " and x != "?":
                        staged_paths.append(path)
                    if y != " " and y != "?":
                        unstaged_paths.append(path)
                    if x == "?" and y == "?":
                        untracked_paths.append(path)

                    fp = compute_file_fingerprint(root / path, is_secret_path(path), repo_root=root)
                    worktree_fingerprints[path] = fp
                    initial_status_map[path] = {"code": code, "extra": None, "fingerprint": fp}
            i += 1

        # 3. git ls-files -s -z for complete index entries (mode, blob sha, stage)
        r = subprocess.run(
            ["git", "ls-files", "-s", "-z"],
            cwd=str(root),
            capture_output=True,
            check=False
        )
        if r.returncode != 0:
            raise CollaborationError("process_error", f"Git ls-files failed: {r.stderr.decode('utf-8', errors='replace').strip()}")

        index_entries = {}
        index_blobs = {}
        raw_ls = r.stdout.split(b"\x00")
        for item in raw_ls:
            if not item:
                continue
            item_str = item.decode("utf-8", errors="replace")
            if "\t" in item_str:
                meta, file_path = item_str.split("\t", 1)
                meta_parts = meta.split()
                if len(meta_parts) >= 3:
                    mode = meta_parts[0]
                    blob_sha = meta_parts[1]
                    stage = meta_parts[2]
                    entry_dict = {"mode": mode, "sha": blob_sha, "stage": stage}
                    index_entries.setdefault(file_path, []).append(entry_dict)
                    index_blobs[file_path] = blob_sha

        for fpath in index_entries:
            index_entries[fpath].sort(key=lambda s: s["stage"])

        return {
            "head_commit": head_commit,
            "staged_paths": sorted(list(set(staged_paths))),
            "unstaged_paths": sorted(list(set(unstaged_paths))),
            "untracked_paths": sorted(list(set(untracked_paths))),
            "initial_status_map": initial_status_map,
            "index_entries": index_entries,
            "index_blobs": index_blobs,
            "worktree_fingerprints": worktree_fingerprints,
            "checkout_key": canonical_checkout_key(root),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    except CollaborationError:
        raise
    except Exception as e:
        raise CollaborationError("process_error", f"Exception computing git baseline: {e}")


def verify_scope_integrity(
    repo_root: Path,
    baseline: Dict[str, Any],
    allowed_files: List[str]
) -> Tuple[bool, List[str]]:
    """
    Verifies that no files outside allowed_files have been modified, staged, removed, or renamed.
    Preserves initial dirty/staged files from baseline; rejects any new change or stage conflict.
    """
    root = Path(repo_root).resolve()
    allowed_set = {p.replace("\\", "/") for p in allowed_files}
    violations = []

    initial_fingerprints = baseline.get("worktree_fingerprints", {})
    initial_status_map = baseline.get("initial_status_map", {})
    initial_index_entries = baseline.get("index_entries", {})
    if not initial_index_entries and "index_blobs" in baseline:
        initial_index_entries = {k: [{"mode": "100644", "sha": v, "stage": "0"}] for k, v in baseline["index_blobs"].items()}
    elif isinstance(initial_index_entries, dict):
        for k, v in list(initial_index_entries.items()):
            if isinstance(v, dict):
                initial_index_entries[k] = [v]

    # 1. Run git status -z
    r = subprocess.run(
        ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
        cwd=str(root),
        capture_output=True,
        check=False
    )
    if r.returncode != 0:
        raise CollaborationError("process_error", f"Git status failed during scope check: {r.stderr.decode('utf-8', errors='replace').strip()}")

    raw_status = r.stdout
    parts = raw_status.split(b"\x00")
    i = 0
    current_status_paths = set()
    while i < len(parts):
        entry = parts[i]
        if not entry:
            i += 1
            continue
        entry_str = entry.decode("utf-8", errors="replace")
        if len(entry_str) >= 3:
            x = entry_str[0]
            y = entry_str[1]
            code = entry_str[:2]
            path = entry_str[3:]

            if x == "R" or y == "R":
                dest_path = path
                i += 1
                src_path = parts[i].decode("utf-8", errors="replace") if i < len(parts) else dest_path
                current_status_paths.add(dest_path)
                current_status_paths.add(src_path)
                if dest_path not in allowed_set:
                    init_dest = initial_status_map.get(dest_path)
                    if init_dest and ("R" in init_dest.get("code", "") or init_dest.get("code") == code) and init_dest.get("extra") == src_path:
                        cur_fp = compute_file_fingerprint(root / dest_path, is_secret_path(dest_path), repo_root=root)
                        if cur_fp != init_dest.get("fingerprint"):
                            violations.append(f"Pre-existing rename destination modified outside scope: {dest_path}")
                    else:
                        violations.append(f"Rename destination outside scope: {dest_path}")
                if src_path not in allowed_set:
                    init_src = initial_status_map.get(src_path)
                    if init_src and ("R" in init_src.get("code", "") or init_src.get("code") == code):
                        pass
                    else:
                        violations.append(f"Rename source outside scope: {src_path}")
            elif x == "D" or y == "D":
                current_status_paths.add(path)
                if path not in allowed_set:
                    init_del = initial_status_map.get(path)
                    if init_del and "D" in init_del.get("code", ""):
                        pass
                    else:
                        violations.append(f"File deleted outside scope: {path}")
            else:
                current_status_paths.add(path)
                if path not in allowed_set:
                    init_entry = initial_status_map.get(path)
                    cur_fp = compute_file_fingerprint(root / path, is_secret_path(path), repo_root=root)
                    if init_entry and init_entry.get("code") == code:
                        old_fp = init_entry.get("fingerprint") or initial_fingerprints.get(path)
                        if cur_fp != old_fp:
                            violations.append(f"Pre-existing dirty file modified outside scope: {path}")
                    else:
                        violations.append(f"New unexpected change outside scope: {path}")
        i += 1

    # 2. Check all initial worktree fingerprints outside allowed scope
    # Catches initially dirty/untracked files reverted to clean HEAD, deleted, or altered,
    # including files that have completely disappeared from current git status.
    all_initial_paths = set(initial_fingerprints.keys()) | set(initial_status_map.keys())
    for path in sorted(all_initial_paths):
        if path in allowed_set:
            continue

        if any(path in v for v in violations):
            continue

        old_fp = initial_fingerprints.get(path)
        if not old_fp and path in initial_status_map:
            old_fp = initial_status_map[path].get("fingerprint")

        cur_fp = compute_file_fingerprint(root / path, is_secret_path(path), repo_root=root)

        if path not in current_status_paths:
            # File was dirty, untracked, or deleted initially, but is no longer in current git status
            if cur_fp.get("status") == "missing":
                violations.append(f"Pre-existing file removed or disappeared outside scope: {path}")
            else:
                violations.append(f"Pre-existing dirty file reverted to clean HEAD outside scope: {path}")
        elif cur_fp != old_fp:
            if cur_fp.get("status") == "missing":
                violations.append(f"Pre-existing dirty file removed outside scope: {path}")
            else:
                violations.append(f"Pre-existing dirty file modified outside scope: {path}")

    # 3. Check complete staged index entries (blobs, mode, stage, removal)
    r = subprocess.run(
        ["git", "ls-files", "-s", "-z"],
        cwd=str(root),
        capture_output=True,
        check=False
    )
    if r.returncode != 0:
        raise CollaborationError("process_error", f"Git ls-files failed during scope check: {r.stderr.decode('utf-8', errors='replace').strip()}")

    current_index_entries = {}
    raw_ls = r.stdout.split(b"\x00")
    for item in raw_ls:
        if not item:
            continue
        item_str = item.decode("utf-8", errors="replace")
        if "\t" in item_str:
            meta, file_path = item_str.split("\t", 1)
            meta_parts = meta.split()
            if len(meta_parts) >= 3:
                mode = meta_parts[0]
                blob_sha = meta_parts[1]
                stage = meta_parts[2]
                entry_dict = {"mode": mode, "sha": blob_sha, "stage": stage}
                current_index_entries.setdefault(file_path, []).append(entry_dict)

    for fpath in current_index_entries:
        current_index_entries[fpath].sort(key=lambda s: s["stage"])

    all_index_paths = set(initial_index_entries.keys()) | set(current_index_entries.keys())
    for file_path in all_index_paths:
        if file_path not in allowed_set:
            init_stages = initial_index_entries.get(file_path, [])
            curr_stages = current_index_entries.get(file_path, [])
            if init_stages != curr_stages:
                if not curr_stages:
                    violations.append(f"Staged file removal outside scope: {file_path}")
                elif not init_stages:
                    violations.append(f"New staged file outside scope: {file_path}")
                else:
                    violations.append(f"Staged index modified outside scope: {file_path}")

    return len(violations) == 0, violations


# -----------------------------------------------------------------------------
# WorkStore (T005, T006)
# -----------------------------------------------------------------------------

class WorkStore:
    """
    Manages durable storage for a single work item under logs/ai/collaboration/works/<work_id>/,
    and coordinates shared checkout-level reservation and execution locks under
    logs/ai/collaboration/checkouts/<checkout_hash>/.
    Enforces atomic mutation/checkpoint coupling, crash recovery, and CAS ownership.
    """
    def __init__(self, base_dir: Path, work_id: str, checkout_root: Optional[Path] = None):
        self.work_id = validate_uuid(work_id, "work_id")
        self.base_dir = Path(base_dir).resolve()
        self.work_dir = self.base_dir / "works" / self.work_id
        self.snapshot_file = self.work_dir / "snapshot.json"
        self.events_file = self.work_dir / "events.jsonl"
        self.store_lock_file = self.work_dir / "store.lock"
        self.handoff_file = self.work_dir / "handoff.md"
        self.approval_file = self.work_dir / "approval.json"
        self.quota_snapshot_file = self.work_dir / "quota_snapshot.json"
        self.quota_notice_file = self.work_dir / "quota_notice.json"
        self._thread_lock = threading.RLock()

        root = find_git_root(checkout_root or Path.cwd())
        self.checkout_key = canonical_checkout_key(root)
        checkout_hash = hashlib.sha256(self.checkout_key.encode("utf-8")).hexdigest()[:16]
        self.checkout_dir = self.base_dir / "checkouts" / checkout_hash
        self.reservation_file = self.checkout_dir / "reservation.json"
        self.execution_lock_file = self.checkout_dir / "execution.lock"
        self.ownership_lock_file = self.checkout_dir / "ownership.lock"

    def ensure_dir(self):
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.checkout_dir.mkdir(parents=True, exist_ok=True)

    def lock(self) -> FileLock:
        self.ensure_dir()
        return FileLock(self.store_lock_file)

    def execution_lock(self) -> FileLock:
        self.ensure_dir()
        return FileLock(self.execution_lock_file)

    def ownership_lock(self) -> FileLock:
        self.ensure_dir()
        return FileLock(self.ownership_lock_file)

    def write_atomic_json(self, target_path: Path, data: Dict[str, Any]):
        self.ensure_dir()
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

    # -------------------------------------------------------------------------
    # Private Unlocked Helpers (Must be called under self.lock())
    # -------------------------------------------------------------------------

    def _read_events_unlocked(self, after_seq: int = 0) -> List[Dict[str, Any]]:
        if not self.events_file.exists():
            return []
        events = []
        last_seq = 0
        with open(self.events_file, "r", encoding="utf-8") as f:
            for idx, line in enumerate(f, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except Exception:
                    raise CollaborationError(
                        "corruption_detected",
                        f"Corrupted event line {idx} in {self.events_file}"
                    )
                if record.get("work_id") != self.work_id:
                    raise CollaborationError(
                        "corruption_detected",
                        f"Foreign work_id in events log line {idx}"
                    )
                s = record.get("seq", 0)
                if s <= last_seq:
                    raise CollaborationError(
                        "corruption_detected",
                        f"Non-monotonic seq in line {idx}: {s} <= {last_seq}"
                    )
                last_seq = s
                if s > after_seq:
                    events.append(record)
        return events

    def _get_latest_event_info_unlocked(self) -> Tuple[int, int]:
        if not self.events_file.exists():
            return 0, 0
        last_seq = 0
        last_rev = 0
        with open(self.events_file, "r", encoding="utf-8") as f:
            for idx, line in enumerate(f, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except Exception:
                    raise CollaborationError(
                        "corruption_detected",
                        f"Corrupted event line {idx} in {self.events_file}"
                    )
                if item.get("work_id") != self.work_id:
                    raise CollaborationError(
                        "corruption_detected",
                        f"Foreign work_id in events log line {idx}"
                    )
                s = item.get("seq", 0)
                if s <= last_seq:
                    raise CollaborationError(
                        "corruption_detected",
                        f"Non-monotonic sequence number in events log line {idx}"
                    )
                last_seq = s
                last_rev = item.get("revision", 0)
        return last_seq, last_rev

    def _load_snapshot_unlocked(self) -> Work:
        if not self.snapshot_file.exists():
            raise CollaborationError("protocol_error", f"Snapshot not found for work: {self.work_id}")

        try:
            with open(self.snapshot_file, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            raise CollaborationError(
                "corruption_detected",
                f"Corrupted snapshot JSON for work {self.work_id}"
            )

        try:
            loaded = Work.from_dict(data)
        except CollaborationError:
            raise
        except Exception:
            raise CollaborationError(
                "corruption_detected",
                f"Invalid schema or data in snapshot for work {self.work_id}"
            )

        if loaded.work_id != self.work_id:
            raise CollaborationError("protocol_error", "Snapshot work_id mismatch")

        latest_seq, latest_rev = self._get_latest_event_info_unlocked()
        if latest_seq > 0 and latest_rev != loaded.revision:
            raise CollaborationError(
                "corruption_detected",
                f"Inconsistent state: snapshot revision ({loaded.revision}) does not match latest event revision ({latest_rev})"
            )

        return loaded

    def _save_snapshot_unlocked(self, work: Work):
        if work.work_id != self.work_id:
            raise CollaborationError("protocol_error", f"Work ID mismatch in save_snapshot: expected {self.work_id}, got {work.work_id}")
        self.write_atomic_json(self.snapshot_file, work.to_dict())

    def _append_event_unlocked(self, event_type: str, payload: Dict[str, Any], revision: int, request_id: Optional[str] = None) -> int:
        self.ensure_dir()
        latest_seq, latest_rev = self._get_latest_event_info_unlocked()
        next_seq = latest_seq + 1

        safe_payload = sanitize_safe_output(payload)
        payload_digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        event_record = {
            "seq": next_seq,
            "work_id": self.work_id,
            "revision": revision,
            "request_id": request_id,
            "event_type": event_type,
            "payload": safe_payload,
            "payload_digest": payload_digest,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        line = json.dumps(event_record, ensure_ascii=False) + "\n"
        with open(self.events_file, "a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())

        return next_seq

    # -------------------------------------------------------------------------
    # Public Locked Methods
    # -------------------------------------------------------------------------

    def read_events(self, after_seq: int = 0) -> List[Dict[str, Any]]:
        with self._thread_lock:
            with self.lock():
                return self._read_events_unlocked(after_seq)

    def get_latest_event_info(self) -> Tuple[int, int]:
        with self._thread_lock:
            with self.lock():
                return self._get_latest_event_info_unlocked()

    def get_latest_event_seq(self) -> int:
        with self._thread_lock:
            with self.lock():
                return self._get_latest_event_info_unlocked()[0]

    def load_snapshot(self) -> Work:
        with self._thread_lock:
            with self.lock():
                return self._load_snapshot_unlocked()

    def save_snapshot(self, work: Work):
        with self._thread_lock:
            with self.lock():
                self._save_snapshot_unlocked(work)

    def append_event(self, event_type: str, payload: Dict[str, Any], revision: int, request_id: Optional[str] = None) -> int:
        with self._thread_lock:
            with self.lock():
                return self._append_event_unlocked(event_type, payload, revision, request_id)

    def commit_mutation(
        self,
        work: Work,
        event_type: str,
        payload: Dict[str, Any],
        request_id: Optional[str] = None
    ) -> int:
        """
        Unified atomic mutation & checkpoint protocol under a single lock.
        Enforces request idempotency, monotonic sequence, and coupled revisions.
        """
        with self._thread_lock:
            with self.lock():
                self.ensure_dir()

                # 1. Request_id deduplication check (uses _read_events_unlocked - NO nested lock!)
                if request_id:
                    existing_events = self._read_events_unlocked()
                    for ev in existing_events:
                        if ev.get("request_id") == request_id:
                            if ev.get("revision") != work.revision or ev.get("event_type") != event_type:
                                raise CollaborationError(
                                    "request_conflict",
                                    "Duplicate request_id with conflicting revision or event type"
                                )
                            raw_digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
                            if ev.get("payload_digest") is not None:
                                if ev.get("payload_digest") != raw_digest:
                                    raise CollaborationError(
                                        "request_conflict",
                                        "Duplicate request_id with conflicting payload"
                                    )
                            else:
                                ev_payload = ev.get("payload")
                                clean_payload = sanitize_safe_output(payload)
                                if ev_payload is not None and clean_payload != ev_payload:
                                    raise CollaborationError(
                                        "request_conflict",
                                        "Duplicate request_id with conflicting payload"
                                    )

                            # Verify snapshot existence and consistency
                            if not self.snapshot_file.exists():
                                # Recover missing snapshot using the trusted work object from this mutation
                                self._save_snapshot_unlocked(work)
                                if not self.snapshot_file.exists():
                                    raise CollaborationError(
                                        "corruption_detected",
                                        f"Failed to persist snapshot during recovery for work {self.work_id}"
                                    )
                                return ev["seq"]

                            # Snapshot file exists: verify it is not corrupt and matches this revision
                            try:
                                with open(self.snapshot_file, "r", encoding="utf-8") as f:
                                    s_data = json.load(f)
                            except Exception:
                                raise CollaborationError(
                                    "corruption_detected",
                                    f"Corrupted snapshot JSON for work {self.work_id}"
                                )
                            if s_data.get("work_id") != self.work_id:
                                raise CollaborationError(
                                    "corruption_detected",
                                    f"Snapshot work_id mismatch: expected {self.work_id}, got {s_data.get('work_id')}"
                                )
                            if s_data.get("revision") != work.revision:
                                raise CollaborationError(
                                    "corruption_detected",
                                    f"Inconsistent state: snapshot revision ({s_data.get('revision')}) does not match event revision ({work.revision})"
                                )
                            return ev["seq"]

                # 2. Strict revision coupling (uses _get_latest_event_info_unlocked - NO nested lock!)
                latest_seq, latest_rev = self._get_latest_event_info_unlocked()
                expected_rev = 1 if latest_seq == 0 else latest_rev + 1

                if work.revision != expected_rev:
                    raise CollaborationError(
                        "revision_conflict",
                        f"Mutation revision mismatch: work has revision {work.revision}, expected {expected_rev}"
                    )

                next_seq = self._append_event_unlocked(event_type, payload, work.revision, request_id)
                self._save_snapshot_unlocked(work)
                return next_seq

    def recover_interrupted_state(self) -> bool:
        """Detects and safely recovers from interrupted writes at tail of events log."""
        with self._thread_lock:
            with self.lock():
                if not self.events_file.exists():
                    return False
                valid_lines = []
                has_corrupted_tail = False
                with open(self.events_file, "rb") as f:
                    content = f.read()

                raw_lines = content.split(b"\n")
                for i, r_line in enumerate(raw_lines):
                    if not r_line.strip():
                        continue
                    try:
                        json.loads(r_line.decode("utf-8"))
                        valid_lines.append(r_line)
                    except Exception:
                        if i == len(raw_lines) - 1 or (i == len(raw_lines) - 2 and not raw_lines[-1].strip()):
                            has_corrupted_tail = True
                            break
                        else:
                            raise CollaborationError("corruption_detected", "Corrupted event in middle of log")

                if has_corrupted_tail:
                    with open(self.events_file, "wb") as f:
                        for vl in valid_lines:
                            f.write(vl + b"\n")
                        f.flush()
                        os.fsync(f.fileno())
                    return True
                return False

    def save_handoff(self, handoff: Handoff):
        with self._thread_lock:
            with self.lock():
                self.ensure_dir()
                md_content = handoff.to_markdown()
                temp_path = self.handoff_file.parent / f".tmp_{uuid.uuid4().hex}_handoff.md"
                with open(temp_path, "w", encoding="utf-8") as f:
                    f.write(md_content)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(temp_path, self.handoff_file)

    def load_handoff(self) -> str:
        with self._thread_lock:
            with self.lock():
                if not self.handoff_file.exists():
                    raise CollaborationError("protocol_error", f"Handoff markdown not found for work: {self.work_id}")
                with open(self.handoff_file, "r", encoding="utf-8") as f:
                    return f.read()

    def save_approval(self, approval: Approval):
        with self._thread_lock:
            with self.lock():
                self.ensure_dir()
                self.write_atomic_json(self.approval_file, approval.to_dict())

    def load_approval(self) -> Optional[Approval]:
        with self._thread_lock:
            with self.lock():
                if not self.approval_file.exists():
                    return None
                try:
                    with open(self.approval_file, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    return Approval.from_dict(data)
                except Exception:
                    raise CollaborationError("corruption_detected", f"Corrupted approval record for work {self.work_id}")

    def save_quota_snapshot(self, snapshot: QuotaSnapshot):
        with self._thread_lock:
            with self.lock():
                self.ensure_dir()
                self.write_atomic_json(self.quota_snapshot_file, snapshot.to_dict())

    def load_quota_snapshot(self) -> Optional[QuotaSnapshot]:
        with self._thread_lock:
            with self.lock():
                if not self.quota_snapshot_file.exists():
                    return None
                try:
                    with open(self.quota_snapshot_file, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    return QuotaSnapshot.from_dict(data)
                except Exception:
                    raise CollaborationError("corruption_detected", f"Corrupted quota snapshot for work {self.work_id}")

    def save_quota_notice(self, notice: QuotaNotice):
        with self._thread_lock:
            with self.lock():
                self.ensure_dir()
                self.write_atomic_json(self.quota_notice_file, notice.to_dict())

    def load_quota_notice(self) -> Optional[QuotaNotice]:
        with self._thread_lock:
            with self.lock():
                if not self.quota_notice_file.exists():
                    return None
                try:
                    with open(self.quota_notice_file, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    return QuotaNotice.from_dict(data)
                except Exception:
                    raise CollaborationError("corruption_detected", f"Corrupted quota notice for work {self.work_id}")

    def prepare_reservation(self, worker_token: str, start_time: float):
        """
        Prepares persistent reservation record before Popen, establishing CAS ownership.
        """
        with self._thread_lock:
            with self.ownership_lock():
                self.ensure_dir()
                if self.reservation_file.exists():
                    try:
                        with open(self.reservation_file, "r", encoding="utf-8") as f:
                            data = json.load(f)
                    except Exception:
                        raise CollaborationError(
                            "corruption_detected",
                            "Corrupted reservation file in checkout"
                        )

                    existing_work_id = data.get("work_id")
                    if existing_work_id != self.work_id:
                        raise CollaborationError(
                            "checkout_busy",
                            f"Checkout is already reserved by work {existing_work_id}",
                            details={"existing_work_id": existing_work_id, "checkout_key": self.checkout_key}
                        )

                    if data.get("execution_known") is False:
                        raise CollaborationError(
                            "checkout_busy",
                            "Checkout has an unverified execution_unknown reservation; manual recovery required"
                        )

                    existing_pid = data.get("actual_observer_pid") or data.get("pid")
                    existing_start = data.get("actual_observer_start_time") or data.get("start_time")
                    if existing_pid and is_process_alive(existing_pid, existing_start) in (True, "unknown"):
                        raise CollaborationError(
                            "checkout_busy",
                            f"Previous worker process (PID {existing_pid}) is still alive/active on this checkout"
                        )

                record = {
                    "checkout_key": self.checkout_key,
                    "work_id": self.work_id,
                    "pid": None,
                    "start_time": validate_strict_float(start_time, "start_time", min_val=0.0, max_val=None),
                    "launcher_pid": None,
                    "launcher_start_time": None,
                    "actual_observer_pid": None,
                    "actual_observer_start_time": None,
                    "worker_token": validate_non_empty_str(worker_token, "worker_token"),
                    "status": "launching",
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "execution_known": True,
                }
                self.write_atomic_json(self.reservation_file, record)

    def update_reservation_process(
        self,
        worker_token: str,
        pid: int,
        start_time: float,
        launcher_pid: Optional[int] = None,
        launcher_start_time: Optional[float] = None,
        actual_observer_pid: Optional[int] = None,
        actual_observer_start_time: Optional[float] = None,
    ):
        """
        Updates reservation with launcher process PID and creation time immediately after Popen.
        Does not overwrite actual observer identity if the child observer process has already registered.
        """
        with self._thread_lock:
            with self.ownership_lock():
                if not self.reservation_file.exists():
                    raise CollaborationError("protocol_error", "Reservation record does not exist during update")
                try:
                    with open(self.reservation_file, "r", encoding="utf-8") as f:
                        data = json.load(f)
                except Exception:
                    raise CollaborationError("corruption_detected", "Corrupted reservation file")

                if data.get("work_id") != self.work_id or data.get("worker_token") != worker_token:
                    raise CollaborationError("protocol_error", "Reservation identity mismatch during process update")

                eff_l_pid = validate_strict_int(launcher_pid or pid, "launcher_pid", min_val=1)
                eff_l_time = validate_strict_float(launcher_start_time or start_time, "launcher_start_time", min_val=0.0, max_val=None)

                data["launcher_pid"] = eff_l_pid
                data["launcher_start_time"] = eff_l_time

                if actual_observer_pid is not None:
                    act_pid = validate_strict_int(actual_observer_pid, "actual_observer_pid", min_val=1)
                    act_time = validate_strict_float(actual_observer_start_time or eff_l_time, "actual_observer_start_time", min_val=0.0, max_val=None)
                    data["actual_observer_pid"] = act_pid
                    data["actual_observer_start_time"] = act_time
                    data["pid"] = act_pid
                    data["start_time"] = act_time
                elif data.get("actual_observer_pid") is not None:
                    # Child already registered its actual PID; keep data['pid'] and data['start_time']
                    pass
                else:
                    data["pid"] = eff_l_pid
                    data["start_time"] = eff_l_time

                data["status"] = "active"
                data["updated_at"] = datetime.now(timezone.utc).isoformat()
                self.write_atomic_json(self.reservation_file, data)

    def register_actual_observer(self, worker_token: str, actual_pid: int, actual_start_time: float):
        """
        Called by child observer process itself under ownership lock after verifying launcher lineage.
        Records actual observer PID and creation time.
        """
        clean_token = validate_non_empty_str(worker_token, "worker_token")
        act_pid = validate_strict_int(actual_pid, "actual_pid", min_val=1)
        act_time = validate_strict_float(actual_start_time, "actual_start_time", min_val=0.0, max_val=None)

        with self._thread_lock:
            with self.ownership_lock():
                if not self.reservation_file.exists():
                    raise CollaborationError("protocol_error", "Reservation record does not exist during observer registration")
                try:
                    with open(self.reservation_file, "r", encoding="utf-8") as f:
                        data = json.load(f)
                except Exception:
                    raise CollaborationError("corruption_detected", "Corrupted reservation file")

                if data.get("work_id") != self.work_id or data.get("worker_token") != clean_token:
                    raise CollaborationError("protocol_error", "Reservation identity mismatch during actual observer registration")

                data["actual_observer_pid"] = act_pid
                data["actual_observer_start_time"] = act_time
                data["pid"] = act_pid
                data["start_time"] = act_time
                data["status"] = "active"
                data["updated_at"] = datetime.now(timezone.utc).isoformat()
                self.write_atomic_json(self.reservation_file, data)

    def record_turn_completion(self, worker_token: str, state: str, turn_id: str):
        """
        Records turn completion and observer exit without releasing persistent work reservation.
        Work reservation remains held to block foreign work until acceptance review or handoff.
        """
        with self._thread_lock:
            with self.ownership_lock():
                if not self.reservation_file.exists():
                    return
                try:
                    with open(self.reservation_file, "r", encoding="utf-8") as f:
                        data = json.load(f)
                except Exception:
                    raise CollaborationError("corruption_detected", "Corrupted reservation file")

                if data.get("work_id") == self.work_id and data.get("worker_token") == worker_token:
                    data["status"] = state
                    data["observer_exited"] = True
                    data["completed_turn_id"] = turn_id
                    data["completed_at"] = datetime.now(timezone.utc).isoformat()
                    self.write_atomic_json(self.reservation_file, data)

    def acquire_reservation(self, pid: int, start_time: float, worker_token: str):
        """
        Atomically checks owner under shared execution lock.
        Enforces CAS ownership, idempotency for same identity, and blocks re-acquiring unknown/live work.
        """
        with self._thread_lock:
            with self.ownership_lock():
                self.ensure_dir()
                if self.reservation_file.exists():
                    try:
                        with open(self.reservation_file, "r", encoding="utf-8") as f:
                            data = json.load(f)
                    except Exception:
                        raise CollaborationError(
                            "corruption_detected",
                            "Corrupted reservation file in checkout"
                        )

                    existing_work_id = data.get("work_id")
                    if existing_work_id != self.work_id:
                        raise CollaborationError(
                            "checkout_busy",
                            f"Checkout is already reserved by work {existing_work_id}",
                            details={"existing_work_id": existing_work_id, "checkout_key": self.checkout_key}
                        )

                    # Same work reacquire validation
                    if data.get("execution_known") is False:
                        raise CollaborationError(
                            "checkout_busy",
                            "Checkout has an unverified execution_unknown reservation; manual recovery required"
                        )

                    # Same identity: idempotent success
                    if data.get("worker_token") == worker_token and (
                        data.get("pid") == pid
                        or data.get("actual_observer_pid") == pid
                        or data.get("launcher_pid") == pid
                    ):
                        return data

                    # Check if previous process is still alive:
                    existing_pid = data.get("actual_observer_pid") or data.get("pid")
                    existing_start = data.get("actual_observer_start_time") or data.get("start_time")
                    if existing_pid:
                        alive_check = is_process_alive(existing_pid, existing_start)
                        if alive_check in (True, "unknown"):
                            raise CollaborationError(
                                "checkout_busy",
                                f"Previous worker process (PID {existing_pid}) is still alive/active on this checkout"
                            )

                record = {
                    "checkout_key": self.checkout_key,
                    "work_id": self.work_id,
                    "pid": validate_strict_int(pid, "pid", min_val=1),
                    "start_time": validate_strict_float(start_time, "start_time", min_val=0.0, max_val=None),
                    "worker_token": validate_non_empty_str(worker_token, "worker_token"),
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "status": "active",
                    "execution_known": True,
                }
                self.write_atomic_json(self.reservation_file, record)

    def mark_execution_unknown(self, worker_token: str, reason: str):
        """Marks current reservation as execution_unknown, preventing release without recovery."""
        with self._thread_lock:
            with self.ownership_lock():
                if not self.reservation_file.exists():
                    return
                try:
                    with open(self.reservation_file, "r", encoding="utf-8") as f:
                        data = json.load(f)
                except Exception:
                    raise CollaborationError("corruption_detected", "Corrupted reservation file")

                if data.get("work_id") == self.work_id and data.get("worker_token") == worker_token:
                    data["execution_known"] = False
                    data["unknown_reason"] = sanitize_safe_output(reason)
                    data["unknown_marked_at"] = datetime.now(timezone.utc).isoformat()
                    self.write_atomic_json(self.reservation_file, data)

    def release_reservation(
        self,
        release_evidence: str,
        is_safe_completion: bool = False,
        is_recovery: bool = False,
        worker_token: Optional[str] = None,
        proof: Optional[Dict[str, Any]] = None,
        checkpoint_revision: Optional[int] = None,
    ):
        """
        Releases reservation only after verified safe completion and proven persisted state.
        UNKNOWN reservation release is strictly forbidden in this phase (reconciliation_required).
        Verifies exact identity, dead non-unknown process, terminal received, process exited,
        no pending tools, valid outcome/result, and consistent checkpoint revision.
        """
        if not is_safe_completion and not is_recovery:
            raise CollaborationError(
                "permission_denied",
                "Reservation release requires verified safe completion or explicit recovery evidence"
            )

        if not release_evidence or type(release_evidence) is not str or not release_evidence.strip():
            raise CollaborationError(
                "protocol_error",
                "Release evidence cannot be empty"
            )

        clean_token = validate_non_empty_str(worker_token, "worker_token")

        with self._thread_lock:
            with self.ownership_lock():
                if not self.reservation_file.exists():
                    return

                try:
                    with open(self.reservation_file, "r", encoding="utf-8") as f:
                        data = json.load(f)
                except Exception:
                    raise CollaborationError("corruption_detected", "Corrupted reservation file on release")

                if data.get("work_id") != self.work_id:
                    raise CollaborationError(
                        "permission_denied",
                        f"Cannot release reservation owned by work {data.get('work_id')}"
                    )

                if data.get("worker_token") != clean_token:
                    raise CollaborationError(
                        "permission_denied",
                        "Worker token mismatch on reservation release"
                    )

                # UNKNOWN reservation release is strictly forbidden even with recovery bool or MANUAL_AUDIT!
                if data.get("execution_known") is False:
                    raise CollaborationError(
                        "reconciliation_required",
                        "Cannot release reservation in execution_unknown state; recovery/reconciliation is unsupported in this phase"
                    )

                # In this phase, recovery release is unsupported (reconciliation_required)
                if is_recovery:
                    raise CollaborationError(
                        "reconciliation_required",
                        "Recovery release is unsupported in this phase; manual reconciliation required"
                    )

                # Process liveness check: must be verified dead (proc_state is False), never alive or unknown
                res_pid = data.get("actual_observer_pid") or data.get("pid")
                expected_start = data.get("actual_observer_start_time") or data.get("start_time")
                if res_pid is None or expected_start is None:
                    raise CollaborationError(
                        "permission_denied",
                        "Reservation record is missing process identity (pid or start_time)"
                    )

                proc_state = is_process_alive(res_pid, expected_start)
                if proc_state is not False:
                    raise CollaborationError(
                        "permission_denied",
                        f"Cannot release reservation while worker process (PID {res_pid}) is alive or active (state={proc_state})"
                    )

                # Check launcher process as well if present
                launcher_pid = data.get("launcher_pid")
                launcher_start = data.get("launcher_start_time")
                if launcher_pid and launcher_pid != res_pid:
                    l_state = is_process_alive(launcher_pid, launcher_start)
                    if l_state is True:
                        raise CollaborationError(
                            "permission_denied",
                            f"Cannot release reservation while launcher process (PID {launcher_pid}) is still alive"
                        )

                # Persisted Work snapshot and checkpoint verification
                with self.lock():
                    if not self.snapshot_file.exists():
                        raise CollaborationError(
                            "permission_denied",
                            f"Cannot release reservation: persisted work snapshot not found for work {self.work_id}"
                        )

                    try:
                        with open(self.snapshot_file, "r", encoding="utf-8") as f:
                            snap_data = json.load(f)
                        work = Work.from_dict(snap_data)
                    except CollaborationError:
                        raise
                    except Exception:
                        raise CollaborationError(
                            "corruption_detected",
                            f"Invalid schema or data in snapshot for work {self.work_id}"
                        )

                    if work.work_id != self.work_id:
                        raise CollaborationError("protocol_error", "Snapshot work_id mismatch")

                    latest_seq, latest_rev = self._get_latest_event_info_unlocked()
                    if latest_seq > 0 and latest_rev != work.revision:
                        raise CollaborationError(
                            "stale_checkpoint",
                            f"Snapshot revision ({work.revision}) does not match latest event revision ({latest_rev})"
                        )

                    if checkpoint_revision is not None and work.revision != checkpoint_revision:
                        raise CollaborationError(
                            "stale_checkpoint",
                            f"Caller expected checkpoint revision {checkpoint_revision}, but current revision is {work.revision}"
                        )

                    if proof is not None:
                        if not isinstance(proof, dict):
                            raise CollaborationError("protocol_error", "Proof must be a dictionary")
                        proof_rev = proof.get("checkpoint_revision") if "checkpoint_revision" in proof else proof.get("revision")
                        if proof_rev is not None and work.revision != proof_rev:
                            raise CollaborationError(
                                "stale_checkpoint",
                                f"Proof revision {proof_rev} does not match current snapshot revision {work.revision}"
                            )
                        proof_turn_id = proof.get("turn_id")
                        if proof_turn_id is not None:
                            if not work.current_turn or work.current_turn.turn_id != proof_turn_id:
                                raise CollaborationError(
                                    "permission_denied",
                                    f"Proof turn_id {proof_turn_id} does not match persisted current_turn"
                                )
                        proof_token = proof.get("worker_token")
                        if proof_token is not None and proof_token != clean_token:
                            raise CollaborationError(
                                "permission_denied",
                                "Proof worker_token mismatch"
                            )

                    # Persisted Work.current_turn verification
                    turn = work.current_turn
                    if not turn:
                        raise CollaborationError(
                            "permission_denied",
                            "Cannot release reservation: no persisted current_turn in work state"
                        )

                    if turn.work_id != self.work_id:
                        raise CollaborationError(
                            "permission_denied",
                            f"Turn work_id mismatch: {turn.work_id} != {self.work_id}"
                        )

                    if turn.worker_token != clean_token:
                        raise CollaborationError(
                            "permission_denied",
                            f"Turn worker_token mismatch: {turn.worker_token} != {clean_token}"
                        )

                    if not is_recovery and work.state in ("in_review", "needs_answer"):
                        raise CollaborationError(
                            "permission_denied",
                            f"Cannot release reservation while work is {work.state}; reservation must be retained until acceptance review or handoff"
                        )

                    if turn.pid != res_pid:
                        raise CollaborationError(
                            "permission_denied",
                            f"Turn pid mismatch: {turn.pid} != {res_pid}"
                        )

                    if turn.start_time is None or abs(turn.start_time - expected_start) > 5.0:
                        raise CollaborationError(
                            "permission_denied",
                            f"Turn start_time mismatch: {turn.start_time} != {expected_start}"
                        )

                    # Execution invariants
                    if not turn.terminal_received:
                        raise CollaborationError(
                            "permission_denied",
                            "Cannot release reservation: terminal_received is false"
                        )

                    if not turn.process_exited:
                        raise CollaborationError(
                            "permission_denied",
                            "Cannot release reservation: process_exited is false"
                        )

                    if turn.pending_tools and len(turn.pending_tools) > 0:
                        raise CollaborationError(
                            "permission_denied",
                            f"Cannot release reservation: turn has pending tools: {turn.pending_tools}"
                        )

                    if not turn.outcome or turn.outcome not in ("SUCCESS", "ERROR"):
                        raise CollaborationError(
                            "permission_denied",
                            f"Cannot release reservation: invalid or missing turn outcome '{turn.outcome}'"
                        )

                    if turn.outcome == "SUCCESS":
                        if not work.last_result:
                            raise CollaborationError(
                                "permission_denied",
                                "Cannot release reservation: SUCCESS outcome requires persisted last_result"
                            )
                        if work.last_result.work_id != self.work_id:
                            raise CollaborationError(
                                "permission_denied",
                                f"last_result work_id mismatch: {work.last_result.work_id} != {self.work_id}"
                            )
                        if work.last_result.turn_id != turn.turn_id:
                            raise CollaborationError(
                                "permission_denied",
                                f"last_result turn_id mismatch: {work.last_result.turn_id} != {turn.turn_id}"
                            )

                    # Write release evidence record first
                    data["released"] = True
                    data["release_evidence"] = sanitize_safe_output(release_evidence.strip())
                    data["released_at"] = datetime.now(timezone.utc).isoformat()
                    data["is_recovery"] = False
                    data["released_revision"] = work.revision
                    data["released_turn_id"] = turn.turn_id
                    if proof is not None:
                        data["proof"] = sanitize_safe_output(proof)

                    evidence_file = self.checkout_dir / f"released_{self.work_id}.json"
                    self.write_atomic_json(evidence_file, data)

                    if self.reservation_file.exists():
                        self.reservation_file.unlink()
