"""Read-only health check tests (ROADMAP §10 Phase 4).

Scope: `src/desktop_automation_mcp/health.py`. Policy readers are called
FOR REAL (not mocked); the Win32 DPI query, dependency metadata and the
window-reading prohibitions are proven with mocks.
"""

import ctypes
import os
import sys
import unittest
from importlib.metadata import PackageNotFoundError
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from desktop_automation_mcp import health, platform_win32, server, target, visibility

EXE = r"C:\fake\health-app.exe"


def _patched_env(**overrides):
    """Isolates the policy environment (no DESKTOP_AUTOMATION_* leakage)."""
    base = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("DESKTOP_AUTOMATION_")
    }
    base.update(overrides)
    return patch.dict(os.environ, base, clear=True)


def _configured_env():
    return _patched_env(
        DESKTOP_AUTOMATION_ALLOWED_TITLES="*Ruffle*",
        DESKTOP_AUTOMATION_ALLOWED_PROCESS_PATHS=EXE,
        DESKTOP_AUTOMATION_ALLOWED_ACTIONS="observe",
    )


class PolicySectionTests(unittest.TestCase):
    def test_configured_policy_reports_ok(self):
        with _configured_env():
            report = health.check_health()
        self.assertTrue(report["policy"]["ok"])
        for item in ("titles", "process_paths", "actions"):
            self.assertTrue(report["policy"][item]["ok"])
            self.assertTrue(report["policy"][item]["configured"])

    def test_unconfigured_policy_is_absent_not_broken(self):
        with _patched_env():
            report = health.check_health()
        self.assertFalse(report["policy"]["ok"])
        self.assertFalse(report["policy"]["titles"]["configured"])
        self.assertIn("is not configured", report["policy"]["titles"]["detail"])

    def test_broken_policy_is_configured_but_failing(self):
        with _patched_env(
            DESKTOP_AUTOMATION_ALLOWED_TITLES="*Ruffle*",
            DESKTOP_AUTOMATION_ALLOWED_PROCESS_PATHS=EXE,
            DESKTOP_AUTOMATION_ALLOWED_ACTIONS="observe,teleport",
        ):
            report = health.check_health()
        actions = report["policy"]["actions"]
        self.assertFalse(report["policy"]["ok"])
        self.assertTrue(actions["configured"])
        # NOT "is not configured" — a separate broken-state message. After
        # F-6 (independent security review 2026-09-14) this branch no
        # longer carries the error's FULL text (which may include a file
        # path) to the client — it returns a generic but DISTINGUISHABLE
        # message from "not configured"; the full detail goes to stderr.
        self.assertNotIn("is not configured", actions["detail"])
        self.assertIn("could not be loaded", actions["detail"])


class DependencySectionTests(unittest.TestCase):
    def test_import_failure_is_not_ok(self):
        with patch.object(
            health._dist_metadata,
            "version",
            side_effect=PackageNotFoundError("Pillow"),
        ):
            report = health.check_health()
        self.assertFalse(report["dependencies"]["ok"])
        self.assertFalse(report["dependencies"]["pillow"]["ok"])
        self.assertIn(
            "could not be imported", report["dependencies"]["pillow"]["detail"]
        )

    def test_below_floor_version_is_not_ok(self):
        versions = {"mcp": "1.29.1", "Pillow": "1.0"}
        with patch.object(
            health._dist_metadata, "version", side_effect=lambda name: versions[name]
        ):
            report = health.check_health()
        self.assertFalse(report["dependencies"]["ok"])
        self.assertTrue(report["dependencies"]["mcp"]["ok"])
        self.assertIn("is below the floor", report["dependencies"]["pillow"]["detail"])

    def test_mcp_v2_is_not_ok_when_the_server_requires_the_v1_api(self):
        versions = {"mcp": "2.0.0", "Pillow": "12.3.0"}
        with patch.object(
            health._dist_metadata, "version", side_effect=lambda name: versions[name]
        ):
            report = health.check_health()
        self.assertFalse(report["dependencies"]["ok"])
        self.assertFalse(report["dependencies"]["mcp"]["ok"])
        self.assertIn("outside the ceiling", report["dependencies"]["mcp"]["detail"])

    def test_version_compare_treats_missing_patch_as_zero(self):
        self.assertTrue(health._version_gte("12.3", "12.3.0"))
        self.assertTrue(health._version_gte("12.3.0", "12.3"))
        self.assertFalse(health._version_gte("12.2.9", "12.3"))
        self.assertTrue(health._version_gte("1.29.1", "1.29.1"))


class DpiSectionTests(unittest.TestCase):
    def test_query_uses_the_real_win32_call(self):
        calls = []

        def fake_query(_handle, out_param):
            ctypes.cast(out_param, ctypes.POINTER(ctypes.c_int)).contents.value = 2
            calls.append(True)
            return 0

        with patch.object(
            platform_win32._shcore, "GetProcessDpiAwareness", side_effect=fake_query
        ):
            report = health.check_health()
        self.assertEqual(len(calls), 1)
        self.assertTrue(report["dpi_awareness"]["ok"])
        self.assertEqual(report["dpi_awareness"]["value"], 2)

    def test_failed_hresult_is_not_ok(self):
        with patch.object(
            platform_win32._shcore, "GetProcessDpiAwareness", return_value=5
        ):
            report = health.check_health()
        self.assertFalse(report["dpi_awareness"]["ok"])
        self.assertIsNone(report["dpi_awareness"]["value"])

    def test_missing_shcore_is_not_ok(self):
        with patch.object(platform_win32, "_shcore", None):
            report = health.check_health()
        self.assertFalse(report["dpi_awareness"]["ok"])


class NoScreenContentTests(unittest.TestCase):
    def test_check_health_touches_no_window_reading_function(self):
        spies = [
            patch.object(target, "_enum_windows"),
            patch.object(target, "_current_window_snapshot"),
            patch.object(target, "_window_rect"),
            patch.object(visibility, "_verify_action_target"),
            patch.object(visibility, "_is_visibly_on_top"),
            patch.object(platform_win32.user32, "GetWindowTextW"),
            patch.object(server, "list_windows"),
            patch.object(server, "screenshot_window"),
        ]
        started = []
        for spy in spies:
            started.append(spy.start())
            self.addCleanup(spy.stop)
        with _configured_env():
            report = health.check_health()
        for mock in started:
            mock.assert_not_called()
        # The returned dict has no content key/value (status info only):
        payload = repr(sorted(report.keys())) + repr(report["dependencies"])
        for forbidden in ("hwnd", "title", "screenshot", "png", "Ruffle"):
            self.assertNotIn(forbidden, payload)


if __name__ == "__main__":
    unittest.main()
