"""Screenshot-crop / click-coordinate mismatch tests (ROADMAP backlog item).

Scope: `screenshot_window` returns a CROPPED image by default
(crop_left=12, crop_top=40, crop_right=12, crop_bottom=12) while
`click_window`/`drag_window`/`hover_window` expect RAW window-relative
coordinates. Nothing today exposes the offset between the two systems, so
a caller reading (x, y) off a default screenshot clicks the wrong spot by
the crop offset. These tests pin the contract: the returned `_meta` must
carry the translation (`window_offset` + `image_size`) in both crop and
named-region modes.

File layout note: separate from `tests/test_server.py` so the red test
lands without touching existing suites.
"""

import os
import sys
import tempfile
import unittest
from ctypes import wintypes
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from desktop_automation_mcp import server, target

POLICY_VARS = [
    "DESKTOP_AUTOMATION_POLICY_FILE",
    "DESKTOP_AUTOMATION_ALLOWED_TITLES",
    "DESKTOP_AUTOMATION_ALLOWED_PROCESS_PATHS",
    "DESKTOP_AUTOMATION_ALLOWED_ACTIONS",
    "DESKTOP_AUTOMATION_FOCUS_MODE",
    "DESKTOP_AUTOMATION_TEXT_MODE",
    "DESKTOP_AUTOMATION_ALLOW_UNRESTRICTED_SCREENSHOTS",
]


def _env_patch(**overrides):
    env = dict(os.environ)
    for var in POLICY_VARS:
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


class ScreenshotCropOffsetTests(unittest.TestCase):
    def _screenshot(self, tgt, **kwargs):
        fake_image = MagicMock()
        with (
            patch.object(target, "_enum_windows", return_value=[tgt]),
            patch.object(server, "_focus_and_verify"),
            patch.object(server, "_verify_action_target"),
            patch.object(server, "_verify_observable"),
            patch.object(
                server, "_window_rect", return_value=wintypes.RECT(0, 0, 100, 100)
            ),
            patch.object(server.ImageGrab, "grab", return_value=fake_image),
        ):
            return server.screenshot_window(hwnd=tgt.hwnd, **kwargs)

    def test_default_crop_exposes_window_offset_in_meta(self):
        # 100x100 window, default crops (12,40,12,12): image pixel (0,0)
        # is raw window-relative (12,40). The meta must say so.
        tgt = _target()
        with _env_patch(
            DESKTOP_AUTOMATION_ALLOWED_ACTIONS="observe",
            DESKTOP_AUTOMATION_ALLOW_UNRESTRICTED_SCREENSHOTS="true",
        ):
            result = self._screenshot(tgt)
        self.assertIsNotNone(result.meta)
        self.assertEqual(result.meta["window_offset"], [12, 40])

    def test_default_crop_exposes_image_size_in_meta(self):
        # 100-12-12 = 76 wide, 100-40-12 = 48 tall.
        tgt = _target()
        with _env_patch(
            DESKTOP_AUTOMATION_ALLOWED_ACTIONS="observe",
            DESKTOP_AUTOMATION_ALLOW_UNRESTRICTED_SCREENSHOTS="true",
        ):
            result = self._screenshot(tgt)
        self.assertEqual(result.meta["image_size"], [76, 48])

    def test_meta_offset_translates_image_point_to_click_coordinates(self):
        # The contract the fix guarantees: image (10,10) + offset ==
        # the raw window-relative point click_window expects, inside the
        # 100x100 window bounds.
        tgt = _target()
        with _env_patch(
            DESKTOP_AUTOMATION_ALLOWED_ACTIONS="observe",
            DESKTOP_AUTOMATION_ALLOW_UNRESTRICTED_SCREENSHOTS="true",
        ):
            result = self._screenshot(tgt)
        ox, oy = result.meta["window_offset"]
        w, h = result.meta["image_size"]
        win_x, win_y = 10 + ox, 10 + oy
        self.assertEqual((win_x, win_y), (22, 50))
        self.assertTrue(0 <= win_x < 100 and 0 <= win_y < 100)
        self.assertEqual([ox + w, oy + h], [88, 88])

    def test_zero_crop_is_identity_offset(self):
        # Backward compatibility: callers that already zero the crops
        # (the old workaround) see a zero offset — their math is unchanged.
        tgt = _target()
        with _env_patch(
            DESKTOP_AUTOMATION_ALLOWED_ACTIONS="observe",
            DESKTOP_AUTOMATION_ALLOW_UNRESTRICTED_SCREENSHOTS="true",
        ):
            result = self._screenshot(
                tgt, crop_left=0, crop_top=0, crop_right=0, crop_bottom=0
            )
        self.assertEqual(result.meta["window_offset"], [0, 0])
        self.assertEqual(result.meta["image_size"], [100, 100])

    def test_named_region_mode_exposes_region_origin_in_meta(self):
        # Same mechanism covers policy-file region mode: region
        # [12,40,88,88] in a 100x100 window behaves like the default crop.
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "region.yaml"
        path.write_text(
            "application:\n"
            '  executable_path: "C:\\\\Program Files\\\\ruffle\\\\bin\\\\ruffle.exe"\n'
            "  title_patterns:\n"
            "    - *Ruffle*\n"
            "  allowed_actions:\n"
            "    - observe\n"
            "  safe_regions:\n"
            "    - name: stage\n"
            "      rect: [12, 40, 88, 88]\n",
            encoding="utf-8",
        )
        tgt = _target()
        with _env_patch(DESKTOP_AUTOMATION_POLICY_FILE=str(path)):
            result = self._screenshot(tgt, region_name="stage")
        self.assertEqual(result.meta["window_offset"], [12, 40])
        self.assertEqual(result.meta["image_size"], [76, 48])

    def test_correlation_id_still_present_alongside_offset(self):
        # The new keys are additive: the existing correlation contract
        # (pinned by test_server.py) must keep working.
        tgt = _target()
        with _env_patch(
            DESKTOP_AUTOMATION_ALLOWED_ACTIONS="observe",
            DESKTOP_AUTOMATION_ALLOW_UNRESTRICTED_SCREENSHOTS="true",
        ):
            result = self._screenshot(tgt)
        self.assertIn("correlation_id", result.meta)
        self.assertEqual(len(result.meta["correlation_id"]), 12)


if __name__ == "__main__":
    unittest.main()
