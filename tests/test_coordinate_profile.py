"""Coordinate profile tests (ROADMAP §8 Phase 2).

Scope: `src/desktop_automation_mcp/coordinate_profile.py` + example
profile file + the `tools/validate_coordinate_profile.py` CLI. Images
themselves are never stored anywhere; the tests use small SYNTHETIC byte
sequences (NO real screenshot).
"""

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from desktop_automation_mcp import coordinate_profile

REPO_ROOT = Path(__file__).resolve().parents[1]
VALIDATOR_PATH = REPO_ROOT / "tools" / "validate_coordinate_profile.py"
EXAMPLE_JSON = (
    REPO_ROOT / "schema" / "examples" / "coordinate_profile-ruffle-800x600.json"
)

# Source of the hash in the example file (NOT a real screenshot):
SYNTHETIC_BYTES = b"ruffle-reference-800x600-synthetic-v1"
SYNTHETIC_HASH = "3e84c9b0ba339366e3df0b6ee9b95d531edbf9e56816cb77215d4d889e2246e6"


def _base_profile() -> dict:
    return {
        "profile_id": "ruffle-800x600-v1",
        "application": {"process_path": r"C:\Program Files\ruffle\bin\ruffle.exe"},
        "window_size": {"width": 800, "height": 600},
        "reference_region_name": "menu",
        "reference_image_hash": SYNTHETIC_HASH,
        "safe_regions": [{"name": "menu", "rect": [280, 96, 520, 304]}],
        "verification_points": [{"point": [400, 200], "expected_color": [24, 24, 32]}],
        "created_at": "2026-09-14T12:00:00+03:00",
    }


def _mutated(mutate) -> dict:
    doc = _base_profile()
    mutate(doc)
    return doc


class ProfileValidationTests(unittest.TestCase):
    def test_valid_profile_passes(self):
        self.assertEqual(
            coordinate_profile.validate_profile_document(_base_profile()), []
        )
        self.assertIsNone(coordinate_profile.validate_profile(_base_profile()))

    def test_example_file_loads_and_validates(self):
        doc = coordinate_profile.load_profile_file(str(EXAMPLE_JSON))
        self.assertEqual(doc["profile_id"], "ruffle-800x600-v1")
        self.assertIsNone(coordinate_profile.validate_profile(doc))

    def test_missing_required_fields_rejected(self):
        cases = [
            "profile_id",
            "application",
            "window_size",
            "reference_region_name",
            "reference_image_hash",
            "safe_regions",
            "verification_points",
            "created_at",
        ]
        for field in cases:
            with self.subTest(field=field):
                doc = _mutated(lambda d: d.pop(field))
                problems = coordinate_profile.validate_profile_document(doc)
                self.assertTrue(
                    any(field in problem for problem in problems),
                    msg=f"absence of {field} was not caught: {problems}",
                )

    def test_profile_id_shape_rejected(self):
        for bad in ["", "a b", "-onsoz", "x" * 129, 42]:
            with self.subTest(value=bad):
                doc = _mutated(lambda d: d.update(profile_id=bad))
                self.assertNotEqual(
                    coordinate_profile.validate_profile_document(doc), []
                )

    def test_application_shape_rejected(self):
        doc = _mutated(lambda d: d.update(application="C:\\x.exe"))
        self.assertIn(
            "'application' must be an object",
            coordinate_profile.validate_profile_document(doc)[0],
        )
        doc = _mutated(lambda d: d["application"].pop("process_path"))
        self.assertTrue(
            any(
                "process_path" in p
                for p in coordinate_profile.validate_profile_document(doc)
            )
        )
        doc = _mutated(lambda d: d["application"].update(process_path="   "))
        self.assertTrue(
            any(
                "must not be empty" in p
                for p in coordinate_profile.validate_profile_document(doc)
            )
        )
        doc = _mutated(lambda d: d["application"].update(app_version=""))
        self.assertTrue(
            any(
                "app_version" in p
                for p in coordinate_profile.validate_profile_document(doc)
            )
        )
        doc = _mutated(lambda d: d["application"].update(focus_mode="x"))
        self.assertTrue(
            any(
                "unknown field" in p
                for p in coordinate_profile.validate_profile_document(doc)
            )
        )

    def test_window_size_shape_rejected(self):
        for width in [0, -800, True, "800"]:
            with self.subTest(width=width):
                doc = _mutated(lambda d: d["window_size"].update(width=width))
                problems = coordinate_profile.validate_profile_document(doc)
                self.assertTrue(
                    any("width" in p for p in problems), msg=f"{width!r}: {problems}"
                )

    def test_reference_hash_shape_rejected(self):
        for bad in ["xyz", "A" * 64, "3e84", 12345]:
            with self.subTest(value=str(bad)[:12]):
                doc = _mutated(lambda d: d.update(reference_image_hash=bad))
                problems = coordinate_profile.validate_profile_document(doc)
                self.assertTrue(
                    any("reference_image_hash" in p for p in problems),
                    msg=f"{bad!r}: {problems}",
                )

    def test_reference_region_must_name_exactly_one_safe_region(self):
        doc = _mutated(lambda d: d.update(reference_region_name="missing"))
        problems = coordinate_profile.validate_profile_document(doc)
        self.assertTrue(
            any("reference_region_name" in problem for problem in problems),
            msg=str(problems),
        )

    def test_verification_points_must_stay_inside_reference_region(self):
        doc = _mutated(
            lambda d: d.update(
                verification_points=[
                    {"point": [10, 10], "expected_color": [24, 24, 32]}
                ]
            )
        )
        problems = coordinate_profile.validate_profile_document(doc)
        self.assertTrue(
            any("reference region" in problem for problem in problems),
            msg=str(problems),
        )

    def test_region_rules_rejected(self):
        doc = _mutated(lambda d: d.update(safe_regions=[]))
        self.assertTrue(
            any(
                "at least 1 region" in p
                for p in coordinate_profile.validate_profile_document(doc)
            )
        )
        doc = _mutated(
            lambda d: d.update(
                safe_regions=[
                    {"name": "a", "rect": [0, 0, 10, 10]},
                    {"name": "a", "rect": [20, 20, 30, 30]},
                ]
            )
        )
        self.assertTrue(
            any(
                "duplicated" in p
                for p in coordinate_profile.validate_profile_document(doc)
            )
        )
        doc = _mutated(
            lambda d: d.update(
                safe_regions=[{"name": "reversed", "rect": [50, 50, 10, 10]}]
            )
        )
        self.assertTrue(
            any(
                "must be ordered" in p
                for p in coordinate_profile.validate_profile_document(doc)
            )
        )
        doc = _mutated(
            lambda d: d.update(
                safe_regions=[{"name": "oversized", "rect": [0, 0, 900, 10]}]
            )
        )
        self.assertTrue(
            any(
                "outside window bounds" in p
                for p in coordinate_profile.validate_profile_document(doc)
            )
        )
        doc = _mutated(lambda d: d.update(safe_regions=[{"name": "nameless"}]))
        self.assertTrue(
            any("rect" in p for p in coordinate_profile.validate_profile_document(doc))
        )

    def test_point_rules_rejected(self):
        doc = _mutated(lambda d: d.update(verification_points=[]))
        self.assertTrue(
            any(
                "at least 1 point" in p
                for p in coordinate_profile.validate_profile_document(doc)
            )
        )
        doc = _mutated(
            lambda d: d.update(
                verification_points=[{"point": [900, 10], "expected_color": [0, 0, 0]}]
            )
        )
        self.assertTrue(
            any(
                "outside window bounds" in p
                for p in coordinate_profile.validate_profile_document(doc)
            )
        )
        doc = _mutated(
            lambda d: d.update(
                verification_points=[{"point": [1, 2], "expected_color": [0, 0, 256]}]
            )
        )
        self.assertTrue(
            any(
                "expected_color" in p
                for p in coordinate_profile.validate_profile_document(doc)
            )
        )

    def test_created_at_rules_rejected(self):
        doc = _mutated(lambda d: d.update(created_at="2026-09-14T12:00:00"))
        self.assertTrue(
            any(
                "offset" in p for p in coordinate_profile.validate_profile_document(doc)
            )
        )
        doc = _mutated(lambda d: d.update(created_at="2099-01-01T00:00:00+03:00"))
        self.assertTrue(
            any(
                "in the future" in p
                for p in coordinate_profile.validate_profile_document(doc)
            )
        )

    def test_extra_root_field_rejected(self):
        doc = _mutated(lambda d: d.update(screenshot_bytes="..."))
        self.assertTrue(
            any(
                "unknown field" in p
                for p in coordinate_profile.validate_profile_document(doc)
            )
        )

    def test_validate_profile_raises_permission_error_not_value_error(self):
        doc = _mutated(lambda d: d.pop("profile_id"))
        with self.assertRaises(PermissionError) as ctx:
            coordinate_profile.validate_profile(doc)
        self.assertNotIsInstance(ctx.exception, ValueError)
        self.assertIn("profile_id", str(ctx.exception))

    def test_load_missing_file_is_oserror_not_permission_error(self):
        with self.assertRaises(OSError) as ctx:
            coordinate_profile.load_profile_file(str(REPO_ROOT / "schema" / "yok.json"))
        self.assertNotIsInstance(ctx.exception, PermissionError)


class ResolvePointTests(unittest.TestCase):
    def test_center_is_returned(self):
        doc = _base_profile()
        # ((280+520)//2, (96+304)//2) == (400, 200)
        self.assertEqual(coordinate_profile.resolve_point(doc, "menu"), (400, 200))

    def test_unknown_region_lists_available_names(self):
        with self.assertRaises(PermissionError) as ctx:
            coordinate_profile.resolve_point(_base_profile(), "yok-bolge")
        message = str(ctx.exception)
        self.assertIn("yok-bolge", message)
        self.assertIn("menu", message)

    def test_copy_of_profile_is_not_mutated(self):
        doc = _base_profile()
        before = copy.deepcopy(doc)
        coordinate_profile.resolve_point(doc, "menu")
        self.assertEqual(doc, before)


class MatchLiveWindowTests(unittest.TestCase):
    def test_full_match(self):
        matched, reason = coordinate_profile.profile_matches_live_window(
            _base_profile(), 800, 600, SYNTHETIC_BYTES
        )
        self.assertTrue(matched, msg=reason)
        self.assertIn("matched", reason)

    def test_size_mismatch_has_own_message(self):
        matched, reason = coordinate_profile.profile_matches_live_window(
            _base_profile(), 1024, 768, SYNTHETIC_BYTES
        )
        self.assertFalse(matched)
        self.assertIn("size mismatch", reason)
        self.assertIn("1024x768", reason)

    def test_hash_mismatch_has_own_message(self):
        matched, reason = coordinate_profile.profile_matches_live_window(
            _base_profile(), 800, 600, b"another-image"
        )
        self.assertFalse(matched)
        self.assertIn("hash", reason)

    def test_missing_bytes_skips_hash_explicitly(self):
        matched, reason = coordinate_profile.profile_matches_live_window(
            _base_profile(), 800, 600, None
        )
        self.assertTrue(matched)
        self.assertIn("skipped", reason)

    def test_invalid_profile_never_matches(self):
        matched, reason = coordinate_profile.profile_matches_live_window(
            _mutated(lambda d: d.pop("profile_id")), 800, 600, SYNTHETIC_BYTES
        )
        self.assertFalse(matched)
        self.assertIn("profile invalid", reason)

    def test_example_bytes_match_example_file(self):
        doc = coordinate_profile.load_profile_file(str(EXAMPLE_JSON))
        matched, reason = coordinate_profile.profile_matches_live_window(
            doc, 800, 600, SYNTHETIC_BYTES
        )
        self.assertTrue(matched, msg=reason)


class PixelMatchTests(unittest.TestCase):
    def test_exact_match(self):
        point = {"point": [1, 2], "expected_color": [24, 24, 32]}
        self.assertTrue(coordinate_profile.pixel_matches(point, (24, 24, 32)))
        self.assertTrue(coordinate_profile.pixel_matches(point, [24, 24, 32]))

    def test_off_by_one_is_rejected(self):
        point = {"point": [1, 2], "expected_color": [24, 24, 32]}
        self.assertFalse(coordinate_profile.pixel_matches(point, (24, 24, 33)))

    def test_malformed_inputs_are_rejected(self):
        self.assertFalse(coordinate_profile.pixel_matches({}, (0, 0, 0)))
        self.assertFalse(
            coordinate_profile.pixel_matches(
                {"point": [1, 2], "expected_color": [0, 0, 0]}, "siyah"
            )
        )


class ProfileCliTests(unittest.TestCase):
    def test_cli_validates_example_with_exit_zero(self):
        proc = subprocess.run(
            [sys.executable, str(VALIDATOR_PATH), str(EXAMPLE_JSON)],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        self.assertIn("OK", proc.stdout)

    def test_cli_reports_rule_violation_with_exit_one(self):
        doc = _base_profile()
        doc.pop("profile_id")
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        ) as handle:
            json.dump(doc, handle)
            invalid = handle.name
        try:
            proc = subprocess.run(
                [sys.executable, str(VALIDATOR_PATH), invalid],
                capture_output=True,
                text=True,
                cwd=str(REPO_ROOT),
            )
        finally:
            Path(invalid).unlink(missing_ok=True)
        self.assertEqual(proc.returncode, 1, msg=proc.stderr)
        self.assertIn("profile_id", proc.stdout)

    def test_cli_missing_file_is_probe_error(self):
        proc = subprocess.run(
            [
                sys.executable,
                str(VALIDATOR_PATH),
                str(REPO_ROOT / "schema" / "yok.json"),
            ],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )
        self.assertEqual(proc.returncode, 2)
        self.assertIn("FILE ERROR", proc.stderr)


if __name__ == "__main__":
    unittest.main()
