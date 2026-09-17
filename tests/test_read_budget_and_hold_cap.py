"""Read-budget + sequence-hold-cap hardening tests (adversarial round 2).

Two missing aggregate bounds, same family as the closed MAX_* caps:

- FIX 1: read/enumeration tools (`list_windows`, `get_window_state`,
  `preview_action`, `wait_for_*`) consume NO rate budget today — a tight
  polling loop burns unbounded EnumWindows+OpenProcess work. They now
  charge one unit of a dedicated read budget (`MAX_READS_PER_MINUTE`).
  Self-diagnostics (`health_check`, file-only profile resolve, and the
  rate/audit/bounds readers) are deliberately EXEMPT — diagnosing a rate
  denial must never itself be rate-denied (lockout avoidance).
- FIX 2: `send_key_sequence` caps steps (10) and per-step hold (5000 ms)
  but not their PRODUCT — 10 x 5000 ms is one ~50 s input-holding call.
  The total is now capped (`MAX_SEQUENCE_TOTAL_HOLD_MS`).

File layout note: separate from existing suites so the red tests land
without touching them.
"""

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from desktop_automation_mcp import errors, server, target

RUFFLE_EXE = r"C:\Program Files\ruffle\bin\ruffle.exe"


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
        DESKTOP_AUTOMATION_ALLOWED_PROCESS_PATHS=RUFFLE_EXE,
        DESKTOP_AUTOMATION_ALLOWED_ACTIONS="observe",
    )


class ReadBudgetTests(unittest.TestCase):
    def setUp(self):
        server._reset_rate_limits_for_tests()
        self.addCleanup(server._reset_rate_limits_for_tests)

    def test_reads_denied_after_budget_exhausted(self):
        with _observe_env():
            with (
                patch.object(server, "MAX_READS_PER_MINUTE", 2),
                patch.object(target, "_enum_windows", return_value=[]),
            ):
                server.list_windows()
                server.list_windows()
                with self.assertRaises(errors.PolicyDeniedError):
                    server.list_windows()

    def test_wait_counts_against_read_budget(self):
        with _observe_env():
            with (
                patch.object(
                    server,
                    "_resolve_window",
                    side_effect=ValueError(server._NOT_FOUND_MESSAGE),
                ),
            ):
                with self.assertRaises(errors.ActionTimeoutError):
                    server.wait_for_window(
                        "Ruffle", timeout_ms=100, poll_interval_ms=50
                    )
        self.assertEqual(server._read_rate_limiter.snapshot()["total_used"], 1)

    def test_diagnostics_exempt_from_read_budget(self):
        # Even with the budget fully spent, self-diagnosis must work —
        # otherwise a rate denial could lock the operator out of the very
        # tools that explain it.
        with _observe_env():
            with (
                patch.object(server, "MAX_READS_PER_MINUTE", 1),
                patch.object(target, "_enum_windows", return_value=[]),
                patch.object(
                    server, "virtual_screen_bounds", return_value=(0, 0, 100, 100)
                ),
            ):
                server.list_windows()
                server.health_check()
                server.get_rate_limit_state()
                server.get_audit_events()
                server.get_virtual_screen_bounds()


class SequenceHoldCapTests(unittest.TestCase):
    def test_total_hold_capped(self):
        tgt = target.TargetSnapshot(
            hwnd=42,
            title="Ruffle player",
            pid=1,
            rect=(0, 0, 100, 100),
            process_started_at=1,
            process_path=RUFFLE_EXE,
            window_class="",
        )
        steps = [{"key": "a"}] * 10
        with _env_patch(
            DESKTOP_AUTOMATION_ALLOWED_TITLES="*Ruffle*",
            DESKTOP_AUTOMATION_ALLOWED_PROCESS_PATHS=RUFFLE_EXE,
            DESKTOP_AUTOMATION_ALLOWED_ACTIONS="key",
        ):
            with (
                patch.object(server, "_prepare_action_target", return_value=tgt),
                patch.object(server, "_verify_action_target"),
                patch.object(server.time, "sleep"),
                patch.object(server, "_send_vk") as send_vk,
            ):
                with self.assertRaises(errors.InputRejectedError):
                    server.send_key_sequence(keys=steps, hold_ms=5000, hwnd=42)
                send_vk.assert_not_called()

    def test_total_hold_boundary_exact(self):
        tgt = target.TargetSnapshot(
            hwnd=42,
            title="Ruffle player",
            pid=1,
            rect=(0, 0, 100, 100),
            process_started_at=1,
            process_path=RUFFLE_EXE,
            window_class="",
        )
        with _env_patch(
            DESKTOP_AUTOMATION_ALLOWED_TITLES="*Ruffle*",
            DESKTOP_AUTOMATION_ALLOWED_PROCESS_PATHS=RUFFLE_EXE,
            DESKTOP_AUTOMATION_ALLOWED_ACTIONS="key",
        ):
            with (
                patch.object(server, "_prepare_action_target", return_value=tgt),
                patch.object(server, "_verify_action_target"),
                patch.object(server.time, "sleep"),
                patch.object(server, "_send_vk"),
            ):
                # Exactly at the cap: admitted.
                result = server.send_key_sequence(
                    keys=[{"key": "a"}] * 10, hold_ms=1000, hwnd=42
                )
                self.assertIn("10 step", result)
                # One millisecond over: rejected before anything is sent.
                with self.assertRaises(errors.InputRejectedError):
                    server.send_key_sequence(
                        keys=[{"key": "a"}] * 10, hold_ms=1001, hwnd=42
                    )


if __name__ == "__main__":
    unittest.main()
