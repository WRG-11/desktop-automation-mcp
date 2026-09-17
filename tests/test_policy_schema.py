"""Phase 3 policy file format tests (design draft).

Scope: the tools/validate_policy.py validator + example YAML policies
under schema/examples. Does not touch the existing tests/test_server.py;
runs standalone via `py -3.12 -m unittest tests/test_policy_schema.py -v`.
"""

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
VALIDATOR_PATH = REPO_ROOT / "tools" / "validate_policy.py"
RUFFLE_YAML = REPO_ROOT / "schema" / "examples" / "ruffle-policy.yaml"
NOTEPAD_YAML = REPO_ROOT / "schema" / "examples" / "notepad-text-policy.yaml"

_spec = importlib.util.spec_from_file_location("validate_policy", VALIDATOR_PATH)
assert _spec is not None and _spec.loader is not None
validate_policy = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(validate_policy)


def _future_stamp() -> str:
    moment = datetime.now(timezone.utc) + timedelta(days=30)
    return moment.isoformat(timespec="seconds")


def _past_stamp() -> str:
    moment = datetime.now(timezone.utc) - timedelta(days=1)
    return moment.isoformat(timespec="seconds")


def _base_policy() -> dict:
    return {
        "application": {
            "executable_path": r"C:\Program Files\ruffle\bin\ruffle.exe",
            "title_patterns": ["*Ruffle*"],
            "allowed_actions": ["observe", "hover", "click", "key"],
        }
    }


class PolicySchemaExampleTests(unittest.TestCase):
    def test_valid_ruffle_example_passes(self):
        doc = validate_policy.load_policy(RUFFLE_YAML)
        self.assertEqual(validate_policy.validate_policy(doc), [])

    def test_valid_notepad_example_passes(self):
        doc = validate_policy.load_policy(NOTEPAD_YAML)
        problems = validate_policy.validate_policy(doc)
        self.assertEqual(problems, [])
        # The "text and close are protected classes" principle should be concrete.
        protected = doc["application"]["protected_actions"]
        self.assertIn("text", protected)
        self.assertIn("close", protected)

    def test_validator_cli_reports_ok_with_exit_zero(self):
        proc = subprocess.run(
            [sys.executable, str(VALIDATOR_PATH), str(RUFFLE_YAML)],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        self.assertIn("OK", proc.stdout)


class PolicyCrossFieldTests(unittest.TestCase):
    def test_screenshot_privacy_constraints_have_strict_shapes(self):
        valid = _base_policy()
        valid["application"].update(
            safe_regions=[{"name": "content", "rect": [10, 20, 300, 220]}],
            screenshot_masks=[{"rect": [40, 50, 80, 90]}],
            max_screenshot_bytes=1_000_000,
        )
        self.assertEqual(validate_policy.validate_policy(valid), [])

        invalid = _base_policy()
        invalid["application"].update(
            safe_regions=[
                {"name": "duplicate", "rect": [10, 20, 5, 30]},
                {"name": "duplicate", "rect": [0, 0, 10, 10]},
            ],
            screenshot_masks=[{"rect": [-1, 0, 4, 4], "extra": True}],
            max_screenshot_bytes=0,
        )
        problems = validate_policy.validate_policy(invalid)
        joined = "\n".join(problems)
        self.assertIn("must be ordered", joined)
        self.assertIn("duplicate", joined)
        self.assertIn("negative", joined)
        self.assertIn("unknown field", joined)
        self.assertIn("max_screenshot_bytes", joined)

    def test_protected_outside_allowed_is_rejected(self):
        doc = _base_policy()
        doc["application"]["protected_actions"] = ["close"]
        problems = validate_policy.validate_policy(doc)
        self.assertTrue(
            any("subset" in p for p in problems),
            msg=f"expected subset error missing: {problems}",
        )

    def test_unknown_action_name_is_rejected(self):
        doc = _base_policy()
        doc["application"]["allowed_actions"] = ["observe", "teleport"]
        problems = validate_policy.validate_policy(doc)
        self.assertTrue(
            any("unknown action" in p and "teleport" in p for p in problems),
            msg=f"expected unknown-action error missing: {problems}",
        )

    def test_drag_is_supported_by_file_policy(self):
        doc = _base_policy()
        doc["application"]["allowed_actions"].append("drag")
        doc["application"]["protected_actions"] = ["drag"]
        self.assertEqual(validate_policy.validate_policy(doc), [])

    def test_expired_expires_at_is_rejected(self):
        doc = _base_policy()
        doc["application"]["expires_at"] = _past_stamp()
        problems = validate_policy.validate_policy(doc)
        self.assertTrue(
            any("expired" in p for p in problems),
            msg=f"expected expiry error missing: {problems}",
        )

    def test_future_expires_at_with_offset_passes(self):
        doc = _base_policy()
        doc["application"]["expires_at"] = _future_stamp()
        self.assertEqual(validate_policy.validate_policy(doc), [])

    def test_bare_z_expires_at_is_rejected(self):
        doc = _base_policy()
        doc["application"]["expires_at"] = "2027-01-01T00:00:00Z"
        problems = validate_policy.validate_policy(doc)
        self.assertTrue(
            any("offset" in p for p in problems),
            msg=f"expected offset error missing: {problems}",
        )

    def test_offsetless_expires_at_is_rejected(self):
        doc = _base_policy()
        doc["application"]["expires_at"] = "2027-01-01T00:00:00"
        problems = validate_policy.validate_policy(doc)
        self.assertTrue(
            any("offset" in p for p in problems),
            msg=f"expected offset error missing: {problems}",
        )

    def test_extra_root_field_is_rejected(self):
        doc = _base_policy()
        doc["unexpected"] = {"note": "unknown field must not be swallowed"}
        problems = validate_policy.validate_policy(doc)
        self.assertTrue(
            any("unknown field" in p for p in problems),
            msg=f"expected additionalProperties error missing: {problems}",
        )

    def test_missing_file_is_probe_error_not_invalid(self):
        with self.assertRaises(validate_policy.PolicyFileError) as ctx:
            validate_policy.load_policy(REPO_ROOT / "schema" / "yok.yaml")
        self.assertIn("not found", str(ctx.exception))

    def test_broken_file_is_probe_error_with_exit_two(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False, encoding="utf-8"
        ) as handle:
            handle.write("application:\n  title_patterns: [unterminated\n")
            broken = handle.name
        try:
            proc = subprocess.run(
                [sys.executable, str(VALIDATOR_PATH), broken],
                capture_output=True,
                text=True,
                cwd=str(REPO_ROOT),
            )
        finally:
            Path(broken).unlink(missing_ok=True)
        self.assertEqual(proc.returncode, 2, msg=proc.stdout)
        self.assertIn("FILE ERROR", proc.stderr)

    def test_invalid_policy_cli_exits_one_with_rule_message(self):
        doc = _base_policy()
        doc["application"]["allowed_actions"] = ["observe", "flightmode"]
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        ) as handle:
            json.dump(doc, handle)
            invalid = handle.name
        try:
            proc = subprocess.run(
                [sys.executable, str(VALIDATOR_PATH), invalid],
                capture_output=True,
                text=True,
                cwd=str(REPO_ROOT),
            )
        finally:
            Path(invalid).unlink(missing_ok=True)
        self.assertEqual(proc.returncode, 1, msg=proc.stderr)
        self.assertIn("unknown action", proc.stdout)


if __name__ == "__main__":
    unittest.main()
