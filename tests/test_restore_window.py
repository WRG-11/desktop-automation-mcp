"""restore_window tests (ROADMAP backlog item).

Scope: a minimized window cannot be recovered in passive mode today —
`ShowWindow(SW_RESTORE)` lives only inside `_focus_and_verify`'s activate
branch, and iconic windows are invisible to title/hw­nd resolution
(`_enum_windows` skips them), so the operator must un-minimize by hand.
`restore_window(hwnd)` closes that gap as an `observe`-class, hwnd-only
repair: it clears iconic state, never touches foreground, and every input
gate (foreground/occlusion/identity) keeps enforcing afterwards.

Fail-closed contract pinned here: out-of-policy hwnds are still denied
without any Win32 effect call, and the tool never calls the focus path
(`SetForegroundWindow`/`_focus_and_verify`) — a restored-but-background
window is still rejected by the input tools' own passive gates.

File layout note: separate from `tests/test_server.py` so the red test
lands without touching existing suites.
"""

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import call, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from desktop_automation_mcp import audit, errors, server
from desktop_automation_mcp.platform_win32 import SW_RESTORE

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


def _diagnostic(*, iconic, pid=1):
    return {
        "hwnd": 42,
        "title": "Ruffle player",
        "pid": pid,
        "process_path": RUFFLE_EXE,
        "window_class": "RuffleWindowClass",
        "rect": [0, 0, 100, 100],
        "visible": True,
        "iconic": iconic,
    }


class RestoreWindowTests(unittest.TestCase):
    def setUp(self):
        audit.clear_events()
        self.addCleanup(audit.clear_events)
        server._reset_rate_limits_for_tests()
        self.addCleanup(server._reset_rate_limits_for_tests)

    def test_minimized_window_restored_and_reverified(self):
        with _observe_env():
            with (
                patch.object(
                    server,
                    "_window_diagnostic_state",
                    side_effect=[
                        _diagnostic(iconic=True),
                        _diagnostic(iconic=False),
                    ],
                ),
                patch.object(
                    server,
                    "_focus_and_verify",
                    side_effect=AssertionError("focus path must not run"),
                ),
                patch("desktop_automation_mcp.server._user32") as user32,
            ):
                result = server.restore_window(hwnd=42)
        user32.return_value.ShowWindow.assert_called_once_with(42, SW_RESTORE)
        # Nothing else on the shared window state: no foreground grab, no
        # focus verification — ShowWindow is the ONLY Win32 effect call.
        self.assertEqual(
            user32.return_value.mock_calls, [call.ShowWindow(42, SW_RESTORE)]
        )
        self.assertIn("correlation_id=", result)
        events = audit.read_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["outcome"], audit.OUTCOME_ALLOWED)
        self.assertEqual(events[0]["operation"], "restore_window")

    def test_non_minimized_window_is_a_noop_without_win32_effect(self):
        with _observe_env():
            with (
                patch.object(
                    server,
                    "_window_diagnostic_state",
                    return_value=_diagnostic(iconic=False),
                ),
                patch("desktop_automation_mcp.server._user32") as user32,
            ):
                result = server.restore_window(hwnd=42)
        user32.return_value.ShowWindow.assert_not_called()
        self.assertIn("nothing to do", result)

    def test_out_of_policy_hwnd_denied_without_win32_effect(self):
        with _observe_env():
            with (
                patch.object(
                    server,
                    "_window_diagnostic_state",
                    side_effect=errors.PolicyDeniedError(
                        "hwnd=99 is not within the allowed policy scope."
                    ),
                ),
                patch("desktop_automation_mcp.server._user32") as user32,
            ):
                with self.assertRaises(errors.PolicyDeniedError):
                    server.restore_window(hwnd=99)
        user32.return_value.ShowWindow.assert_not_called()
        events = audit.read_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["outcome"], audit.OUTCOME_DENIED)

    def test_gone_hwnd_denied_as_error_not_success(self):
        with _observe_env():
            with patch.object(
                server,
                "_window_diagnostic_state",
                side_effect=ValueError("hwnd=99 no longer exists."),
            ):
                with self.assertRaises(ValueError):
                    server.restore_window(hwnd=99)
        events = audit.read_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["outcome"], audit.OUTCOME_DENIED)

    def test_recycled_hwnd_rejected_when_pid_changes(self):
        with _observe_env():
            with (
                patch.object(
                    server,
                    "_window_diagnostic_state",
                    side_effect=[
                        _diagnostic(iconic=True, pid=1),
                        _diagnostic(iconic=False, pid=2),
                    ],
                ),
                patch("desktop_automation_mcp.server._user32"),
            ):
                with self.assertRaises(errors.TargetStaleError):
                    server.restore_window(hwnd=42)
        events = audit.read_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["outcome"], audit.OUTCOME_DENIED)

    def test_still_iconic_after_restore_is_platform_error(self):
        with _observe_env():
            with (
                patch.object(
                    server,
                    "_window_diagnostic_state",
                    side_effect=[
                        _diagnostic(iconic=True),
                        _diagnostic(iconic=True),
                    ],
                ),
                patch("desktop_automation_mcp.server._user32"),
            ):
                with self.assertRaises(errors.PlatformError):
                    server.restore_window(hwnd=42)

    def test_requires_observe_permission_before_any_probe(self):
        with _env_patch(
            DESKTOP_AUTOMATION_ALLOWED_TITLES="*Ruffle*",
            DESKTOP_AUTOMATION_ALLOWED_PROCESS_PATHS=RUFFLE_EXE,
            DESKTOP_AUTOMATION_ALLOWED_ACTIONS="click",
        ):
            with patch.object(server, "_window_diagnostic_state") as diagnostic:
                with self.assertRaises(errors.PolicyDeniedError):
                    server.restore_window(hwnd=42)
        diagnostic.assert_not_called()


if __name__ == "__main__":
    unittest.main()
