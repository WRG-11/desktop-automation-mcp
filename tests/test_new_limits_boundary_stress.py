"""Boundary/stress tests for the new limit constants (no production change).

Scope: `MAX_KEY_SEQUENCE_LENGTH` (=10) and `MAX_DRAG_WAYPOINTS` (=16) must
be fail-closed AT the boundary. Scenario map:

- (1) exactly-MAX succeeds;
- (2) MAX+1 is `InputRejectedError` with ZERO Win32 emissions;
- (3) empty input: `keys=[]` is rejected (existing non-empty check), while
  `path=[]` is NOT an error by design (`_checked_drag_path`: "`None`
  (and `[]`) means the legacy straight drag") — the asymmetry is pinned,
  not "fixed";
- (4) an invalid LAST element fails with zero prior emissions (upfront
  resolve/validation is real: shape checks run before `_execute_guarded_action`,
  name/bounds resolution runs before the first emission inside `effect`).

Contract (both burn findings fixed): rejections raised BEFORE `effect`
(bad shapes, overlong lists, unknown key names) burn neither the token
nor any Win32 emission — `send_key_sequence` resolves all names pre-target
and `drag_window` checks path shape/length pre-consume. Only failures
AFTER consume (stale target, occluded window, Win32 refusal mid-emission)
can still strand a token, which is correct: input may already have gone
out. See the ROADMAP entries for both closed findings.

File layout note: separate from existing suites so these tests land
without touching them.
"""

import os
import sys
import tempfile
import unittest
from ctypes import wintypes
from pathlib import Path
from unittest.mock import call, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from desktop_automation_mcp import errors, server, target, visibility
from desktop_automation_mcp import platform_win32

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


def _key_env(*actions):
    return _env_patch(
        DESKTOP_AUTOMATION_ALLOWED_TITLES="*Ruffle*",
        DESKTOP_AUTOMATION_ALLOWED_PROCESS_PATHS=RUFFLE_EXE,
        DESKTOP_AUTOMATION_ALLOWED_ACTIONS=",".join(actions),
    )


def _target():
    return target.TargetSnapshot(
        hwnd=42,
        title="Ruffle player",
        pid=1,
        rect=(0, 0, 100, 100),
        process_started_at=1,
        process_path=RUFFLE_EXE,
        window_class="RuffleWindowClass",
    )


def _key_patches(tgt):
    return (
        patch.object(server, "_prepare_action_target", return_value=tgt),
        patch.object(server, "_verify_action_target"),
        patch.object(server.time, "sleep"),
    )


def _drag_patches(tgt, consume_mock):
    return (
        patch.object(server, "_prepare_action_target", return_value=tgt),
        consume_mock,
        patch.object(server, "_verify_action_target"),
        patch.object(
            visibility, "_window_rect", return_value=wintypes.RECT(0, 0, 100, 100)
        ),
        patch.object(platform_win32.user32, "SetCursorPos", return_value=True),
        patch.object(platform_win32.user32, "mouse_event"),
        patch.object(server.time, "sleep"),
    )


def _key_protected_policy_file(tmpdir):
    quoted = RUFFLE_EXE.replace("\\", "\\\\")
    path = Path(tmpdir) / "key-protected.yaml"
    path.write_text(
        "application:\n"
        f'  executable_path: "{quoted}"\n'
        "  title_patterns:\n"
        "    - *Ruffle*\n"
        "  allowed_actions:\n"
        "    - observe\n"
        "    - key\n"
        "  protected_actions:\n"
        "    - key\n",
        encoding="utf-8",
    )
    return str(path)


class KeySequenceBoundaryTests(unittest.TestCase):
    def test_exactly_max_steps_succeeds(self):
        """(1) MAX_KEY_SEQUENCE_LENGTH valid steps are all sent."""
        tgt = _target()
        calls: list[tuple[int, bool]] = []

        def fake_send_vk(code, *, key_up=False):
            calls.append((code, key_up))

        steps = [{"key": "a"}] * server.MAX_KEY_SEQUENCE_LENGTH
        prepare, verify, sleep = _key_patches(tgt)
        with _key_env("key"):
            with (
                prepare,
                verify,
                sleep,
                patch.object(server, "_send_vk", side_effect=fake_send_vk),
            ):
                result = server.send_key_sequence(keys=steps, hold_ms=0, hwnd=tgt.hwnd)
        # No modifiers: one down + one up per step, nothing left held.
        self.assertEqual(len(calls), 2 * server.MAX_KEY_SEQUENCE_LENGTH)
        downs = [c for c, up in calls if not up]
        ups = [c for c, up in calls if up]
        self.assertEqual(sorted(downs), sorted(ups))
        self.assertIn(f"{server.MAX_KEY_SEQUENCE_LENGTH} step", result)

    def test_max_plus_one_rejected_before_target_with_zero_emissions(self):
        """(2) MAX+1 steps are rejected pre-target: no input, no resolution."""
        tgt = _target()
        too_many = [{"key": "a"}] * (server.MAX_KEY_SEQUENCE_LENGTH + 1)
        prepare, verify, sleep = _key_patches(tgt)
        with _key_env("key"):
            with (
                prepare as prep_mock,
                verify,
                sleep,
                patch.object(server, "_send_vk") as send_vk,
            ):
                with self.assertRaises(errors.InputRejectedError):
                    server.send_key_sequence(keys=too_many, hwnd=tgt.hwnd)
                send_vk.assert_not_called()
            # The length check runs before `_execute_guarded_action`, so the
            # mocked target resolution never even runs.
            prep_mock.assert_not_called()

    def test_empty_keys_rejected(self):
        """(3) `keys=[]` hits the existing non-empty check."""
        tgt = _target()
        prepare, verify, sleep = _key_patches(tgt)
        with _key_env("key"):
            with prepare, verify, sleep, patch.object(server, "_send_vk") as send_vk:
                with self.assertRaisesRegex(errors.InputRejectedError, "non-empty"):
                    server.send_key_sequence(keys=[], hwnd=tgt.hwnd)
                send_vk.assert_not_called()

    def test_unknown_key_last_emits_nothing(self):
        """(4) Unknown name in the LAST step: all-names-upfront resolution
        fails before the first `_send_vk` — earlier valid steps never run."""
        tgt = _target()
        steps = [{"key": "a"}] * (server.MAX_KEY_SEQUENCE_LENGTH - 1)
        steps.append({"key": "not-a-real-key"})
        prepare, verify, sleep = _key_patches(tgt)
        with _key_env("key"):
            with prepare, verify, sleep, patch.object(server, "_send_vk") as send_vk:
                with self.assertRaisesRegex(errors.InputRejectedError, "Unknown key"):
                    server.send_key_sequence(keys=steps, hold_ms=0, hwnd=tgt.hwnd)
                send_vk.assert_not_called()

    def test_unknown_key_last_preserves_token_for_retry(self):
        """FIXED contract (was: token-burn): in file mode with `key`
        protected, an unknown last-step name must reject BEFORE the token
        is consumed — so the SAME token still buys one valid retry."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        tgt = _target()
        prepare, verify, sleep = _key_patches(tgt)
        with _env_patch(
            DESKTOP_AUTOMATION_POLICY_FILE=_key_protected_policy_file(tmp.name)
        ):
            with prepare, verify, sleep, patch.object(server, "_send_vk") as send_vk:
                token = server.request_confirmation(action_class="key", hwnd=tgt.hwnd)
                token_id = token["token_id"]
                with self.assertRaisesRegex(errors.InputRejectedError, "Unknown key"):
                    server.send_key_sequence(
                        keys=[{"key": "a"}, {"key": "not-a-real-key"}],
                        hold_ms=0,
                        hwnd=tgt.hwnd,
                        confirmation_token=token_id,
                    )
                send_vk.assert_not_called()
                # Same token still live: one valid sequence consumes it.
                result = server.send_key_sequence(
                    keys=[{"key": "a"}],
                    hold_ms=0,
                    hwnd=tgt.hwnd,
                    confirmation_token=token_id,
                )
                self.assertIn("1 step", result)
        with self.assertRaises(errors.PolicyDeniedError) as ctx:
            server.consume_confirmation(token_id, server._target_identity(tgt), "key")
        self.assertIn("already consumed", str(ctx.exception))


class DragPathBoundaryTests(unittest.TestCase):
    def test_exactly_max_waypoints_succeeds(self):
        """(1) MAX_DRAG_WAYPOINTS valid waypoints are all traversed."""
        tgt = _target()
        points = [[i + 1, i + 2] for i in range(server.MAX_DRAG_WAYPOINTS)]
        patches = _drag_patches(tgt, patch.object(server, "consume_confirmation"))
        prepare, consume, verify, rect, set_pos, mouse_event, sleep = patches
        with prepare, consume, verify, rect, sleep:
            with set_pos as set_pos_mock, mouse_event:
                result = server.drag_window(
                    hwnd=42,
                    start_x=10,
                    start_y=10,
                    end_x=40,
                    end_y=40,
                    confirmation_token="tok-1",
                    path=points,
                )
        # Start + every waypoint + end, in order.
        self.assertEqual(set_pos_mock.call_count, 2 + server.MAX_DRAG_WAYPOINTS)
        self.assertIn(f"{server.MAX_DRAG_WAYPOINTS} waypoint", result)

    def test_max_plus_one_rejected_with_zero_mouse_effects(self):
        """(2) MAX+1 waypoints are rejected; the cursor never moves."""
        tgt = _target()
        too_many = [[i + 1, i + 2] for i in range(server.MAX_DRAG_WAYPOINTS + 1)]
        patches = _drag_patches(tgt, patch.object(server, "consume_confirmation"))
        prepare, consume, verify, rect, set_pos, mouse_event, sleep = patches
        with prepare, consume, verify, rect, sleep:
            with set_pos as set_pos_mock, mouse_event as mouse_mock:
                with self.assertRaisesRegex(errors.InputRejectedError, "at most"):
                    server.drag_window(
                        hwnd=42,
                        start_x=10,
                        start_y=10,
                        end_x=40,
                        end_y=40,
                        confirmation_token="tok-1",
                        path=too_many,
                    )
                set_pos_mock.assert_not_called()
                mouse_mock.assert_not_called()

    def test_empty_path_is_legacy_drag_not_an_error(self):
        """(3) `path=[]` is NOT rejected: by design it means the legacy
        straight drag (same as `path=None`). Pinned against a future
        "non-empty" check that would break backward compatibility."""
        tgt = _target()
        patches = _drag_patches(tgt, patch.object(server, "consume_confirmation"))
        prepare, consume, verify, rect, set_pos, mouse_event, sleep = patches
        with prepare, consume, verify, rect, sleep:
            with set_pos as set_pos_mock, mouse_event as mouse_mock:
                result = server.drag_window(
                    hwnd=42,
                    start_x=10,
                    start_y=10,
                    end_x=40,
                    end_y=40,
                    confirmation_token="tok-1",
                    path=[],
                )
        set_pos_mock.assert_has_calls([call(10, 10), call(40, 40)])
        self.assertEqual(set_pos_mock.call_count, 2)
        move = mouse_mock.call_args_list[0]
        down = mouse_mock.call_args_list[1]
        up = mouse_mock.call_args_list[-1]
        self.assertEqual(
            [c.args[0] for c in (move, down, up)],
            [
                server.MOUSEEVENTF_MOVE,
                server.MOUSEEVENTF_LEFTDOWN,
                server.MOUSEEVENTF_LEFTUP,
            ],
        )
        self.assertNotIn("waypoint", result)

    def test_malformed_last_waypoint_emits_nothing(self):
        """(4) A malformed LAST waypoint (non-integer pair) fails shape
        validation with zero mouse effects — earlier valid points never
        move the cursor."""
        tgt = _target()
        bad_last = [[20, 20], [30, 30], ["x", 10]]
        patches = _drag_patches(tgt, patch.object(server, "consume_confirmation"))
        prepare, consume, verify, rect, set_pos, mouse_event, sleep = patches
        with prepare, consume, verify, rect, sleep:
            with set_pos as set_pos_mock, mouse_event as mouse_mock:
                with self.assertRaisesRegex(
                    errors.InputRejectedError, r"path\[2\] must be an"
                ):
                    server.drag_window(
                        hwnd=42,
                        start_x=10,
                        start_y=10,
                        end_x=40,
                        end_y=40,
                        confirmation_token="tok-1",
                        path=bad_last,
                    )
                set_pos_mock.assert_not_called()
                mouse_mock.assert_not_called()

    def test_overlong_path_rejected_before_token_consume(self):
        """FIXED contract (was: token-burn characterization): like
        `send_key_sequence` (length check pre-target), the drag waypoint
        limit + shape checks now run BEFORE `consume_confirmation`, so a
        MAX+1 `path` rejects with zero mouse effects AND the single-use
        token survives — proven by spending the SAME token on a valid
        drag right afterwards."""
        tgt = _target()
        too_many = [[i + 1, i + 2] for i in range(server.MAX_DRAG_WAYPOINTS + 1)]
        # REAL confirmation store (no consume mock): the token must live
        # through the rejection below.
        token = server.issue_confirmation(server._target_identity(tgt), "drag")
        token_id = token["token_id"]
        patches = (
            patch.object(server, "_prepare_action_target", return_value=tgt),
            patch.object(server, "_verify_action_target"),
            patch.object(
                visibility, "_window_rect", return_value=wintypes.RECT(0, 0, 100, 100)
            ),
            patch.object(platform_win32.user32, "SetCursorPos", return_value=True),
            patch.object(platform_win32.user32, "mouse_event"),
            patch.object(server.time, "sleep"),
        )
        prepare, verify, rect, set_pos, mouse_event, sleep = patches
        with prepare, verify, rect, sleep:
            with set_pos as set_pos_mock, mouse_event as mouse_mock:
                with self.assertRaises(errors.InputRejectedError):
                    server.drag_window(
                        hwnd=42,
                        start_x=10,
                        start_y=10,
                        end_x=40,
                        end_y=40,
                        confirmation_token=token_id,
                        path=too_many,
                    )
                set_pos_mock.assert_not_called()
                mouse_mock.assert_not_called()
                # Same token still live: a valid drag consumes it exactly once.
                result = server.drag_window(
                    hwnd=42,
                    start_x=10,
                    start_y=10,
                    end_x=40,
                    end_y=40,
                    confirmation_token=token_id,
                    path=[[20, 20]],
                )
        self.assertIn("1 waypoint", result)
        with self.assertRaises(errors.PolicyDeniedError) as ctx:
            server.consume_confirmation(token_id, server._target_identity(tgt), "drag")
        self.assertIn("already consumed", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
