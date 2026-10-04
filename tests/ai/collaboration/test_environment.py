"""
Unit tests for environment discovery, doctor diagnostics, capability verification,
and read-only quota adapter (Tasks T008, T048).
"""

import os
import sys
import shutil
import tempfile
import unittest
from pathlib import Path

# Add project root to sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.ai.collaboration.environment import (
    find_git_root,
    discover_executable,
    verify_agy_capabilities,
    doctor,
    CollaborationError,
    QuotaAdapter,
    QuotaSnapshot,
    APPROVED_MODEL_FAMILIES,
    VERIFIED_QUOTA_GROUPS,
)

FAKE_CLI_PATH = Path(__file__).resolve().parent / "fake_cli.py"


class TestEnvironmentAndDoctor(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_find_git_root_valid_and_invalid(self):
        # 1. Non-git directory raises tool_unavailable
        non_git = Path(self.temp_dir) / "not_git"
        non_git.mkdir()
        with self.assertRaises(CollaborationError) as ctx:
            find_git_root(override=str(non_git))
        self.assertEqual(ctx.exception.code, "tool_unavailable")

        # 2. Fake git root with .git folder
        fake_git = Path(self.temp_dir) / "fake_git"
        fake_git.mkdir()
        (fake_git / ".git").mkdir()

        root = find_git_root(override=str(fake_git))
        self.assertEqual(root, fake_git.resolve())

        # 3. Script outside root without allow_outside raises tool_unavailable
        outside_script = non_git / "script.py"
        with self.assertRaises(CollaborationError) as ctx:
            find_git_root(script_path=outside_script, override=str(fake_git), allow_outside=False)
        self.assertEqual(ctx.exception.code, "tool_unavailable")

    def test_discover_executable_rejects_bat_and_cmd(self):
        fake_bin = Path(self.temp_dir) / "fake_bin"
        fake_bin.mkdir()

        # Batch file
        bat_file = fake_bin / "run.bat"
        bat_file.write_text("@echo off", encoding="utf-8")

        with self.assertRaises(CollaborationError) as ctx:
            discover_executable("run", arg_override=str(bat_file))
        self.assertEqual(ctx.exception.code, "tool_unavailable")
        self.assertIn("Batch script launcher", ctx.exception.message)

        # Cmd file
        cmd_file = fake_bin / "run.cmd"
        cmd_file.write_text("@echo off", encoding="utf-8")

        with self.assertRaises(CollaborationError) as ctx:
            discover_executable("run", arg_override=str(cmd_file))
        self.assertEqual(ctx.exception.code, "tool_unavailable")

    def test_discover_python_defaults_to_sys_executable(self):
        py_exe = discover_executable("python")
        self.assertEqual(py_exe, Path(sys.executable).resolve())

    def test_doctor_structure_and_mcp_version(self):
        # Run doctor on current repo
        diag = doctor(project_root=str(PROJECT_ROOT), allow_outside_script=True)

        self.assertIn("git_root", diag["diagnostics"])
        self.assertIn("python_version", diag["diagnostics"])
        self.assertIn("python_exe", diag["diagnostics"])
        self.assertEqual(diag["diagnostics"]["python_exe"], str(Path(sys.executable).resolve()))
        self.assertIn("mcp_installed", diag["diagnostics"])


class TestQuotaAdapterAndT048(unittest.TestCase):
    def setUp(self):
        os.environ.pop("FAKE_AGY_MODE", None)

    def tearDown(self):
        os.environ.pop("FAKE_AGY_MODE", None)

    def _create_adapter(self, threshold=0.20, default_timeout=10.0):
        return QuotaAdapter(
            agy_exe=Path(sys.executable),
            threshold=threshold,
            default_timeout=default_timeout,
            cli_args_prefix=[str(FAKE_CLI_PATH)],
        )

    def test_quota_adapter_gemini_models_available(self):
        os.environ["FAKE_AGY_MODE"] = "usage_ok"
        adapter = self._create_adapter()
        snapshot = adapter.check_quota(model_override="gemini-3.8-flash")

        self.assertEqual(snapshot.availability, "available")
        self.assertEqual(snapshot.group_name, "Gemini Models")
        self.assertGreater(snapshot.buckets[0]["remaining_fraction"], 0.20)
        self.assertIsNone(snapshot.uncertainty_reason)

    def test_quota_adapter_claude_gpt_group(self):
        os.environ["FAKE_AGY_MODE"] = "usage_ok"
        adapter = self._create_adapter()
        snapshot = adapter.check_quota(model_override="claude-3-7-sonnet")

        self.assertEqual(snapshot.availability, "available")
        self.assertEqual(snapshot.group_name, "Claude and GPT models")

    def test_quota_adapter_strict_model_prefix_rejection(self):
        # Substring prefix matching like 'xgemini-fake-model' must be rejected (must start with gemini-)
        os.environ["FAKE_AGY_MODE"] = "model_xgemini_substring"
        adapter = self._create_adapter()
        snapshot = adapter.check_quota()

        self.assertEqual(snapshot.availability, "unknown")
        self.assertEqual(snapshot.group_name, "unknown")
        self.assertIn("Unrecognized model family", snapshot.uncertainty_reason)

    def test_quota_adapter_canary_in_model_stderr_not_leaked(self):
        # Canary token written to stderr by /model command must NOT leak into uncertainty_reason
        canary = "CANARY_SECRET_MODEL_112233"
        os.environ["FAKE_AGY_MODE"] = "model_canary_stderr"
        adapter = self._create_adapter()
        snapshot = adapter.check_quota()

        self.assertEqual(snapshot.availability, "unknown")
        self.assertNotIn(canary, str(snapshot.uncertainty_reason))
        self.assertNotIn(canary, str(snapshot.to_dict()))

    def test_quota_adapter_canary_in_usage_stderr_not_leaked(self):
        # Canary token written to stderr by /usage command must NOT leak into uncertainty_reason
        canary = "CANARY_SECRET_USAGE_998877"
        os.environ["FAKE_AGY_MODE"] = "usage_canary_stderr"
        adapter = self._create_adapter()
        snapshot = adapter.check_quota(model_override="gemini-3.8-flash")

        self.assertEqual(snapshot.availability, "unknown")
        self.assertNotIn(canary, str(snapshot.uncertainty_reason))
        self.assertNotIn(canary, str(snapshot.to_dict()))

    def test_quota_adapter_non_success_wire_status(self):
        # Non-success wire status in /model must be rejected
        os.environ["FAKE_AGY_MODE"] = "model_unsupported_status"
        adapter = self._create_adapter()
        snapshot = adapter.check_quota()

        self.assertEqual(snapshot.availability, "unknown")
        self.assertIn("non-success status", snapshot.uncertainty_reason)

    def test_quota_adapter_missing_usage_status(self):
        # Missing status envelope in /usage must be rejected
        os.environ["FAKE_AGY_MODE"] = "usage_missing_status"
        adapter = self._create_adapter()
        snapshot = adapter.check_quota(model_override="gemini-3.8-flash")

        self.assertEqual(snapshot.availability, "unknown")
        self.assertIn("non-success status", snapshot.uncertainty_reason)

    def test_quota_adapter_malformed_shape(self):
        # Malformed command shape in /model must be rejected
        os.environ["FAKE_AGY_MODE"] = "model_malformed_shape"
        adapter = self._create_adapter()
        snapshot = adapter.check_quota()

        self.assertEqual(snapshot.availability, "unknown")
        self.assertIn("Missing or malformed 'command' envelope", snapshot.uncertainty_reason)

    def test_quota_adapter_boolean_fraction_rejected(self):
        os.environ["FAKE_AGY_MODE"] = "usage_boolean_fraction"
        adapter = self._create_adapter()
        snapshot = adapter.check_quota(model_override="gemini-3.8-flash")

        self.assertEqual(snapshot.availability, "unknown")
        self.assertIn("invalid/missing remaining_fraction", snapshot.uncertainty_reason)

    def test_quota_adapter_nan_fraction_rejected(self):
        os.environ["FAKE_AGY_MODE"] = "usage_nan_fraction"
        adapter = self._create_adapter()
        snapshot = adapter.check_quota(model_override="gemini-3.8-flash")

        self.assertEqual(snapshot.availability, "unknown")
        self.assertIn("invalid/missing remaining_fraction", snapshot.uncertainty_reason)

    def test_quota_adapter_missing_fraction_rejected(self):
        os.environ["FAKE_AGY_MODE"] = "usage_missing_fraction"
        adapter = self._create_adapter()
        snapshot = adapter.check_quota(model_override="gemini-3.8-flash")

        self.assertEqual(snapshot.availability, "unknown")
        self.assertIn("invalid/missing remaining_fraction", snapshot.uncertainty_reason)

    def test_quota_adapter_naive_reset_timestamp_rejected(self):
        # Reset timestamp without timezone information must be rejected
        os.environ["FAKE_AGY_MODE"] = "usage_naive_reset"
        adapter = self._create_adapter()
        snapshot = adapter.check_quota(model_override="gemini-3.8-flash")

        self.assertEqual(snapshot.availability, "unknown")
        self.assertIn("missing, naive, or expired reset_time", snapshot.uncertainty_reason)

    def test_quota_adapter_expired_reset_timestamp_rejected(self):
        # Reset timestamp in past must be rejected
        os.environ["FAKE_AGY_MODE"] = "usage_expired_reset"
        adapter = self._create_adapter()
        snapshot = adapter.check_quota(model_override="gemini-3.8-flash")

        self.assertEqual(snapshot.availability, "unknown")
        self.assertIn("missing, naive, or expired reset_time", snapshot.uncertainty_reason)

    def test_quota_adapter_missing_window_metadata_rejected(self):
        # Missing window string metadata must mark availability as unknown
        os.environ["FAKE_AGY_MODE"] = "usage_missing_window"
        adapter = self._create_adapter()
        snapshot = adapter.check_quota(model_override="gemini-3.8-flash")

        self.assertEqual(snapshot.availability, "unknown")
        self.assertIn("invalid or missing window/name/id metadata", snapshot.uncertainty_reason)

    def test_quota_adapter_timeout_bounded_executor(self):
        # Bounded subprocess execution terminates cleanly without zombie upon timeout
        os.environ["FAKE_AGY_MODE"] = "usage_timeout"
        adapter = self._create_adapter(default_timeout=0.1)
        snapshot = adapter.check_quota(model_override="gemini-3.8-flash", timeout=0.1)

        self.assertEqual(snapshot.availability, "unknown")
        self.assertIn("timed out", snapshot.uncertainty_reason.lower())

    def test_quota_adapter_threshold_exhausted_and_low(self):
        # Fraction 0.0 -> exhausted
        os.environ["FAKE_AGY_MODE"] = "usage_exhausted"
        adapter = self._create_adapter(threshold=0.20)
        snapshot = adapter.check_quota(model_override="gemini-3.8-flash")
        self.assertEqual(snapshot.availability, "exhausted")

        # Fraction 0.15 (below 0.20) -> low
        os.environ["FAKE_AGY_MODE"] = "usage_low"
        snapshot_low = adapter.check_quota(model_override="gemini-3.8-flash")
        self.assertEqual(snapshot_low.availability, "low")

    def test_quota_adapter_real_wire_command_names_success(self):
        # Real agy wire contract returns command.name without leading slash ('model', 'usage')
        adapter = self._create_adapter()
        ok_m, model_data, err_m = adapter._run_print_command("/model", 10.0)
        self.assertTrue(ok_m, f"_run_print_command(/model) failed: {err_m}")
        self.assertEqual(model_data["command"]["name"], "model")

        ok_u, usage_data, err_u = adapter._run_print_command("/usage", 10.0)
        self.assertTrue(ok_u, f"_run_print_command(/usage) failed: {err_u}")
        self.assertEqual(usage_data["command"]["name"], "usage")

    def test_quota_adapter_wrong_command_name_rejected(self):
        # Mismatched/foreign command.name in response must be strictly rejected
        os.environ["FAKE_AGY_MODE"] = "model_wrong_command_name"
        adapter = self._create_adapter()
        snapshot = adapter.check_quota()

        self.assertEqual(snapshot.availability, "unknown")
        self.assertIn("Command name mismatch in envelope: expected model, got wrong_model", snapshot.uncertainty_reason)

    def test_quota_adapter_slash_command_name_rejected_without_permissive_fallback(self):
        # Old slash-prefixed command.name ('/model') must be rejected without permissive fallback
        os.environ["FAKE_AGY_MODE"] = "model_slash_command_name"
        adapter = self._create_adapter()
        snapshot = adapter.check_quota()

        self.assertEqual(snapshot.availability, "unknown")
        self.assertIn("Command name mismatch in envelope: expected model, got /model", snapshot.uncertainty_reason)

    def test_quota_adapter_missing_command_name_rejected(self):
        # Missing command.name must be rejected
        os.environ["FAKE_AGY_MODE"] = "model_missing_command_name"
        adapter = self._create_adapter()
        snapshot = adapter.check_quota()

        self.assertEqual(snapshot.availability, "unknown")
        self.assertIn("Command name mismatch in envelope: expected model, got None", snapshot.uncertainty_reason)

    def test_quota_adapter_wrong_usage_command_name_rejected(self):
        # Mismatched command.name for /usage must be strictly rejected
        os.environ["FAKE_AGY_MODE"] = "usage_wrong_command_name"
        adapter = self._create_adapter()
        snapshot = adapter.check_quota(model_override="gemini-3.8-flash")

        self.assertEqual(snapshot.availability, "unknown")
        self.assertIn("Command name mismatch in envelope: expected usage, got wrong_usage", snapshot.uncertainty_reason)


if __name__ == "__main__":
    unittest.main()
