"""Chaos tests: window closing, PID recycling, rect disagreement,
occlusion and SendInput rejection (ROADMAP §10 Phase 4).

Every scenario calls REAL `target.py` / `visibility.py` / `server.py`
functions; only the Win32 layer is faked via `fake_platform.py`. The
existing `tests/test_server.py` is untouched (it stays in its own mock
pattern); this file is only for NEW scenarios.

SKIPPED SCENARIOS (deliberately, not silently):

- Loss of a UIA element: UIA integration was DEFERRED via ADR-0001; there
  is no UIA element to lose in the middle of anything. This file will be
  extended if that threshold is crossed.
- Audit write failure: `audit.py` (`record_event`, the in-memory ring
  buffer) is NOW called from inside `_prepare_action_target`; what the
  action should do on a recording failure is that integration line's
  contract and is still in flux. Stubbing it here would CONFLICT with
  that line; the gap should be filled once the audit API stabilizes.
"""

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))

from desktop_automation_mcp import (
    confirmation,
    errors,
    input_ as input_mod,
    server,
    target,
    visibility,
)
from fake_platform import FakeWin32Platform
from desktop_automation_mcp.audit import clear_events

HWND = 42
PID = 100
STARTED_AT = 1111
EXE = r"C:\fake\chaos-app.exe"
TITLE = "Ruffle chaos"
RECT = (0, 0, 200, 200)

IDENTITY = {"hwnd": HWND, "pid": PID, "process_started_at": STARTED_AT}


def _patched_env(actions):
    """Isolates the policy environment (no DESKTOP_AUTOMATION_* leakage)."""
    base = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("DESKTOP_AUTOMATION_")
    }
    base.update(
        {
            "DESKTOP_AUTOMATION_ALLOWED_TITLES": "*Ruffle*",
            "DESKTOP_AUTOMATION_ALLOWED_PROCESS_PATHS": EXE,
            "DESKTOP_AUTOMATION_ALLOWED_ACTIONS": actions,
        }
    )
    return patch.dict(os.environ, base, clear=True)


def _platform(test_case, rect=RECT):
    """A fake desktop with a single window, foreground on the target; returns it patched."""
    fake = FakeWin32Platform()
    fake.add_window(HWND, PID, TITLE, rect, STARTED_AT, EXE)
    fake.set_foreground(HWND)
    return fake.patch_into(test_case)


def _text_token():
    """A token from the real confirmation store, bound to the fake target."""
    store = confirmation.ConfirmationStore()
    token_id = store.issue(dict(IDENTITY), "text")["token_id"]
    consume = patch.object(server, "consume_confirmation", side_effect=store.consume)
    consume.start()
    return token_id, consume.stop


class WindowClosedMidActionTests(unittest.TestCase):
    def test_send_aborts_on_close_with_no_held_key(self):
        # `send_key` does NOT re-verify within the action (a single gate);
        # the per-unit re-verifying `_send_unicode_text` path is where the
        # chaos will show up — that is why the tool used here is
        # `send_text`.
        fake = _platform(self)
        token_id, stop_consume = _text_token()
        self.addCleanup(stop_consume)
        self.addCleanup(clear_events)
        fake.after_sends(2, lambda platform: platform.close_window(HWND))
        with _patched_env("observe,text"):
            with self.assertRaises(errors.TargetStaleError) as ctx:
                server.send_text(hwnd=HWND, text="AB", confirmation_token=token_id)
        self.assertEqual(ctx.exception.code, errors.TARGET_STALE)
        # Unit A went through completely (down+up); B was never sent.
        accepted = [call for call in fake.sendinput_calls if call.accepted]
        self.assertEqual(len(accepted), 2)
        self.assertEqual(fake.held_keys(), [])


class PidRecyclingTests(unittest.TestCase):
    def test_recycled_pid_is_rejected_as_stale(self):
        fake = _platform(self)
        token_id, stop_consume = _text_token()
        self.addCleanup(stop_consume)
        self.addCleanup(clear_events)
        fake.after_sends(2, lambda platform: platform.set_pid(HWND, 200, 2222))
        with _patched_env("observe,text"):
            with self.assertRaises(errors.TargetStaleError) as ctx:
                server.send_text(hwnd=HWND, text="AB", confirmation_token=token_id)
        self.assertEqual(ctx.exception.code, errors.TARGET_STALE)
        self.assertEqual(fake.held_keys(), [])
        accepted = [call for call in fake.sendinput_calls if call.accepted]
        self.assertEqual(len(accepted), 2)


class OcclusionTests(unittest.TestCase):
    def test_window_covered_mid_action_is_rejected_as_occluded(self):
        fake = _platform(self)
        fake.add_window(99, 200, "Other", RECT, 2222, r"C:\fake\other.exe")
        token_id, stop_consume = _text_token()
        self.addCleanup(stop_consume)
        self.addCleanup(clear_events)
        # At preparation time the target is both foreground and on top; after
        # 2 sends, 99 is brought forward (foreground STAYS ON THE TARGET —
        # this is purely the occlusion branch).
        fake.after_sends(2, lambda platform: platform.bring_to_front(99))
        with _patched_env("observe,text"):
            with self.assertRaises(errors.TargetOccludedError) as ctx:
                server.send_text(hwnd=HWND, text="AB", confirmation_token=token_id)
        self.assertEqual(ctx.exception.code, errors.TARGET_OCCLUDED)
        self.assertEqual(fake.held_keys(), [])
        accepted = [call for call in fake.sendinput_calls if call.accepted]
        self.assertEqual(len(accepted), 2)


class SendInputRejectionTests(unittest.TestCase):
    def test_rejected_keypress_releases_modifiers(self):
        fake = _platform(self)
        self.addCleanup(clear_events)
        # ctrl-down goes through, "a"-down is rejected: `finally` must still
        # send ctrl-up (no modifier is left held down).
        fake.raise_next_sendinput_failure(after=1, times=1)
        with _patched_env("observe,key"):
            with self.assertRaises(errors.PlatformError) as ctx:
                server.send_key(hwnd=HWND, key="a", modifiers=["ctrl"], hold_ms=0)
        self.assertEqual(ctx.exception.code, errors.PLATFORM_ERROR)
        ctrl, key_a = input_mod.VK_MAP["ctrl"], input_mod.VK_MAP["a"]
        keyup = input_mod.KEYEVENTF_KEYUP
        self.assertEqual(
            [
                (call.vk, call.scan, call.flags, call.accepted)
                for call in fake.sendinput_calls
            ],
            [
                (ctrl, 0, 0, True),
                (key_a, 0, 0, False),
                (ctrl, 0, keyup, True),
            ],
        )
        self.assertEqual(fake.held_keys(), [])


class RectDisagreementTests(unittest.TestCase):
    """DPI ZOOM (bounds documented in `fake_platform.py`'s docstring):
    consecutive rect reads disagree. Proven: the identity gate does not
    look at geometry (still passes), the bounds check is done against the
    FRESH rect (stale geometry cannot produce an out-of-window effect).
    This is NOT the real %100/125/150 DPI matrix (that ROADMAP item
    stays open)."""

    def test_stale_geometry_cannot_produce_out_of_window_effects(self):
        fake = _platform(self, rect=(0, 0, 100, 100))
        self.addCleanup(clear_events)
        with (
            _patched_env("observe,click"),
            patch.object(server.time, "sleep"),
        ):
            server.click_window(hwnd=HWND, x=90, y=90)
            moves_before = len(fake.cursor_moves)
            clicks_before = len(fake.mouse_events)
            first = target._window_rect(HWND)
            fake.set_rect(HWND, (0, 0, 80, 80))
            second = target._window_rect(HWND)
            self.assertNotEqual(
                (first.left, first.top, first.right, first.bottom),
                (second.left, second.top, second.right, second.bottom),
            )
            # The identity gate does not look at geometry: the same target is still valid.
            snapshot = target._resolve_window(None, HWND)
            visibility._verify_action_target(snapshot)
            # But the stale coordinate is now OUTSIDE the window: reject without moving the cursor.
            with self.assertRaises(errors.InputRejectedError):
                server.click_window(hwnd=HWND, x=90, y=90)
        self.assertEqual(len(fake.cursor_moves), moves_before)
        self.assertEqual(len(fake.mouse_events), clicks_before)


if __name__ == "__main__":
    unittest.main()
