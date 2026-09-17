"""Phase 3 confirmation token schema tests (design draft).

Scope: tools/validate_confirmation_token.py + the example token under
schema/examples. Does not touch tests/test_server.py or
tests/test_policy_schema.py.
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
VALIDATOR_PATH = REPO_ROOT / "tools" / "validate_confirmation_token.py"
EXAMPLE_JSON = (
    REPO_ROOT / "schema" / "examples" / "confirmation_token-close-action.json"
)

_spec = importlib.util.spec_from_file_location(
    "validate_confirmation_token", VALIDATOR_PATH
)
assert _spec is not None and _spec.loader is not None
validate_token = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(validate_token)


def _stamp(days_from_now: float) -> str:
    moment = datetime.now(timezone.utc) + timedelta(days=days_from_now)
    return moment.isoformat(timespec="seconds")


def _base_token() -> dict:
    return {
        "token_id": "9b2c4d6e-8f1a-4b3c-9d5e-6f7a8b9c0d1e",
        "target_identity": {
            "hwnd": 65986,
            "pid": 12340,
            "process_started_at": 134021234567890000,
        },
        "action_class": "close",
        "issued_at": _stamp(-1 / 24),
        "expires_at": _stamp(1 / 24),
        "single_use": True,
    }


class ConfirmationTokenExampleTests(unittest.TestCase):
    def test_valid_example_passes(self):
        doc = validate_token.load_token(EXAMPLE_JSON)
        self.assertEqual(validate_token.validate_token(doc), [])

    def test_validator_cli_reports_ok_with_exit_zero(self):
        proc = subprocess.run(
            [sys.executable, str(VALIDATOR_PATH), str(EXAMPLE_JSON)],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        self.assertIn("OK", proc.stdout)


class ConfirmationTokenRuleTests(unittest.TestCase):
    def test_single_use_false_is_rejected(self):
        doc = _base_token()
        doc["single_use"] = False
        problems = validate_token.validate_token(doc)
        self.assertTrue(
            any("const:true" in p for p in problems),
            msg=f"expected const violation missing: {problems}",
        )

    def test_expires_at_not_after_issued_at_is_rejected(self):
        doc = _base_token()
        doc["expires_at"] = doc["issued_at"]
        problems = validate_token.validate_token(doc)
        self.assertTrue(
            any("negative/zero" in p for p in problems),
            msg=f"expected duration error missing: {problems}",
        )

    def test_expired_token_is_rejected(self):
        doc = _base_token()
        doc["issued_at"] = _stamp(-2)
        doc["expires_at"] = _stamp(-1)
        problems = validate_token.validate_token(doc)
        self.assertTrue(
            any("expired" in p for p in problems),
            msg=f"expected expiry error missing: {problems}",
        )

    def test_unknown_action_class_is_rejected(self):
        doc = _base_token()
        doc["action_class"] = "click"
        problems = validate_token.validate_token(doc)
        self.assertTrue(
            any("unknown action class" in p for p in problems),
            msg=f"expected action-class error missing: {problems}",
        )

    def test_drag_action_class_is_valid(self):
        doc = _base_token()
        doc["action_class"] = "drag"
        self.assertEqual(validate_token.validate_token(doc), [])

    def test_drag_example_passes(self):
        doc = validate_token.load_token(
            REPO_ROOT / "schema" / "examples" / "confirmation_token-drag-action.json"
        )
        self.assertEqual(validate_token.validate_token(doc), [])
        self.assertEqual(doc["action_class"], "drag")

    def test_offsetless_issued_at_is_rejected(self):
        doc = _base_token()
        doc["issued_at"] = "2027-01-01T00:00:00"
        problems = validate_token.validate_token(doc)
        self.assertTrue(
            any("offset" in p for p in problems),
            msg=f"expected offset error missing: {problems}",
        )

    def test_extra_root_field_is_rejected(self):
        doc = _base_token()
        doc["screenshot_bytes"] = "unknown field must not be swallowed"
        problems = validate_token.validate_token(doc)
        self.assertTrue(
            any("unknown field" in p for p in problems),
            msg=f"expected additionalProperties error missing: {problems}",
        )

    def test_malformed_token_id_is_rejected(self):
        doc = _base_token()
        doc["token_id"] = "close-123"
        problems = validate_token.validate_token(doc)
        self.assertTrue(
            any("UUID4" in p for p in problems),
            msg=f"expected UUID4 error missing: {problems}",
        )

    def test_missing_file_is_probe_error_not_invalid(self):
        with self.assertRaises(validate_token.TokenFileError) as ctx:
            validate_token.load_token(REPO_ROOT / "schema" / "yok.json")
        self.assertIn("not found", str(ctx.exception))

    def test_invalid_token_cli_exits_one_with_rule_message(self):
        doc = _base_token()
        doc["single_use"] = False
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
        self.assertIn("const:true", proc.stdout)


if __name__ == "__main__":
    unittest.main()
