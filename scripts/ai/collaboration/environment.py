"""
Environment discovery, doctor diagnostics, and read-only quota adapter
for Codex and Antigravity collaboration (Feature 007, Tasks T008, T048).
"""

import os
import sys
import json
import time
import math
import uuid
import shutil
import threading
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Any, Tuple, Union

from .state import (
    CollaborationError,
    QuotaSnapshot,
    validate_strict_float,
    validate_non_empty_str,
    sanitize_safe_output,
    VALID_QUOTA_AVAILABILITIES,
    find_git_root,
)

VERIFIED_QUOTA_GROUPS = {
    "gemini": "Gemini Models",
    "claude": "Claude and GPT models"
}

APPROVED_MODEL_FAMILIES = {
    "gemini": ["gemini-"],
    "claude": ["claude-", "gpt-", "o1-", "o3-"]
}


def discover_executable(
    name: str,
    arg_override: Optional[Union[str, Path]] = None,
    explicit_override: Optional[Union[str, Path]] = None,
    env_override_name: Optional[str] = None,
    env_var: Optional[str] = None,
    cwd: Optional[Path] = None,
) -> Optional[Path]:
    """
    Resolves native executable path by checking override, environment variables, PATH,
    or platform-specific standard locations. Rejects .bat and .cmd launcher scripts.
    """
    override = arg_override if arg_override is not None else explicit_override

    if override is not None:
        cand_str = str(override).strip()
        if cand_str.lower().endswith(".bat") or cand_str.lower().endswith(".cmd"):
            raise CollaborationError(
                "tool_unavailable",
                f"Batch script launcher '{cand_str}' is not a native executable"
            )
        p = Path(override).resolve()
        if p.is_file():
            if p.suffix.lower() in (".bat", ".cmd"):
                raise CollaborationError(
                    "tool_unavailable",
                    f"Batch script launcher '{p}' is not a native executable"
                )
            return p
        return None

    # Python defaults to sys.executable in current active environment
    if name.lower() in ("python", "python3", "python.exe", "python3.exe"):
        return Path(sys.executable).resolve()

    # Check environment variable overrides
    env_key = env_override_name or env_var
    if not env_key:
        if name.lower() in ("agy", "agy.exe"):
            env_key = "AGY_BIN_PATH"
        elif name.lower() in ("codex", "codex.exe"):
            env_key = "CODEX_BIN_PATH"

    if env_key and os.environ.get(env_key):
        val = os.environ[env_key].strip()
        if val.lower().endswith(".bat") or val.lower().endswith(".cmd"):
            raise CollaborationError(
                "tool_unavailable",
                f"Batch script launcher '{val}' is not a native executable"
            )
        p = Path(val).resolve()
        if p.is_file():
            if p.suffix.lower() in (".bat", ".cmd"):
                raise CollaborationError(
                    "tool_unavailable",
                    f"Batch script launcher '{p}' is not a native executable"
                )
            return p

    # Check PATH via shutil.which, filtering out .bat / .cmd
    found = shutil.which(name)
    if found:
        cand = Path(found).resolve()
        if cand.suffix.lower() not in (".bat", ".cmd") and cand.is_file():
            return cand

    # Standard native discovery on Windows
    if sys.platform == "win32":
        localappdata = os.environ.get("LOCALAPPDATA")
        appdata = os.environ.get("APPDATA")
        program_files = os.environ.get("ProgramFiles")

        candidates = []
        name_clean = name.lower().replace(".exe", "")

        if localappdata:
            candidates.append(Path(localappdata) / "Programs" / name_clean / f"{name_clean}.exe")
            if name_clean == "agy":
                candidates.append(Path(localappdata) / "Antigravity" / "bin" / "agy.exe")
        if program_files:
            candidates.append(Path(program_files) / name_clean / f"{name_clean}.exe")
            if name_clean == "agy":
                candidates.append(Path(program_files) / "Antigravity" / "bin" / "agy.exe")
        if appdata:
            candidates.append(Path(appdata) / "uv" / "tools" / name_clean / "bin" / f"{name_clean}.exe")

        for cand in candidates:
            if cand.is_file() and cand.suffix.lower() not in (".bat", ".cmd"):
                return cand.resolve()

    return None


def verify_agy_capabilities(agy_exe: Path) -> Dict[str, bool]:
    """Verifies that agy CLI supports required headless flags without invoking model turns."""
    caps = {
        "supports_input_format": False,
        "supports_output_format": False,
        "supports_json_schema": False,
        "supports_mode": False,
        "supports_print_cmd": False,
    }
    try:
        r = subprocess.run(
            [str(agy_exe), "--help"],
            capture_output=True,
            text=True,
            timeout=5.0,
            check=False
        )
        help_text = r.stdout + "\n" + r.stderr
        caps["supports_input_format"] = "--input-format" in help_text
        caps["supports_output_format"] = "--output-format" in help_text
        caps["supports_json_schema"] = "--json-schema" in help_text
        caps["supports_mode"] = "--mode" in help_text
        caps["supports_print_cmd"] = "-p" in help_text or "--print" in help_text
    except Exception:
        pass
    return caps


def doctor(
    project_root: Optional[Union[str, Path]] = None,
    agy_exe: Optional[Union[str, Path]] = None,
    codex_exe: Optional[Union[str, Path]] = None,
    python_exe: Optional[Union[str, Path]] = None,
    check_android: bool = False,
    allow_outside_script: bool = True,
    agy_override: Optional[Union[str, Path]] = None,
    codex_override: Optional[Union[str, Path]] = None,
    python_override: Optional[Union[str, Path]] = None,
) -> Dict[str, Any]:
    """
    Performs complete read-only system and dependency capability check (T008).
    Unified interface for CLI and tests. Never prints or transmits API keys or secrets.
    """
    results = {
        "ok": True,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "project_root": None,
        "diagnostics": {},
        "errors": [],
    }

    # 1. Project root
    try:
        root = find_git_root(override=project_root, allow_outside=allow_outside_script)
        results["project_root"] = str(root)
        is_git = (root / ".git").exists()
        results["diagnostics"]["git_root"] = str(root)
        results["diagnostics"]["is_git_repository"] = is_git
        if not is_git:
            results["ok"] = False
            results["errors"].append(f"Directory {root} is not a valid Git repository root")
    except CollaborationError as e:
        results["ok"] = False
        results["diagnostics"]["git_root"] = None
        results["diagnostics"]["is_git_repository"] = False
        results["errors"].append(f"Git repository discovery failed: {e.message}")
        root = Path.cwd()

    # 2. Python environment & MCP package
    actual_py = python_override or python_exe
    try:
        py_path = discover_executable("python", arg_override=actual_py)
    except CollaborationError as e:
        py_path = None
        results["errors"].append(f"Python executable discovery error: {e.message}")

    if not py_path:
        py_path = Path(sys.executable).resolve()

    results["diagnostics"]["python_exe"] = str(py_path)
    py_ver_str = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    results["diagnostics"]["python_version"] = py_ver_str

    # Query MCP package info for the selected Python
    mcp_installed = False
    mcp_version = None

    if py_path == Path(sys.executable).resolve():
        try:
            import importlib.metadata
            mcp_version = importlib.metadata.version("mcp")
            mcp_installed = True
        except Exception:
            try:
                import mcp
                mcp_version = getattr(mcp, "__version__", "unknown")
                mcp_installed = True
            except ImportError:
                mcp_installed = False
                mcp_version = None
    else:
        try:
            r = subprocess.run(
                [str(py_path), "-c", "import importlib.metadata; print(importlib.metadata.version('mcp'))"],
                capture_output=True,
                text=True,
                timeout=5.0,
                check=False
            )
            if r.returncode == 0 and r.stdout.strip():
                mcp_installed = True
                mcp_version = r.stdout.strip()
        except Exception:
            pass

    results["diagnostics"]["mcp_installed"] = mcp_installed
    results["diagnostics"]["mcp_version"] = mcp_version

    py_diag = {
        "executable": str(py_path),
        "version": py_ver_str,
        "version_compatible": sys.version_info >= (3, 12),
        "mcp_installed": mcp_installed,
        "mcp_version": mcp_version,
    }
    if not py_diag["version_compatible"]:
        results["ok"] = False
        results["errors"].append(f"Python 3.12+ required, found {py_diag['version']}")
    if not mcp_installed:
        results["ok"] = False
        results["errors"].append("Required package 'mcp' is not installed in the active environment")

    results["diagnostics"]["python"] = py_diag

    # 3. Antigravity CLI
    actual_agy = agy_override or agy_exe
    try:
        agy_path = discover_executable("agy", arg_override=actual_agy)
    except CollaborationError as e:
        agy_path = None
        results["errors"].append(f"Antigravity CLI discovery error: {e.message}")

    agy_diag = {
        "discovered": agy_path is not None,
        "path": str(agy_path) if agy_path else None,
        "capabilities": {},
    }
    if agy_path:
        caps = verify_agy_capabilities(agy_path)
        agy_diag["capabilities"] = caps
    results["diagnostics"]["antigravity_cli"] = agy_diag

    # 4. Codex CLI
    actual_codex = codex_override or codex_exe
    try:
        codex_path = discover_executable("codex", arg_override=actual_codex)
    except CollaborationError as e:
        codex_path = None
        results["errors"].append(f"Codex CLI discovery error: {e.message}")

    codex_diag = {
        "discovered": codex_path is not None,
        "path": str(codex_path) if codex_path else None,
    }
    results["diagnostics"]["codex_cli"] = codex_diag

    # 5. Optional Android toolchain
    if check_android:
        android_diag = {
            "jdk_ok": False,
            "sdk_ok": False,
            "wrapper_ok": False,
        }
        java_home = os.environ.get("JAVA_HOME")
        if java_home and Path(java_home).is_dir():
            android_diag["jdk_ok"] = True
        android_home = os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT")
        if android_home and Path(android_home).is_dir():
            android_diag["sdk_ok"] = True
        wrapper_bat = root / "android" / "gradlew.bat"
        if wrapper_bat.is_file():
            android_diag["wrapper_ok"] = True
        results["diagnostics"]["android"] = android_diag

    return results


# -----------------------------------------------------------------------------
# Read-only Quota Adapter (T048)
# -----------------------------------------------------------------------------

class QuotaAdapter:
    """
    Read-only adapter querying agy /model and /usage via bounded external subprocesses (T048).
    Strictly verifies applicable groups ('Gemini Models', 'Claude and GPT models'),
    numeric float fraction 0..1, future UTC reset timestamps, and positive threshold.
    """
    def __init__(
        self,
        agy_exe: Path,
        threshold: float = 0.20,
        default_timeout: float = 15.0,
        cli_args_prefix: Optional[List[str]] = None,
        max_buffer_bytes: int = 64 * 1024,
    ):
        self.agy_exe = Path(agy_exe)
        self.threshold = validate_strict_float(threshold, "threshold", min_val=0.0, max_val=1.0)
        self.default_timeout = validate_strict_float(default_timeout, "default_timeout", min_val=0.01, max_val=None)
        self.cli_args_prefix = list(cli_args_prefix or [])
        self.max_buffer_bytes = max_buffer_bytes

    def _run_print_command(self, cmd_name: str, timeout: float) -> Tuple[bool, Optional[Dict[str, Any]], str]:
        """
        Executes read-only print command with explicit UTF-8, bounded threaded output capture,
        safe process kill on timeout, and strict wire envelope validation.
        Concurrent reading ensures pipe buffer overflows (>64 KB) never deadlock.
        """
        cmd = [str(self.agy_exe)]
        if self.cli_args_prefix:
            cmd.extend(self.cli_args_prefix)
        cmd.extend(["-p", cmd_name, "--output-format", "json"])

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
            )
        except Exception:
            return False, None, f"Failed to spawn print command for {cmd_name}"

        stdout_chunks: List[bytes] = []
        stderr_chunks: List[bytes] = []
        stdout_len = [0]
        stderr_len = [0]

        def read_stream(stream, chunks, length_ref):
            try:
                while True:
                    chunk = stream.read(4096)
                    if not chunk:
                        break
                    chunks.append(chunk)
                    length_ref[0] += len(chunk)
                    if length_ref[0] > self.max_buffer_bytes + 1:
                        # Bounded read to prevent memory exhaustion
                        break
            except Exception:
                pass
            finally:
                try:
                    stream.close()
                except Exception:
                    pass

        t_out = threading.Thread(target=read_stream, args=(proc.stdout, stdout_chunks, stdout_len), daemon=True)
        t_err = threading.Thread(target=read_stream, args=(proc.stderr, stderr_chunks, stderr_len), daemon=True)
        t_out.start()
        t_err.start()

        start_time = time.monotonic()
        timed_out = False

        while time.monotonic() - start_time < timeout:
            if proc.poll() is not None:
                break
            time.sleep(0.01)

        if proc.poll() is None:
            timed_out = True
            try:
                proc.kill()
                proc.wait(timeout=1.0)
            except Exception:
                pass

        t_out.join(timeout=1.0)
        t_err.join(timeout=1.0)

        stdout_bytes = b"".join(stdout_chunks)

        if timed_out:
            return False, None, f"Command {cmd_name} timed out after {timeout}s"

        if len(stdout_bytes) > self.max_buffer_bytes:
            return False, None, f"Command {cmd_name} output exceeded maximum buffer limit"

        if proc.returncode != 0:
            return False, None, f"Command {cmd_name} process exited with status {proc.returncode}"

        try:
            output_text = stdout_bytes.decode("utf-8").strip()
        except UnicodeDecodeError:
            return False, None, f"Invalid UTF-8 in output from {cmd_name}"

        if not output_text:
            return False, None, f"Empty output from {cmd_name}"

        try:
            data = json.loads(output_text)
        except json.JSONDecodeError:
            return False, None, f"Malformed JSON from {cmd_name}"

        if not isinstance(data, dict):
            return False, None, f"Output from {cmd_name} is not a JSON object"

        # Strict wire status verification: must be SUCCESS
        if data.get("status") != "SUCCESS":
            return False, None, f"Command {cmd_name} returned non-success status: {data.get('status')}"

        cmd_envelope = data.get("command")
        if not isinstance(cmd_envelope, dict):
            return False, None, f"Missing or malformed 'command' envelope in {cmd_name} response"

        # Real agy wire contract returns command.name without leading slash for known print commands
        expected_cmd_name = cmd_name.lstrip("/") if cmd_name in ("/model", "/usage") else cmd_name
        actual_cmd_name = cmd_envelope.get("name")
        if not actual_cmd_name or actual_cmd_name != expected_cmd_name:
            return False, None, f"Command name mismatch in envelope: expected {expected_cmd_name}, got {actual_cmd_name}"

        if not isinstance(cmd_envelope.get("data"), dict):
            return False, None, f"Command data payload in {cmd_name} is not an object"

        return True, data, ""

    def check_quota(
        self,
        model_override: Optional[str] = None,
        timeout: Optional[float] = None
    ) -> QuotaSnapshot:
        snapshot_id = str(uuid.uuid4())
        checked_at = datetime.now(timezone.utc).isoformat()
        t = timeout or self.default_timeout

        # 1. Fetch /model
        ok_m, model_data, err_m = self._run_print_command("/model", t)
        if not ok_m:
            return QuotaSnapshot(
                snapshot_id=snapshot_id,
                checked_at=checked_at,
                source="agy_cli",
                model_id=model_override or "unknown",
                group_name="unknown",
                availability="unknown",
                buckets=[],
                threshold=self.threshold,
                uncertainty_reason=f"Failed to query /model: {err_m}"
            )

        cmd_model_data = model_data.get("command", {}).get("data", {})
        active_model_id = model_override or cmd_model_data.get("id")
        if not active_model_id or type(active_model_id) is not str:
            return QuotaSnapshot(
                snapshot_id=snapshot_id,
                checked_at=checked_at,
                source="agy_cli",
                model_id="unknown",
                group_name="unknown",
                availability="unknown",
                buckets=[],
                threshold=self.threshold,
                uncertainty_reason="Model ID missing or invalid in /model output"
            )

        # Validate against recognized approved model family prefixes
        model_lower = active_model_id.lower().strip()
        expected_group = None
        for prefix in APPROVED_MODEL_FAMILIES["gemini"]:
            if model_lower.startswith(prefix):
                expected_group = VERIFIED_QUOTA_GROUPS["gemini"]
                break
        if not expected_group:
            for prefix in APPROVED_MODEL_FAMILIES["claude"]:
                if model_lower.startswith(prefix):
                    expected_group = VERIFIED_QUOTA_GROUPS["claude"]
                    break

        if not expected_group:
            return QuotaSnapshot(
                snapshot_id=snapshot_id,
                checked_at=checked_at,
                source="agy_cli",
                model_id=active_model_id,
                group_name="unknown",
                availability="unknown",
                buckets=[],
                threshold=self.threshold,
                uncertainty_reason=f"Unrecognized model family for '{active_model_id}'; cannot determine quota group"
            )

        # 2. Fetch /usage
        ok_u, usage_data, err_u = self._run_print_command("/usage", t)
        if not ok_u:
            return QuotaSnapshot(
                snapshot_id=snapshot_id,
                checked_at=checked_at,
                source="agy_cli",
                model_id=active_model_id,
                group_name=expected_group,
                availability="unknown",
                buckets=[],
                threshold=self.threshold,
                uncertainty_reason=f"Failed to query /usage: {err_u}"
            )

        groups = usage_data.get("command", {}).get("data", {}).get("groups", [])
        if type(groups) is not list:
            return QuotaSnapshot(
                snapshot_id=snapshot_id,
                checked_at=checked_at,
                source="agy_cli",
                model_id=active_model_id,
                group_name=expected_group,
                availability="unknown",
                buckets=[],
                threshold=self.threshold,
                uncertainty_reason="Malformed groups in /usage output"
            )

        # Detect duplicate groups
        group_names = [g.get("name") for g in groups if isinstance(g, dict)]
        if len(group_names) != len(set(group_names)):
            return QuotaSnapshot(
                snapshot_id=snapshot_id,
                checked_at=checked_at,
                source="agy_cli",
                model_id=active_model_id,
                group_name=expected_group,
                availability="unknown",
                buckets=[],
                threshold=self.threshold,
                uncertainty_reason="Duplicate group names detected in /usage payload"
            )

        matched_group = None
        for grp in groups:
            if isinstance(grp, dict) and grp.get("name") == expected_group:
                matched_group = grp
                break

        if not matched_group:
            return QuotaSnapshot(
                snapshot_id=snapshot_id,
                checked_at=checked_at,
                source="agy_cli",
                model_id=active_model_id,
                group_name=expected_group,
                availability="unknown",
                buckets=[],
                threshold=self.threshold,
                uncertainty_reason=f"Applicable quota group '{expected_group}' not found in /usage output"
            )

        raw_buckets = matched_group.get("buckets", [])
        if not raw_buckets or type(raw_buckets) is not list:
            return QuotaSnapshot(
                snapshot_id=snapshot_id,
                checked_at=checked_at,
                source="agy_cli",
                model_id=active_model_id,
                group_name=expected_group,
                availability="unknown",
                buckets=[],
                threshold=self.threshold,
                uncertainty_reason=f"No buckets found for group '{expected_group}'"
            )

        normalized_buckets = []
        is_exhausted = False
        is_low = False
        has_invalid_fraction = False
        has_invalid_reset = False
        has_invalid_metadata = False
        now_utc = datetime.now(timezone.utc)

        for b in raw_buckets:
            if not isinstance(b, dict):
                has_invalid_metadata = True
                continue

            b_id = b.get("id")
            b_name = b.get("name")
            b_window = b.get("window")
            if not b_id or not isinstance(b_id, str) or not b_name or not isinstance(b_name, str) or not b_window or not isinstance(b_window, str):
                has_invalid_metadata = True

            rf = b.get("remaining_fraction")
            if rf is None or isinstance(rf, bool) or type(rf) not in (int, float):
                has_invalid_fraction = True
                rf_float = None
            else:
                rf_float = float(rf)
                if math.isnan(rf_float) or math.isinf(rf_float) or not (0.0 <= rf_float <= 1.0):
                    has_invalid_fraction = True
                    rf_float = None

            # Verify reset_time: must be aware ISO-8601 string in the future
            reset_time_str = b.get("reset_time")
            if not reset_time_str or not isinstance(reset_time_str, str):
                has_invalid_reset = True
            else:
                try:
                    clean_ts = reset_time_str.replace("Z", "+00:00")
                    reset_dt = datetime.fromisoformat(clean_ts)
                    if reset_dt.tzinfo is None:
                        # Naive timestamp rejected
                        has_invalid_reset = True
                    elif reset_dt <= now_utc:
                        # Expired reset timestamp
                        has_invalid_reset = True
                except Exception:
                    has_invalid_reset = True

            if rf_float is not None:
                if rf_float <= 0.0:
                    is_exhausted = True
                elif rf_float <= self.threshold:
                    is_low = True

            normalized_buckets.append({
                "id": str(b_id or "bucket"),
                "name": str(b_name or "Bucket"),
                "window": str(b_window or "unknown"),
                "remaining_fraction": rf_float,
                "reset_time": reset_time_str
            })

        if has_invalid_fraction:
            availability = "unknown"
            reason = "One or more buckets contain invalid/missing remaining_fraction"
        elif has_invalid_reset:
            availability = "unknown"
            reason = "One or more buckets contain missing, naive, or expired reset_time"
        elif has_invalid_metadata:
            availability = "unknown"
            reason = "One or more buckets contain invalid or missing window/name/id metadata"
        elif is_exhausted:
            availability = "exhausted"
            reason = None
        elif is_low:
            availability = "low"
            reason = None
        else:
            availability = "available"
            reason = None

        return QuotaSnapshot(
            snapshot_id=snapshot_id,
            checked_at=checked_at,
            source="agy_cli",
            model_id=active_model_id,
            group_name=expected_group,
            availability=availability,
            buckets=normalized_buckets,
            threshold=self.threshold,
            uncertainty_reason=reason
        )
