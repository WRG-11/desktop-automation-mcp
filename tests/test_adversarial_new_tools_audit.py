"""Adversarial review of the six new tools (red-team, no code changes).

Scope: "can I abuse this tool to exfiltrate something I could not
otherwise reach (window title, file path, screen pixels, another app's
data)?" Three scenarios were attacked; ONE real finding resulted:

- F-A1 (REAL, tests below are RED): `record_coordinate_profile` has NO
  upper bound on `safe_regions` / `verification_points` counts — unlike
  its siblings (`MAX_KEY_SEQUENCE_LENGTH=10`, `MAX_DRAG_WAYPOINTS=16`,
  `get_audit_events` limit 1..100). A 100k-region / 100k-point call is
  accepted and burns memory/CPU/response size with no cap. Low severity
  (local, already-authorized caller; no leak) but a genuine missing
  bound. Fix is OUT OF SCOPE for this turn (ROADMAP note only).
- Scenario 1 (CLEAN, locked): every `raise` in `server.py` (+ the denial
  paths in policy/target/visibility/confirmation/rate_limit) was read;
  interpolated values are caller-supplied strings, operator config
  labels, numbers, or hwnd ints — no title/path/pixel ever enters a
  `denial_reason`, so harvesting `get_audit_events(limit=100)` yields no
  window content. Locked by `DenialHygieneTests`.
- Scenario 3 (CLEAN, locked): `get_rate_limit_state`,
  `get_audit_events`, and `get_virtual_screen_bounds` all gate on
  `ACTION_OBSERVE` first — empty policy or click-only policy is denied
  before any reading. Locked by `ObserveGateTests`.

This file changes no code under `src/`.
"""

import json
import os
import sys
import tempfile
import unittest
from ctypes import wintypes
from pathlib import Path
from unittest.mock import patch

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from desktop_automation_mcp import audit, errors, server, target, visibility

RUFFLE_EXE = r"C:\Program Files\ruffle\bin\ruffle.exe"

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
        process_path=RUFFLE_EXE,
        window_class="RuffleWindowClass",
    )


def _file_policy_yaml(**overrides):
    lines = [
        "application:",
        '  executable_path: "C:\\\\Program Files\\\\ruffle\\\\bin\\\\ruffle.exe"',
        "  title_patterns:",
        "    - *Ruffle*",
        "  allowed_actions:",
        "    - observe",
        "    - click",
    ]
    if overrides.get("regions"):
        lines.append("  safe_regions:")
        lines.append("    - name: stage")
        lines.append("      rect: [0, 0, 50, 50]")
    return "\n".join(lines) + "\n"


class RecordBoundsFindingTests(unittest.TestCase):
    """F-A1: no count cap on safe_regions / verification_points (RED)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        audit.clear_events()
        self.addCleanup(audit.clear_events)
        server._reset_rate_limits_for_tests()
        self.addCleanup(server._reset_rate_limits_for_tests)
        path = Path(self._tmp.name) / "adv.yaml"
        path.write_text(_file_policy_yaml(), encoding="utf-8")
        self._env = _env_patch(DESKTOP_AUTOMATION_POLICY_FILE=str(path))
        self._env.start()
        self.addCleanup(self._env.stop)

    def _capture_patches(self, tgt):
        live = Image.new("RGB", (50, 50), (10, 20, 30))
        patches = (
            patch.object(target, "_enum_windows", return_value=[tgt]),
            patch.object(server, "_focus_and_verify"),
            patch.object(server, "_verify_action_target"),
            patch.object(server, "_verify_observable"),
            patch.object(
                server, "_window_rect", return_value=wintypes.RECT(0, 0, 100, 100)
            ),
            patch.object(
                visibility, "_window_rect", return_value=wintypes.RECT(0, 0, 100, 100)
            ),
            patch.object(
                server, "virtual_screen_bounds", return_value=(0, 0, 2000, 2000)
            ),
            patch.object(server.ImageGrab, "grab", return_value=live),
            patch.object(server.time, "sleep"),
        )
        for entered in patches:
            entered.start()
            self.addCleanup(entered.stop)

    def test_absurd_safe_regions_count_rejected(self):
        # Any sane bound (the siblings use 10/16/100) rejects 100k
        # regions; today nothing does and the giant profile is built.
        tgt = _target()
        self._capture_patches(tgt)
        regions = [{"name": "stage", "rect": [10, 10, 60, 60]}]
        regions.extend(
            {"name": f"pad-{i}", "rect": [0, 0, 10, 10]} for i in range(100_000)
        )
        with self.assertRaises(errors.InputRejectedError):
            server.record_coordinate_profile(
                profile_id="adv-huge",
                reference_region_name="stage",
                safe_regions=regions,
                verification_points=[[15, 15]],
                hwnd=tgt.hwnd,
            )

    def test_absurd_verification_points_count_rejected(self):
        # 100k duplicate in-region points: sampling burns CPU with no cap.
        tgt = _target()
        self._capture_patches(tgt)
        with self.assertRaises(errors.InputRejectedError):
            server.record_coordinate_profile(
                profile_id="adv-huge",
                reference_region_name="stage",
                safe_regions=[{"name": "stage", "rect": [10, 10, 60, 60]}],
                verification_points=[[15, 15]] * 100_001,
                hwnd=tgt.hwnd,
            )


class DenialHygieneTests(unittest.TestCase):
    """Scenario 1 lock (GREEN): denial text carries no titles/paths."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        audit.clear_events()
        self.addCleanup(audit.clear_events)
        server._reset_rate_limits_for_tests()
        self.addCleanup(server._reset_rate_limits_for_tests)
        path = Path(self._tmp.name) / "adv.yaml"
        path.write_text(_file_policy_yaml(regions=True), encoding="utf-8")
        self._env = _env_patch(DESKTOP_AUTOMATION_POLICY_FILE=str(path))
        self._env.start()
        self.addCleanup(self._env.stop)

    def test_harvested_denials_contain_no_window_content(self):
        tgt = _target()
        live = Image.new("RGB", (50, 50), (10, 20, 30))
        base = (
            patch.object(target, "_enum_windows", return_value=[tgt]),
            patch.object(server, "_verify_action_target"),
            patch.object(server, "_verify_observable"),
            patch.object(
                server, "_window_rect", return_value=wintypes.RECT(0, 0, 100, 100)
            ),
            patch.object(
                visibility, "_window_rect", return_value=wintypes.RECT(0, 0, 100, 100)
            ),
            patch.object(
                server, "virtual_screen_bounds", return_value=(0, 0, 2000, 2000)
            ),
            patch.object(server.ImageGrab, "grab", return_value=live),
            patch.object(server.time, "sleep"),
        )
        for entered in base:
            entered.start()
            self.addCleanup(entered.stop)
        # (a) unknown region miss (operator labels allowed, content not).
        with self.assertRaises(errors.PolicyDeniedError):
            server.screenshot_window(hwnd=tgt.hwnd, region_name="nope")
        # (b) rate-limit exhaustion (numbers only by the F-4a fix).
        server._reset_rate_limits_for_tests()
        with patch.object(server, "MAX_ACTIONS_PER_MINUTE", 1):
            with patch.object(server, "_resolve_window", return_value=tgt):
                with patch.object(server, "_focus_and_verify"):
                    server._prepare_action_target("click", None, tgt.hwnd)
                    with self.assertRaises(errors.PolicyDeniedError):
                        server._prepare_action_target("click", None, tgt.hwnd)
        # (c) token-less protected action (static text).
        with patch.object(server, "_prepare_action_target", return_value=tgt):
            with self.assertRaises(errors.InputRejectedError):
                server.send_text(hwnd=tgt.hwnd, text="hi")
        # (d) unresolvable hwnd + ghost title search (nothing echoed back).
        with self.assertRaises(ValueError):
            server.click_window(hwnd=9999, x=0, y=0)
        with self.assertRaises(ValueError):
            server.click_window(title_contains="NoSuchWindowZZZ", x=0, y=0)
        # Harvest everything, like an attacker would with limit=100.
        listed = server.get_audit_events(limit=100)
        self.assertGreaterEqual(listed["total_retained"], 5)
        dump = json.dumps(listed)
        self.assertNotIn("Ruffle player", dump)
        self.assertNotIn("NoSuchWindowZZZ", dump)
        self.assertNotIn("C:\\", dump)
        self.assertNotIn(".exe", dump)
        for event in listed["events"]:
            if event["outcome"] != "allowed":
                self.assertTrue(event["denial_reason"].strip())


class ObserveGateTests(unittest.TestCase):
    """Scenario 3 lock (GREEN): debug tools really require `observe`."""

    def test_empty_policy_denies_all_three(self):
        with _env_patch():
            for tool in (
                server.get_rate_limit_state,
                server.get_audit_events,
                server.get_virtual_screen_bounds,
            ):
                with self.assertRaises(errors.PolicyDeniedError):
                    tool()

    def test_click_only_policy_denies_all_three(self):
        with _env_patch(
            DESKTOP_AUTOMATION_ALLOWED_TITLES="*Ruffle*",
            DESKTOP_AUTOMATION_ALLOWED_PROCESS_PATHS=RUFFLE_EXE,
            DESKTOP_AUTOMATION_ALLOWED_ACTIONS="click",
        ):
            with self.assertRaises(errors.PolicyDeniedError):
                server.get_rate_limit_state()
            with self.assertRaises(errors.PolicyDeniedError):
                server.get_audit_events()
            with self.assertRaises(errors.PolicyDeniedError):
                server.get_virtual_screen_bounds()


if __name__ == "__main__":
    unittest.main()
