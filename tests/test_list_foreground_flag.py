"""`foreground` flag on `list_windows` entries (architecture audit round 1).

Scope: answering "which allowed window is foreground right now" costs
N `get_window_state` calls today (N+1 round trips for N windows) — the
only per-window foreground probe in the tool set. One
`GetForegroundWindow` read per LIST call (not per window) removes the
N+1 with no new permission: foreground state of already-listed allowed
windows is learnable through the existing tools anyway, so nothing new
is disclosed. Additive key only; existing keys and order unchanged.

File layout note: separate file so the red tests land without touching
existing suites.
"""

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from desktop_automation_mcp import platform_win32, server, target


def _snap(hwnd=42, title="Ruffle player"):
    return target.TargetSnapshot(
        hwnd=hwnd,
        title=title,
        pid=1,
        rect=(0, 0, 100, 100),
        process_started_at=1,
        process_path=r"C:\Program Files\ruffle\bin\ruffle.exe",
        window_class="RuffleWindowClass",
    )


class ListForegroundFlagTests(unittest.TestCase):
    def _list(self, windows, foreground):
        with patch.dict(os.environ, {"DESKTOP_AUTOMATION_ALLOWED_ACTIONS": "observe"}):
            with (
                patch.object(server, "_enum_windows", return_value=windows),
                patch.object(
                    platform_win32.user32,
                    "GetForegroundWindow",
                    return_value=foreground,
                ) as fg,
                patch.object(server.time, "sleep"),
            ):
                entries = server.list_windows()
        return entries, fg

    def test_entries_carry_foreground_true_only_for_the_front_window(self):
        entries, fg = self._list([_snap(42), _snap(99, "Ruffle two")], 99)
        self.assertEqual(
            [entry["hwnd"] for entry in entries],
            [42, 99],
        )
        self.assertIs(entries[0]["foreground"], False)
        self.assertIs(entries[1]["foreground"], True)
        # One probe per LIST call, not one per window.
        self.assertEqual(fg.call_count, 1)

    def test_existing_keys_and_order_unchanged(self):
        entries, _ = self._list([_snap(42)], 42)
        self.assertEqual(
            entries,
            [
                {
                    "hwnd": 42,
                    "title": "Ruffle player",
                    "pid": 1,
                    "rect": [0, 0, 100, 100],
                    "process_path": r"C:\Program Files\ruffle\bin\ruffle.exe",
                    "window_class": "RuffleWindowClass",
                    "foreground": True,
                }
            ],
        )


if __name__ == "__main__":
    unittest.main()
