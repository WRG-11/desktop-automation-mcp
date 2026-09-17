"""Operational visibility tools tests (ROADMAP backlog item).

Scope: diagnosing a `policy_denied` rate rejection today requires
re-reading source — no MCP tool exposes rate-limit state or recent audit
events. `get_rate_limit_state()` and `get_audit_events(limit)` close that
gap as `observe`-class, memory-only, redacted reads.

Redaction contract pinned here: no per-key process paths (limiter keys
are exe paths), no `target_identity` triple, only
numbers/codes/timestamps plus the already-redacted `denial_reason`.
Neither tool audits itself (reading must not fill the log it reads).

File layout note: separate from `tests/test_server.py` so the red test
lands without touching existing suites.
"""

import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from desktop_automation_mcp import audit, errors, server


def _env_patch(**overrides):
    env = dict(os.environ)
    for var in (
        "DESKTOP_AUTOMATION_POLICY_FILE",
        "DESKTOP_AUTOMATION_ALLOWED_TITLES",
        "DESKTOP_AUTOMATION_ALLOWED_PROCESS_PATHS",
        "DESKTOP_AUTOMATION_ALLOWED_ACTIONS",
    ):
        env.pop(var, None)
    env.update(overrides)
    return patch.dict(os.environ, env, clear=True)


def _observe_env():
    return _env_patch(
        DESKTOP_AUTOMATION_ALLOWED_TITLES="*Ruffle*",
        DESKTOP_AUTOMATION_ALLOWED_PROCESS_PATHS=r"C:\Program Files\ruffle\bin\ruffle.exe",
        DESKTOP_AUTOMATION_ALLOWED_ACTIONS="observe",
    )


class RateLimitStateTests(unittest.TestCase):
    def setUp(self):
        server._reset_rate_limits_for_tests()
        self.addCleanup(server._reset_rate_limits_for_tests)

    def test_reports_configured_limits_and_empty_usage(self):
        with _observe_env():
            state = server.get_rate_limit_state()
        for section in (
            "actions_per_minute",
            "text_chars_per_minute",
            "screenshot_pixels_per_minute",
        ):
            self.assertIn("limit", state[section])
            self.assertEqual(state[section]["total_used"], 0)
            self.assertEqual(state[section]["tracked_keys"], 0)
        self.assertIn("limit_bytes", state["screenshot_memory"])
        self.assertEqual(state["screenshot_memory"]["in_use_bytes"], 0)

    def test_reflects_consumed_usage_from_the_real_limiter(self):
        server._action_rate_limiter.consume("some-key", 3, 1_000_000)
        with _observe_env():
            state = server.get_rate_limit_state()
        self.assertEqual(state["actions_per_minute"]["total_used"], 3)
        self.assertEqual(state["actions_per_minute"]["tracked_keys"], 1)
        self.assertEqual(state["text_chars_per_minute"]["total_used"], 0)

    def test_exposes_no_process_paths(self):
        server._action_rate_limiter.consume(
            r"C:\Users\secret-operator\app.exe", 1, 1_000_000
        )
        with _observe_env():
            state = server.get_rate_limit_state()
        self.assertNotIn("secret-operator", json.dumps(state))
        self.assertNotIn("app.exe", json.dumps(state))

    def test_requires_observe_permission(self):
        with _env_patch(
            DESKTOP_AUTOMATION_ALLOWED_TITLES="*Ruffle*",
            DESKTOP_AUTOMATION_ALLOWED_PROCESS_PATHS=r"C:\Program Files\ruffle\bin\ruffle.exe",
            DESKTOP_AUTOMATION_ALLOWED_ACTIONS="click",
        ):
            with self.assertRaises(errors.PolicyDeniedError):
                server.get_rate_limit_state()


class AuditEventsTests(unittest.TestCase):
    def setUp(self):
        audit.clear_events()
        self.addCleanup(audit.clear_events)

    def _record_two(self):
        audit.record_event(
            correlation_id="a" * 12,
            policy_id="env",
            action_type="click",
            outcome=audit.OUTCOME_ALLOWED,
            duration_ms=5,
            target_identity={"hwnd": 42, "pid": 1, "process_started_at": 7},
        )
        audit.record_event(
            correlation_id="b" * 12,
            policy_id="env",
            action_type="click",
            outcome=audit.OUTCOME_DENIED,
            duration_ms=3,
            denial_reason="rate limit exceeded: 120+1 > 120 (window=60 s)",
        )

    def test_returns_newest_first_without_target_identity(self):
        self._record_two()
        with _observe_env():
            result = server.get_audit_events(limit=10)
        self.assertEqual(result["total_retained"], 2)
        self.assertEqual(result["limit"], 10)
        # Newest first: the denial comes before the older allowed event.
        self.assertEqual(result["events"][0]["correlation_id"], "b" * 12)
        self.assertEqual(result["events"][1]["correlation_id"], "a" * 12)
        for event in result["events"]:
            self.assertNotIn("target_identity", event)

    def test_denial_reason_preserved_for_diagnosis(self):
        self._record_two()
        with _observe_env():
            result = server.get_audit_events()
        denied = result["events"][0]
        self.assertEqual(denied["outcome"], "denied")
        self.assertIn("rate limit exceeded", denied["denial_reason"])

    def test_limit_bounds_checked(self):
        with _observe_env():
            for bad in (0, -1, 101, True, "20", 3.5):
                with self.assertRaises(errors.InputRejectedError, msg=repr(bad)):
                    server.get_audit_events(limit=bad)

    def test_limit_caps_returned_events(self):
        self._record_two()
        with _observe_env():
            result = server.get_audit_events(limit=1)
        self.assertEqual(len(result["events"]), 1)
        self.assertEqual(result["total_retained"], 2)

    def test_requires_observe_permission(self):
        with _env_patch(
            DESKTOP_AUTOMATION_ALLOWED_TITLES="*Ruffle*",
            DESKTOP_AUTOMATION_ALLOWED_PROCESS_PATHS=r"C:\Program Files\ruffle\bin\ruffle.exe",
            DESKTOP_AUTOMATION_ALLOWED_ACTIONS="click",
        ):
            with self.assertRaises(errors.PolicyDeniedError):
                server.get_audit_events()

    def test_visibility_tools_do_not_audit_themselves(self):
        with _observe_env():
            server.get_rate_limit_state()
            server.get_audit_events()
        self.assertEqual(audit.read_events(), [])


if __name__ == "__main__":
    unittest.main()
