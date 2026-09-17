"""drag_window waypoint tests (ROADMAP backlog item).

Scope: `drag_window` is a single straight start->end line today; routes
around an obstacle cannot be modeled. An optional `path` (intermediate
window-relative waypoints between start and end) closes that gap with the
button held throughout: every waypoint passes the same independent
bounds check as start/end, the target is re-verified before each leg,
and `finally` releases the button even on a mid-path failure.

Backward compatibility pinned here: without `path` the event order,
verify count, and result message are byte-identical to the legacy drag.

File layout note: separate from `tests/test_server.py` so the red test
lands without touching existing suites.
"""

import sys
import unittest
from ctypes import wintypes
from pathlib import Path
from unittest.mock import call, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from desktop_automation_mcp import errors, server, target, visibility
from desktop_automation_mcp import platform_win32


def _target():
    return target.TargetSnapshot(
        hwnd=42,
        title="Ruffle player",
        pid=1,
        rect=(0, 0, 100, 100),
        process_started_at=1,
        process_path=r"C:\Program Files\ruffle\bin\ruffle.exe",
        window_class="RuffleWindowClass",
    )


def _patches(tgt):
    return (
        patch.object(server, "_prepare_action_target", return_value=tgt),
        patch.object(server, "consume_confirmation"),
        patch.object(server, "_verify_action_target"),
        patch.object(
            visibility, "_window_rect", return_value=wintypes.RECT(0, 0, 100, 100)
        ),
        patch.object(platform_win32.user32, "SetCursorPos", return_value=True),
        patch.object(platform_win32.user32, "mouse_event"),
        patch.object(server.time, "sleep"),
    )


def _drag(**overrides):
    kwargs = {
        "hwnd": 42,
        "start_x": 10,
        "start_y": 10,
        "end_x": 40,
        "end_y": 40,
        "confirmation_token": "tok-1",
    }
    kwargs.update(overrides)
    return server.drag_window(**kwargs)


class DragWaypointTests(unittest.TestCase):
    def test_route_moves_through_each_point_with_button_held(self):
        tgt = _target()
        prepare, consume, verify, rect, set_pos, mouse_event, sleep = _patches(tgt)
        with prepare, consume, verify, rect, sleep:
            with set_pos as set_pos_mock, mouse_event as mouse_mock:
                result = _drag(path=[[20, 20], [30, 10]])
        set_pos_mock.assert_has_calls(
            [call(10, 10), call(20, 20), call(30, 10), call(40, 40)]
        )
        move = call(server.MOUSEEVENTF_MOVE, 0, 0, 0, 0)
        down = call(server.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        up = call(server.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
        # One DOWN before the first waypoint leg, one UP at the very end —
        # the button stays held through every intermediate point.
        self.assertEqual(mouse_mock.call_args_list, [move, down, move, move, move, up])
        self.assertIn("2 waypoint", result)
        self.assertIn("correlation_id=", result)

    def test_no_path_keeps_legacy_message_and_event_order(self):
        tgt = _target()
        prepare, consume, verify, rect, set_pos, mouse_event, sleep = _patches(tgt)
        with prepare, consume, verify, rect, set_pos, sleep:
            with mouse_event as mouse_mock:
                result = _drag()
        move = call(server.MOUSEEVENTF_MOVE, 0, 0, 0, 0)
        down = call(server.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        up = call(server.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
        self.assertEqual(mouse_mock.call_args_list, [move, down, move, up])
        self.assertNotIn("waypoint", result)
        self.assertTrue(
            result.startswith(
                "Dragged: window-relative (10, 10) -> (40, 40), hwnd=42, "
                "correlation_id="
            )
        )

    def test_each_waypoint_bounds_checked_before_any_mouse_effect(self):
        tgt = _target()
        prepare, consume, verify, rect, set_pos, mouse_event, sleep = _patches(tgt)
        with prepare, consume, verify, rect, sleep:
            with set_pos as set_pos_mock, mouse_event as mouse_mock:
                with self.assertRaises(errors.InputRejectedError):
                    _drag(path=[[20, 20], [200, 200]])
        # Resolution happens fully upfront: the out-of-window waypoint
        # fails before the cursor moves or the button goes down.
        set_pos_mock.assert_not_called()
        mouse_mock.assert_not_called()

    def test_mid_path_move_failure_still_releases_button(self):
        tgt = _target()
        prepare, consume, verify, rect, set_pos, mouse_event, sleep = _patches(tgt)
        with prepare, consume, verify, rect, sleep:
            with (
                patch.object(
                    platform_win32.user32,
                    "SetCursorPos",
                    side_effect=[True, True, False],
                ),
                mouse_event as mouse_mock,
            ):
                with self.assertRaises(errors.PlatformError):
                    _drag(path=[[20, 20], [30, 10]])
        downs = [
            c
            for c in mouse_mock.call_args_list
            if c == call(server.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        ]
        ups = [
            c
            for c in mouse_mock.call_args_list
            if c == call(server.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
        ]
        self.assertEqual(len(downs), 1)
        self.assertEqual(len(ups), 1)

    def test_overlong_path_rejected_without_touching_input(self):
        tgt = _target()
        prepare, consume, verify, rect, set_pos, mouse_event, sleep = _patches(tgt)
        too_many = [[50, 50]] * (server.MAX_DRAG_WAYPOINTS + 1)
        with prepare, consume, verify, rect, sleep:
            with set_pos as set_pos_mock, mouse_event as mouse_mock:
                with self.assertRaises(errors.InputRejectedError):
                    _drag(path=too_many)
        set_pos_mock.assert_not_called()
        mouse_mock.assert_not_called()

    def test_malformed_path_rejected_without_touching_input(self):
        tgt = _target()
        prepare, consume, verify, rect, set_pos, mouse_event, sleep = _patches(tgt)
        bad_paths = ("x", [[1]], [[1, 2, 3]], [["a", "b"]], [{"x": 1}], [[True, False]])
        with prepare, consume, verify, rect, sleep:
            with set_pos as set_pos_mock, mouse_event as mouse_mock:
                for bad in bad_paths:
                    with self.assertRaises(errors.InputRejectedError, msg=repr(bad)):
                        _drag(path=bad)
        set_pos_mock.assert_not_called()
        mouse_mock.assert_not_called()

    def test_drag_protection_unchanged_when_path_given(self):
        tgt = _target()
        prepare, consume, verify, rect, set_pos, mouse_event, sleep = _patches(tgt)
        with prepare, consume, verify, rect, sleep:
            with set_pos as set_pos_mock, mouse_event as mouse_mock:
                with self.assertRaises(errors.InputRejectedError):
                    server.drag_window(
                        hwnd=tgt.hwnd,
                        start_x=10,
                        start_y=10,
                        end_x=40,
                        end_y=40,
                        path=[[20, 20]],
                    )
        set_pos_mock.assert_not_called()
        mouse_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
