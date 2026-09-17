"""Post-effect foreground-shift witness (2026-09-17 live incident).

Scope: two MCP server instances in activate mode raced `SetForegroundWindow`
on different windows and one's action was `interrupted` — the classic
verify→emit TOCTOU: `_verify_action_target` proves foreground BEFORE the
Win32 input call, but a rival `SetForegroundWindow` can land between that
check and `mouse_event`/`SendInput` (the click path even sleeps 0.15 s
mid-effect). The input cannot be un-sent, so the fix records a second
audit row (same correlation_id, outcome=error,
denial_reason=post_action_foreground_mismatch) instead of failing:
silent misdelivery becomes visible.

Firing rule (no false positives under mocks): an OBSERVED TRANSITION —
foreground was ours right after preparation AND is someone else's right
after the effect. A single mismatched read never fires.

File layout note: separate file so the red proof lands without touching
existing suites.
"""

import os
import sys
import unittest
from ctypes import wintypes
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from desktop_automation_mcp import audit, server, target, visibility
from desktop_automation_mcp import platform_win32

RIVAL_HWND = 99


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


class PostEffectForegroundTests(unittest.TestCase):
    def setUp(self):
        audit.clear_events()
        server._reset_rate_limits_for_tests()
        self.addCleanup(audit.clear_events)
        self.addCleanup(server._reset_rate_limits_for_tests)

    def _click_patches(self, tgt, fg, flip=True):
        """Fake desktop: foreground reads come from `fg`; the first
        `SetCursorPos` flips foreground to the rival (the other
        instance's SetForegroundWindow landing mid-effect). NOTE:
        `side_effect` is passed in the `patch.object` constructor —
        assigning it on an unstarted patcher silently does nothing."""

        def flip_first(*args, **kwargs):
            if flip:
                fg["hwnd"] = RIVAL_HWND
            return True

        return (
            _env_patch(
                DESKTOP_AUTOMATION_ALLOWED_TITLES="*Ruffle*",
                DESKTOP_AUTOMATION_ALLOWED_PROCESS_PATHS=tgt.process_path,
                DESKTOP_AUTOMATION_ALLOWED_ACTIONS="click",
            ),
            patch.object(server, "_prepare_action_target", return_value=tgt),
            patch.object(server, "_verify_action_target"),
            patch.object(
                visibility, "_window_rect", return_value=wintypes.RECT(0, 0, 100, 100)
            ),
            patch.object(
                platform_win32.user32,
                "GetForegroundWindow",
                side_effect=lambda: fg["hwnd"],
            ),
            patch.object(server.time, "sleep"),
            patch.object(platform_win32.user32, "SetCursorPos", side_effect=flip_first),
            patch.object(platform_win32.user32, "mouse_event"),
        )

    def test_stolen_mid_click_is_emitted_then_witnessed(self):
        """The proof: verification passed, the rival stole foreground at
        the first emission, yet DOWN+UP both fired unnoticed — and the
        post-effect check recorded the mismatch row."""
        tgt = _target()
        fg = {"hwnd": tgt.hwnd}
        patches = self._click_patches(tgt, fg)
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            with patches[6], patches[7] as mouse_mock:
                result = server.click_window(hwnd=tgt.hwnd, x=10, y=10)
        # The emission completed UNDER the stolen foreground: the code
        # never re-checked mid-effect (both DOWN and UP fired).
        kinds = [c.args[0] for c in mouse_mock.call_args_list]
        self.assertIn(server.MOUSEEVENTF_LEFTDOWN, kinds)
        self.assertIn(server.MOUSEEVENTF_LEFTUP, kinds)
        self.assertIn("Clicked", result)
        # ...but the theft is no longer silent: a second audit row with
        # the SAME correlation id flags it.
        correlation_id = result.rsplit("correlation_id=", 1)[1].rstrip(".")
        events = audit.read_events()
        mismatch = [
            event
            for event in events
            if event["correlation_id"] == correlation_id
            and "post_action_foreground_mismatch" in event.get("denial_reason", "")
        ]
        self.assertEqual(len(mismatch), 1)
        self.assertEqual(mismatch[0]["outcome"], audit.OUTCOME_ERROR)
        self.assertNotIn("target_identity", mismatch[0])

    def _mismatch_rows(self, result):
        correlation_id = result.rsplit("correlation_id=", 1)[1].rstrip(".")
        return [
            event
            for event in audit.read_events()
            if event["correlation_id"] == correlation_id
            and "post_action_foreground_mismatch" in event.get("denial_reason", "")
        ]

    def _fg_reader(self, fg):
        return patch.object(
            platform_win32.user32,
            "GetForegroundWindow",
            side_effect=lambda: fg["hwnd"],
        )

    def test_stolen_mid_drag_is_witnessed(self):
        """Same pattern on `drag_window`: steal at the first cursor move,
        the drag still completes, the mismatch row lands."""
        tgt = _target()
        fg = {"hwnd": tgt.hwnd}

        def flip_first(*args, **kwargs):
            fg["hwnd"] = RIVAL_HWND
            return True

        with (
            patch.object(server, "_prepare_action_target", return_value=tgt),
            patch.object(server, "consume_confirmation"),
            patch.object(server, "_verify_action_target"),
            patch.object(
                visibility, "_window_rect", return_value=wintypes.RECT(0, 0, 100, 100)
            ),
            self._fg_reader(fg),
            patch.object(server.time, "sleep"),
            patch.object(platform_win32.user32, "SetCursorPos", side_effect=flip_first),
            patch.object(platform_win32.user32, "mouse_event"),
        ):
            result = server.drag_window(
                hwnd=tgt.hwnd,
                start_x=10,
                start_y=10,
                end_x=40,
                end_y=40,
                confirmation_token="tok-1",
            )
        self.assertIn("Dragged", result)
        mismatch = self._mismatch_rows(result)
        self.assertEqual(len(mismatch), 1)
        self.assertEqual(mismatch[0]["outcome"], audit.OUTCOME_ERROR)

    def test_stolen_mid_text_is_witnessed(self):
        """Same pattern on `send_text` (unicode path): steal inside the
        first emission, the send still completes, the mismatch row lands."""
        tgt = _target()
        fg = {"hwnd": tgt.hwnd}

        def flip_only(target_arg, text_arg):
            # Pure mock emission (unicode internals are covered by the
            # chaos suite): flip foreground, pretend the send landed.
            fg["hwnd"] = RIVAL_HWND
            return None

        with (
            patch.object(server, "_prepare_action_target", return_value=tgt),
            patch.object(server, "consume_confirmation"),
            patch.object(server, "_verify_action_target"),
            self._fg_reader(fg),
            patch.object(server, "_send_unicode_text", side_effect=flip_only),
            patch.object(server.time, "sleep"),
        ):
            result = server.send_text(
                hwnd=tgt.hwnd, text="AB", confirmation_token="tok-1"
            )
        self.assertIn("Text input events sent", result)
        mismatch = self._mismatch_rows(result)
        self.assertEqual(len(mismatch), 1)
        self.assertEqual(mismatch[0]["outcome"], audit.OUTCOME_ERROR)

    def test_stolen_mid_key_sequence_is_witnessed(self):
        """Same pattern on `send_key_sequence`: steal at the first key
        press, later steps still fire, the mismatch row lands."""
        tgt = _target()
        fg = {"hwnd": tgt.hwnd}
        flipped = {"done": False}

        def flip_first(code, *, key_up=False):
            # Pure mock emission (no real SendInput: this dev machine's
            # foreground window must never receive test keystrokes).
            if not flipped["done"]:
                flipped["done"] = True
                fg["hwnd"] = RIVAL_HWND
            return None

        with _env_patch(
            DESKTOP_AUTOMATION_ALLOWED_TITLES="*Ruffle*",
            DESKTOP_AUTOMATION_ALLOWED_PROCESS_PATHS=tgt.process_path,
            DESKTOP_AUTOMATION_ALLOWED_ACTIONS="key",
        ):
            with (
                patch.object(server, "_prepare_action_target", return_value=tgt),
                patch.object(server, "_verify_action_target"),
                self._fg_reader(fg),
                patch.object(server, "_send_vk", side_effect=flip_first),
                patch.object(server.time, "sleep"),
            ):
                result = server.send_key_sequence(
                    keys=[{"key": "a"}, {"key": "b"}], hold_ms=0, hwnd=tgt.hwnd
                )
        self.assertIn("2 step", result)
        mismatch = self._mismatch_rows(result)
        self.assertEqual(len(mismatch), 1)
        self.assertEqual(mismatch[0]["outcome"], audit.OUTCOME_ERROR)

    def test_stolen_mid_close_is_witnessed(self):
        """Same pattern on `close_window`: steal at `PostMessageW`, the
        request still goes out, the mismatch row lands."""
        tgt = _target()
        fg = {"hwnd": tgt.hwnd}

        def flip_first(*args, **kwargs):
            fg["hwnd"] = RIVAL_HWND
            return True

        with (
            patch.object(server, "_prepare_action_target", return_value=tgt),
            patch.object(server, "consume_confirmation"),
            patch.object(server, "_verify_action_target"),
            self._fg_reader(fg),
            patch.object(server.time, "sleep"),
            patch.object(platform_win32.user32, "PostMessageW", side_effect=flip_first),
        ):
            result = server.close_window(hwnd=tgt.hwnd, confirmation_token="tok-1")
        self.assertIn("Close request sent", result)
        mismatch = self._mismatch_rows(result)
        self.assertEqual(len(mismatch), 1)
        self.assertEqual(mismatch[0]["outcome"], audit.OUTCOME_ERROR)

    def test_stable_foreground_records_no_mismatch(self):
        """No transition, no row: a clean effect keeps its single
        allowed audit row (this is what guards the 346 existing tests)."""
        tgt = _target()
        fg = {"hwnd": tgt.hwnd}
        patches = self._click_patches(tgt, fg, flip=False)
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            with patches[6], patches[7]:
                result = server.click_window(hwnd=tgt.hwnd, x=10, y=10)
        self.assertIn("Clicked", result)
        self.assertEqual(fg["hwnd"], tgt.hwnd)  # nobody stole anything
        events = audit.read_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["outcome"], audit.OUTCOME_ALLOWED)

    def test_observe_tool_skips_the_witness(self):
        """`require_foreground=False` tools never probe: a mid-capture
        foreground change is not their business (reading pixels steals
        no focus)."""
        from PIL import Image

        tgt = _target()
        fg = {"hwnd": tgt.hwnd}
        image = Image.new("RGBA", (60, 60), (255, 255, 255, 255))

        def flip_first(*args, **kwargs):
            fg["hwnd"] = RIVAL_HWND
            return image

        constraints = {
            "safe_regions": None,
            "screenshot_masks": [],
            "max_screenshot_bytes": 1_000_000,
        }
        with _env_patch(DESKTOP_AUTOMATION_ALLOWED_ACTIONS="observe"):
            with (
                patch.object(server, "_prepare_action_target", return_value=tgt),
                patch.object(server, "_verify_action_target"),
                patch.object(server, "_verify_observable"),
                patch.object(
                    server, "_window_rect", return_value=wintypes.RECT(0, 0, 100, 100)
                ),
                patch.object(
                    server, "_screenshot_constraints", return_value=constraints
                ),
                self._fg_reader(fg),
                patch.object(server.ImageGrab, "grab", side_effect=flip_first),
            ):
                server.screenshot_window(
                    hwnd=tgt.hwnd,
                    crop_left=0,
                    crop_top=0,
                    crop_right=0,
                    crop_bottom=0,
                )
        self.assertEqual(fg["hwnd"], RIVAL_HWND)
        events = audit.read_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["outcome"], audit.OUTCOME_ALLOWED)

    def test_broken_foreground_probe_never_breaks_the_result(self):
        """Fail-safe: if the probe itself raises (foreign platform), the
        effect result and its single allowed row are untouched."""
        tgt = _target()
        patches = self._click_patches(tgt, {"hwnd": tgt.hwnd})
        with patches[0], patches[1], patches[2], patches[3], patches[5]:
            with (
                patch.object(
                    platform_win32.user32,
                    "GetForegroundWindow",
                    side_effect=OSError("no desktop (simulated)"),
                ),
                patches[6],
                patches[7],
            ):
                result = server.click_window(hwnd=tgt.hwnd, x=10, y=10)
        self.assertIn("Clicked", result)
        events = audit.read_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["outcome"], audit.OUTCOME_ALLOWED)

    def test_steal_and_return_leaves_no_row_characterization(self):
        """RESIDUAL (pinned-as-observed, NOT the desired contract): a
        steal that RETURNS before the post-effect read is invisible to
        the witness — fg_before and fg_after are both ours, yet key step
        1 was emitted while the rival owned the foreground (SendInput is
        foreground-directed, so that step really misdelivered). A
        row-demanding variant of this test stays RED by design: closing
        the gap needs continuous polling during emission, which is
        disproportionate for a sub-verify-gap flutter. Sustained steals
        are still caught loudly (per-step `_verify_action_target` denies
        mid-sequence; the 5 witness tests pin the permanent-steal case).
        See the ROADMAP adversarial-review note."""
        tgt = _target()
        fg = {"hwnd": tgt.hwnd}
        visited = {"rival": False}

        def flutter(code, *, key_up=False):
            # Pure mock emission (no real SendInput — see above): step 1
            # press finds the rival owning the foreground, step 1 release
            # finds us back. The steal fits entirely inside one step.
            if not visited["rival"]:
                visited["rival"] = True
                fg["hwnd"] = RIVAL_HWND
            else:
                fg["hwnd"] = tgt.hwnd
            return None

        with _env_patch(
            DESKTOP_AUTOMATION_ALLOWED_TITLES="*Ruffle*",
            DESKTOP_AUTOMATION_ALLOWED_PROCESS_PATHS=tgt.process_path,
            DESKTOP_AUTOMATION_ALLOWED_ACTIONS="key",
        ):
            with (
                patch.object(server, "_prepare_action_target", return_value=tgt),
                patch.object(server, "_verify_action_target"),
                self._fg_reader(fg),
                patch.object(server, "_send_vk", side_effect=flutter),
                patch.object(server.time, "sleep"),
            ):
                result = server.send_key_sequence(
                    keys=[{"key": "a"}, {"key": "b"}], hold_ms=0, hwnd=tgt.hwnd
                )
        self.assertIn("2 step", result)
        self.assertTrue(visited["rival"])  # the steal really happened
        self.assertEqual(fg["hwnd"], tgt.hwnd)  # ...and returned in time
        self.assertEqual(self._mismatch_rows(result), [])  # ...unwitnessed


if __name__ == "__main__":
    unittest.main()
