import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from desktop_automation_mcp.audit import AuditLog


def _record(log: AuditLog, **overrides):
    values = {
        "correlation_id": "7a1e9f3c2b4d",
        "policy_id": "env",
        "action_type": "observe",
        "outcome": "allowed",
        "duration_ms": 1,
    }
    values.update(overrides)
    return log.record(**values)


class AuditRuntimeContractTests(unittest.TestCase):
    def test_drag_and_redacted_screenshot_context_are_supported(self):
        event = _record(
            AuditLog(),
            action_type="drag",
            operation="screenshot_window",
            privacy_context={
                "region_limited": True,
                "mask_count": 1,
                "pixel_area": 3600,
                "png_bytes": 512,
            },
        )
        self.assertEqual(event["action_type"], "drag")
        self.assertEqual(event["privacy_context"]["mask_count"], 1)

    def test_runtime_rejects_schema_drift_and_sensitive_context(self):
        for overrides in (
            {"correlation_id": "not-a-runtime-id"},
            {"action_type": "teleport"},
            {"operation": "Bad Operation"},
            {"privacy_context": {"region_name": "secret-panel"}},
        ):
            with self.subTest(overrides=overrides):
                with self.assertRaises(ValueError):
                    _record(AuditLog(), **overrides)


if __name__ == "__main__":
    unittest.main()
