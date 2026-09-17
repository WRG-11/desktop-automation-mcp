"""Phase 3 audit event schema tests (design draft).

Scope: tools/validate_audit_event.py + the example event under
schema/examples. Does not touch tests/test_server.py or
tests/test_policy_schema.py.
"""

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
VALIDATOR_PATH = REPO_ROOT / "tools" / "validate_audit_event.py"
EXAMPLE_JSON = REPO_ROOT / "schema" / "examples" / "audit_event-denied-text.json"
AUDIT_SCHEMA = REPO_ROOT / "schema" / "audit_event.schema.json"

_spec = importlib.util.spec_from_file_location("validate_audit_event", VALIDATOR_PATH)
assert _spec is not None and _spec.loader is not None
validate_event = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(validate_event)


def _base_event() -> dict:
    return {
        "timestamp": "2026-09-14T12:04:37+03:00",
        "correlation_id": "7a1e9f3c2b4d",
        "policy_id": "notepad-text-policy/v1",
        "action_type": "text",
        "outcome": "denied",
        "denial_reason": "target window is not foreground (passive focus gate)",
        "duration_ms": 12,
        "target_identity": {
            "hwnd": 65980,
            "pid": 9871,
            "process_started_at": 134021200000000000,
        },
    }


class AuditEventExampleTests(unittest.TestCase):
    def test_valid_example_passes(self):
        doc = validate_event.load_event(EXAMPLE_JSON)
        self.assertEqual(validate_event.validate_event(doc), [])

    def test_validator_cli_reports_ok_with_exit_zero(self):
        proc = subprocess.run(
            [sys.executable, str(VALIDATOR_PATH), str(EXAMPLE_JSON)],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        self.assertIn("OK", proc.stdout)


class AuditEventRuleTests(unittest.TestCase):
    def test_denied_without_reason_is_rejected(self):
        doc = _base_event()
        del doc["denial_reason"]
        problems = validate_event.validate_event(doc)
        self.assertTrue(
            any("denial_reason" in p and "required" in p for p in problems),
            msg=f"expected reason-required error missing: {problems}",
        )

    def test_denied_with_blank_reason_is_rejected(self):
        doc = _base_event()
        doc["denial_reason"] = "   "
        problems = validate_event.validate_event(doc)
        self.assertTrue(
            any("must not be empty" in p for p in problems),
            msg=f"expected blank-reason error missing: {problems}",
        )

    def test_error_without_reason_is_rejected(self):
        doc = _base_event()
        doc["outcome"] = "error"
        del doc["denial_reason"]
        problems = validate_event.validate_event(doc)
        self.assertTrue(
            any("denial_reason" in p and "required" in p for p in problems),
            msg=f"expected reason-required error missing: {problems}",
        )

    def test_allowed_with_reason_is_rejected(self):
        # DECISION: denial_reason cannot be given while outcome is allowed
        # (the schema's else-branch + rationale is in schema/README.md).
        # The conflict is the reason for rejection.
        doc = _base_event()
        doc["outcome"] = "allowed"
        problems = validate_event.validate_event(doc)
        self.assertTrue(
            any("must not be given" in p for p in problems),
            msg=f"expected conflict error missing: {problems}",
        )

    def test_allowed_without_reason_passes(self):
        doc = _base_event()
        doc["outcome"] = "allowed"
        del doc["denial_reason"]
        self.assertEqual(validate_event.validate_event(doc), [])

    def test_drag_action_type_matches_runtime_contract(self):
        doc = _base_event()
        doc["action_type"] = "drag"
        self.assertEqual(validate_event.validate_event(doc), [])

    def test_unknown_action_type_is_rejected(self):
        doc = _base_event()
        doc["action_type"] = "teleport"
        problems = validate_event.validate_event(doc)
        self.assertTrue(
            any("unknown action" in p for p in problems),
            msg=f"expected unknown-action error missing: {problems}",
        )

    def test_runtime_correlation_id_shape_is_enforced(self):
        doc = _base_event()
        doc["correlation_id"] = "7a1e9f3c-2b4d-4e6a-8c1d-3f5a7b9d0e2f"
        problems = validate_event.validate_event(doc)
        self.assertTrue(any("12-lowercase-hex" in p for p in problems))

    def test_redacted_screenshot_context_passes_but_names_do_not(self):
        doc = _base_event()
        doc["operation"] = "screenshot_window"
        doc["privacy_context"] = {
            "region_limited": True,
            "mask_count": 1,
            "pixel_area": 3600,
            "png_bytes": 512,
        }
        self.assertEqual(validate_event.validate_event(doc), [])
        doc["privacy_context"]["region_name"] = "sensitive-panel"
        problems = validate_event.validate_event(doc)
        self.assertTrue(any("sensitive" in p for p in problems), msg=str(problems))

    def test_extra_root_field_is_rejected(self):
        doc = _base_event()
        doc["screenshot_bytes"] = "unknown field must not be swallowed"
        problems = validate_event.validate_event(doc)
        self.assertTrue(
            any("unknown field" in p for p in problems),
            msg=f"expected additionalProperties error missing: {problems}",
        )

    def test_title_in_target_identity_is_rejected(self):
        doc = _base_event()
        doc["target_identity"] = dict(doc["target_identity"], title="Notepad")
        problems = validate_event.validate_event(doc)
        self.assertTrue(
            any("title" in p and "FORBIDDEN" in p for p in problems),
            msg=f"expected title-forbidden error missing: {problems}",
        )

    def test_schema_really_uses_if_then_else(self):
        # The denial_reason condition MUST actually be enforced in the
        # schema via if/then/else (not just a description NOTE).
        schema = json.loads(AUDIT_SCHEMA.read_text(encoding="utf-8"))
        branches = schema.get("allOf", [])
        self.assertTrue(
            any(
                isinstance(b, dict) and "if" in b and "then" in b and "else" in b
                for b in branches
            ),
            msg="no if/then/else branch found in the audit schema",
        )

    def test_missing_file_is_probe_error_not_invalid(self):
        with self.assertRaises(validate_event.AuditFileError) as ctx:
            validate_event.load_event(REPO_ROOT / "schema" / "yok.json")
        self.assertIn("not found", str(ctx.exception))

    def test_invalid_event_cli_exits_one_with_rule_message(self):
        doc = _base_event()
        doc["denial_reason"] = ""
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
        self.assertIn("must not be empty", proc.stdout)


if __name__ == "__main__":
    unittest.main()
