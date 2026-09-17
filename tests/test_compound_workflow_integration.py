"""Compound workflow integration test (session features, end to end).

Scope: the ten features added this session were each proven in isolation;
this file proves they work TOGETHER in one realistic operator flow
against the real policy/confirmation/audit machinery (Win32 itself is
faked, as in every other suite — no live windows are touched):

1. a policy file lists `click` under `protected_actions`;
2. `record_coordinate_profile` PRODUCES a profile from the live window;
3. the profile is saved + `resolve_coordinate_profile_point` RESOLVES a
   point from it (save/reload round-trip);
4. a token-less `click_window` on that point is DENIED, then a
   `request_confirmation` token makes the SAME click succeed;
5. `get_audit_events` shows the successful click's audit row — with NO
   `target_identity` leakage in any returned event.

Only real module APIs are used here (signatures verified against
`server.py`/`policy.py`/`confirmation.py` before writing); no code under
`src/` is modified by this file.
"""

import json
import os
import re
import sys
import tempfile
import unittest
from ctypes import wintypes
from pathlib import Path
from unittest.mock import call, patch

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from desktop_automation_mcp import audit, coordinate_profile, errors, server, target
from desktop_automation_mcp import platform_win32, visibility

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


POLICY_YAML = """\
application:
  executable_path: "C:\\\\Program Files\\\\ruffle\\\\bin\\\\ruffle.exe"
  title_patterns:
    - *Ruffle*
  allowed_actions:
    - observe
    - click
  protected_actions:
    - click
"""


class CompoundWorkflowIntegrationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        audit.clear_events()
        self.addCleanup(audit.clear_events)
        server._reset_rate_limits_for_tests()
        self.addCleanup(server._reset_rate_limits_for_tests)
        self._policy_path = str(Path(self._tmp.name) / "compound.yaml")
        Path(self._policy_path).write_text(POLICY_YAML, encoding="utf-8")
        self._env = _env_patch(DESKTOP_AUTOMATION_POLICY_FILE=self._policy_path)
        self._env.start()
        self.addCleanup(self._env.stop)

    def _win32(self, tgt):
        # Fake Windows; real policy/confirmation/audit machinery.
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
            patch.object(platform_win32.user32, "SetCursorPos", return_value=True),
            patch.object(platform_win32.user32, "mouse_event"),
            patch.object(server.time, "sleep"),
        )
        for entered in patches:
            entered.start()
            self.addCleanup(entered.stop)

    def test_protected_click_record_resolve_confirm_audit(self):
        from desktop_automation_mcp import policy

        tgt = _target()
        self._win32(tgt)

        # (1) The file really extends protection to `click` (union).
        self.assertEqual(policy._protected_actions(), {"click"})
        self.assertTrue(server._requires_confirmation("click"))
        self.assertFalse(server._requires_confirmation("observe"))

        # (2) Record a profile from the live window (real validator inside).
        recorded = server.record_coordinate_profile(
            profile_id="compound-v1",
            reference_region_name="stage",
            safe_regions=[
                {"name": "stage", "rect": [10, 10, 60, 60]},
                {"name": "btn", "rect": [10, 10, 30, 30]},
            ],
            verification_points=[[15, 15]],
            hwnd=tgt.hwnd,
        )
        profile = recorded["profile"]
        self.assertEqual(coordinate_profile.validate_profile_document(profile), [])
        self.assertEqual(
            profile["verification_points"][0]["expected_color"], [10, 20, 30]
        )

        # (3) Save/reload round-trip, then resolve the button center.
        profile_path = str(Path(self._tmp.name) / "compound.json")
        Path(profile_path).write_text(json.dumps(profile), encoding="utf-8")
        point = server.resolve_coordinate_profile_point(profile_path, "btn")
        self.assertEqual((point["x"], point["y"]), (20, 20))

        # (4a) Token-less click on the resolved point is denied...
        with self.assertRaises(errors.InputRejectedError):
            server.click_window(hwnd=tgt.hwnd, x=point["x"], y=point["y"])

        # (4b) ...a confirmation token makes the identical click succeed
        # with one button press held through exactly one down/up cycle.
        token = server.request_confirmation(action_class="click", hwnd=tgt.hwnd)
        with patch.object(platform_win32.user32, "mouse_event") as mouse_event:
            result = server.click_window(
                hwnd=tgt.hwnd,
                x=point["x"],
                y=point["y"],
                confirmation_token=token["token_id"],
            )
        move = call(server.MOUSEEVENTF_MOVE, 0, 0, 0, 0)
        down = call(server.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        up = call(server.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
        self.assertEqual(mouse_event.call_args_list, [move, down, up])
        match = re.search(r"correlation_id=([0-9a-f]{12})", result)
        self.assertIsNotNone(match)
        assert match is not None

        # (5) The successful click's audit row is visible — and NO returned
        # event carries the live target-identity triple or window content.
        listed = server.get_audit_events(limit=50)
        self.assertEqual(listed["total_retained"], 4)
        click_rows = [
            event
            for event in listed["events"]
            if event["correlation_id"] == match.group(1)
        ]
        self.assertEqual(len(click_rows), 1)
        self.assertEqual(click_rows[0]["outcome"], "allowed")
        self.assertEqual(click_rows[0]["action_type"], "click")
        for event in listed["events"]:
            self.assertNotIn("target_identity", event)
        dump = json.dumps(listed)
        self.assertNotIn("Ruffle player", dump)
        self.assertNotIn("target_identity", dump)


if __name__ == "__main__":
    unittest.main()
