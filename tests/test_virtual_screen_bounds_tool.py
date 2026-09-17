"""Virtual-screen bounds debug tool tests (ROADMAP polish item).

Scope: the virtual desktop bounds (the negative-origin multi-monitor
geometry behind every screenshot plan) are only documented today, not
queryable. `get_virtual_screen_bounds()` exposes the live Win32 reading
as a debug-only `observe` tool — a thin wrapper, no invented fields.
The second closed sub-item (scroll docstring clarity) is pinned by the
docstring test at the bottom; it needs no code change.

File layout note: separate from `tests/test_server.py` so the red test
lands without touching existing suites.
"""

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


class VirtualScreenBoundsToolTests(unittest.TestCase):
    def test_returns_live_win32_bounds_with_negative_origin(self):
        # The real DPI-matrix case: second monitor on the left, virtual
        # origin at x=-1920. The tool must surface it, not hide it.
        with _observe_env():
            with patch.object(
                server,
                "virtual_screen_bounds",
                return_value=(-1920, 0, 640, 1600),
            ):
                result = server.get_virtual_screen_bounds()
        self.assertEqual(result["bounds"], [-1920, 0, 640, 1600])
        self.assertEqual(result["origin"], [-1920, 0])
        self.assertEqual(result["size"], [2560, 1600])

    def test_requires_observe_permission(self):
        with _env_patch(
            DESKTOP_AUTOMATION_ALLOWED_TITLES="*Ruffle*",
            DESKTOP_AUTOMATION_ALLOWED_PROCESS_PATHS=r"C:\Program Files\ruffle\bin\ruffle.exe",
            DESKTOP_AUTOMATION_ALLOWED_ACTIONS="click",
        ):
            with self.assertRaises(errors.PolicyDeniedError):
                server.get_virtual_screen_bounds()

    def test_win32_probe_failure_propagates(self):
        with _observe_env():
            with patch.object(
                server,
                "virtual_screen_bounds",
                side_effect=errors.PlatformError("no metrics"),
            ):
                with self.assertRaises(errors.PlatformError):
                    server.get_virtual_screen_bounds()

    def test_does_not_audit_itself(self):
        audit.clear_events()
        self.addCleanup(audit.clear_events)
        with _observe_env():
            with patch.object(
                server, "virtual_screen_bounds", return_value=(0, 0, 100, 100)
            ):
                server.get_virtual_screen_bounds()
        self.assertEqual(audit.read_events(), [])


class ScrollDocstringTests(unittest.TestCase):
    def test_docstring_documents_wheel_convention(self):
        # Second closed sub-item: the WHEEL_DELTA=120 rule, the sign
        # convention, and the deliberate absence of a horizontal axis must
        # be spelled out where the caller reads them — no code change.
        doc = server.scroll_window.__doc__ or ""
        self.assertIn("120", doc)
        self.assertIn("horizontal", doc.lower())


if __name__ == "__main__":
    unittest.main()
