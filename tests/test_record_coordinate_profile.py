"""Coordinate-profile recording helper tests (ROADMAP backlog item).

Scope: profile files are hand-written today and only validated after the
fact (`tools/validate_coordinate_profile.py`); nothing PRODUCES one from
a live window. `record_coordinate_profile` closes that gap: an
`observe`-class MCP tool that samples the live reference region through
the same capture pipeline `check_coordinate_profile` verifies with, and
returns a complete profile dict (never writes a file — saving stays with
the operator).

Contract pinned here: the returned dict passes the REAL
`coordinate_profile.validate_profile()` (no invented field names), the
hash is deterministic across identical captures, and caller bugs
(unknown reference name, out-of-region points) fail fast with
`InputRejectedError`.

File layout note: separate from `tests/test_server.py` so the red test
lands without touching existing suites.
"""

import os
import sys
import unittest
from ctypes import wintypes
from pathlib import Path
from unittest.mock import patch

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from desktop_automation_mcp import coordinate_profile, errors, server, target

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


def _record(**overrides):
    kwargs = {
        "profile_id": "ruffle-test-v1",
        "reference_region_name": "stage",
        "safe_regions": [
            {"name": "stage", "rect": [10, 10, 60, 60]},
            {"name": "corner", "rect": [60, 60, 90, 90]},
        ],
        "verification_points": [[15, 15], [50, 50]],
        "hwnd": 42,
    }
    kwargs.update(overrides)
    result = server.record_coordinate_profile(**kwargs)
    # The tool returns {"profile": <pure schema dict>, "correlation_id"}:
    # pin the envelope here so every assertion below runs against the
    # exactly-eight-fields profile the operator would save to disk.
    assert set(result) == {"profile", "correlation_id"}, result.keys()
    return result["profile"]


class RecordCoordinateProfileTests(unittest.TestCase):
    def setUp(self):
        server._reset_rate_limits_for_tests()
        self.addCleanup(server._reset_rate_limits_for_tests)

    def _live_capture(self):
        # 50x50 solid image standing in for the live reference region.
        return Image.new("RGB", (50, 50), (10, 20, 30))

    def _patches(self, tgt):
        return (
            patch.object(target, "_enum_windows", return_value=[tgt]),
            patch.object(server, "_focus_and_verify"),
            patch.object(server, "_verify_action_target"),
            patch.object(server, "_verify_observable"),
            patch.object(
                server, "_window_rect", return_value=wintypes.RECT(0, 0, 100, 100)
            ),
            patch.object(server.ImageGrab, "grab", return_value=self._live_capture()),
        )

    def test_returns_complete_profile_passing_real_validation(self):
        tgt = _target()
        patches = self._patches(tgt)
        with _observe_env():
            with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
                profile = _record()
        # The REAL validator is the authority: no invented field names.
        self.assertEqual(coordinate_profile.validate_profile_document(profile), [])
        self.assertIsNone(coordinate_profile.validate_profile(profile))
        self.assertEqual(profile["profile_id"], "ruffle-test-v1")
        self.assertEqual(profile["window_size"], {"width": 100, "height": 100})
        self.assertEqual(profile["reference_region_name"], "stage")
        self.assertEqual(profile["application"]["process_path"], tgt.process_path)
        self.assertRegex(profile["reference_image_hash"], r"^[0-9a-f]{64}$")
        self.assertEqual(len(profile["verification_points"]), 2)
        for point in profile["verification_points"]:
            self.assertEqual(point["expected_color"], [10, 20, 30])

    def test_hash_deterministic_across_identical_captures(self):
        tgt = _target()
        with _observe_env():
            with (
                self._patches(tgt)[0],
                self._patches(tgt)[1],
                self._patches(tgt)[2],
                self._patches(tgt)[3],
                self._patches(tgt)[4],
                self._patches(tgt)[5],
            ):
                first = _record()
            with (
                self._patches(tgt)[0],
                self._patches(tgt)[1],
                self._patches(tgt)[2],
                self._patches(tgt)[3],
                self._patches(tgt)[4],
                self._patches(tgt)[5],
            ):
                second = _record()
        self.assertEqual(first["reference_image_hash"], second["reference_image_hash"])

    def test_unknown_reference_region_name_rejected(self):
        tgt = _target()
        patches = self._patches(tgt)
        with _observe_env():
            with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
                with self.assertRaises(errors.InputRejectedError):
                    _record(reference_region_name="nope")

    def test_point_outside_reference_region_rejected(self):
        tgt = _target()
        patches = self._patches(tgt)
        with _observe_env():
            with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
                with self.assertRaises(errors.InputRejectedError):
                    _record(verification_points=[[80, 80]])

    def test_recorded_profile_resolves_points_for_consumers(self):
        # The recorded profile is immediately usable by the consumer side:
        # "corner" [60,60,90,90] resolves to its floor-division center.
        tgt = _target()
        patches = self._patches(tgt)
        with _observe_env():
            with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
                profile = _record()
        self.assertEqual(coordinate_profile.resolve_point(profile, "corner"), (75, 75))
        self.assertEqual(coordinate_profile.resolve_point(profile, "stage"), (35, 35))

    def test_requires_observe_permission(self):
        with _env_patch(
            DESKTOP_AUTOMATION_ALLOWED_TITLES="*Ruffle*",
            DESKTOP_AUTOMATION_ALLOWED_PROCESS_PATHS=RUFFLE_EXE,
            DESKTOP_AUTOMATION_ALLOWED_ACTIONS="click",
        ):
            with self.assertRaises(errors.PolicyDeniedError):
                _record()

    def test_returns_dict_with_schema_keys_not_a_file_path(self):
        tgt = _target()
        patches = self._patches(tgt)
        with _observe_env():
            with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
                profile = _record()
        self.assertIsInstance(profile, dict)
        self.assertEqual(
            sorted(profile),
            sorted(
                [
                    "profile_id",
                    "application",
                    "window_size",
                    "reference_region_name",
                    "reference_image_hash",
                    "safe_regions",
                    "verification_points",
                    "created_at",
                ]
            ),
        )


if __name__ == "__main__":
    unittest.main()
