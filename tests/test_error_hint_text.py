"""Next-call hints in rejection text (ROADMAP lower-priority polish).

Scope: the `title_contains` collision message already suggests the next
call ("Multiple allowed windows matched; use the hwnd from list_windows()
output."), but `region_name` misses and `crop_*`/`grid` parameter errors
do not — the caller gets a bare rejection with no pointer to the valid
values. These tests pin the generalized pattern: every message below
names the concrete next call (valid values, live bounds, or an example)
without adding a new error class and without leaking window content
(titles, pixels, paths): only operator-owned config labels (region
names), caller-supplied strings, and plain numbers appear.

File layout note: separate from `tests/test_server.py` so the red tests
land without touching existing suites.
"""

import os
import sys
import unittest
from ctypes import wintypes
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from desktop_automation_mcp import errors, server, target


def _target():
    return target.TargetSnapshot(
        hwnd=42,
        title="Ruffle player",
        pid=1,
        rect=(0, 0, 100, 100),
        process_started_at=1,
        process_path="",
        window_class="",
    )


def _observe_env():
    return patch.dict(os.environ, {"DESKTOP_AUTOMATION_ALLOWED_ACTIONS": "observe"})


def _shot_patches(image, constraints, rect=(0, 0, 100, 100)):
    left, top, right, bottom = rect
    return (
        _observe_env(),
        patch.object(server, "_prepare_action_target", return_value=_target()),
        patch.object(
            server, "_window_rect", return_value=wintypes.RECT(left, top, right, bottom)
        ),
        patch.object(server, "_verify_action_target"),
        patch.object(server, "_screenshot_constraints", return_value=constraints),
        patch.object(server.ImageGrab, "grab", return_value=image),
    )


def _file_constraints():
    return {
        "safe_regions": [{"name": "content", "rect": [10, 20, 70, 80]}],
        "screenshot_masks": [],
        "max_screenshot_bytes": 1_000_000,
    }


def _env_constraints():
    return {
        "safe_regions": None,
        "screenshot_masks": [],
        "max_screenshot_bytes": 1_000_000,
    }


class RegionHintTests(unittest.TestCase):
    def test_region_miss_lists_allowed_names(self):
        patches = _shot_patches(MagicMock(), _file_constraints())
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            with self.assertRaises(errors.PolicyDeniedError) as ctx:
                server.screenshot_window(hwnd=42, region_name="nope")
        self.assertIn("'nope'", str(ctx.exception))
        self.assertIn("Allowed region_name values: content", str(ctx.exception))

    def test_missing_region_name_lists_allowed_names(self):
        patches = _shot_patches(MagicMock(), _file_constraints())
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            with self.assertRaises(errors.PolicyDeniedError) as ctx:
                server.screenshot_window(hwnd=42)
        self.assertIn("Allowed region_name values: content", str(ctx.exception))

    def test_region_name_in_env_mode_suggests_crops(self):
        patches = _shot_patches(MagicMock(), _env_constraints())
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            with self.assertRaises(errors.PolicyDeniedError) as ctx:
                server.screenshot_window(hwnd=42, region_name="content")
        self.assertIn("crop_*", str(ctx.exception))


class CropHintTests(unittest.TestCase):
    def test_invalid_crop_area_names_live_window_size(self):
        patches = _shot_patches(MagicMock(), _env_constraints())
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            with self.assertRaises(errors.InputRejectedError) as ctx:
                server.screenshot_window(
                    hwnd=42, crop_left=60, crop_top=0, crop_right=60, crop_bottom=0
                )
        self.assertIn("100x100", str(ctx.exception))
        self.assertIn("crop_left+crop_right", str(ctx.exception))

    def test_oversize_screenshot_names_limit_and_retry(self):
        patches = _shot_patches(
            MagicMock(), _env_constraints(), rect=(0, 0, 5000, 5000)
        )
        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patch.object(
                server, "virtual_screen_bounds", return_value=(0, 0, 10000, 10000)
            ),
        ):
            with self.assertRaises(errors.InputRejectedError) as ctx:
                server.screenshot_window(
                    hwnd=42, crop_left=0, crop_top=0, crop_right=0, crop_bottom=0
                )
        self.assertIn("16000000", str(ctx.exception))
        self.assertIn("crop further", str(ctx.exception))

    def test_negative_crop_suggests_full_window(self):
        with self.assertRaises(errors.InputRejectedError) as ctx:
            server.screenshot_window(hwnd=42, crop_left=-1)
        self.assertIn("all four as 0", str(ctx.exception))

    def test_non_boolean_grid_suggests_values(self):
        with self.assertRaises(errors.InputRejectedError) as ctx:
            server.screenshot_window(hwnd=42, grid="yes")
        self.assertIn("grid=True", str(ctx.exception))

    def test_bad_grid_spacing_suggests_default(self):
        with self.assertRaises(errors.InputRejectedError) as ctx:
            server.screenshot_window(hwnd=42, grid=True, grid_spacing=0)
        self.assertIn("default 50", str(ctx.exception))


class RecordReferenceHintTests(unittest.TestCase):
    def test_reference_miss_lists_supplied_names(self):
        with _observe_env():
            with self.assertRaises(errors.InputRejectedError) as ctx:
                server.record_coordinate_profile(
                    profile_id="ruffle-test-v1",
                    reference_region_name="nope",
                    safe_regions=[
                        {"name": "stage", "rect": [10, 10, 60, 60]},
                        {"name": "corner", "rect": [60, 60, 90, 90]},
                    ],
                    verification_points=[[15, 15]],
                    hwnd=42,
                )
        self.assertIn("stage", str(ctx.exception))
        self.assertIn("corner", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
