"""Per-file `protected_actions` enforcement tests (ROADMAP backlog item).

Scope: a policy file that lists `click` under `protected_actions` must make
a token-less `click_window` call fail with `policy_denied`. Before the fix
this protection is a silent no-op: the click succeeds without any token.

File layout note: this file is intentionally separate from
`tests/test_server.py` / `tests/test_policy_file_integration.py` so the red
test lands without touching existing suites.
"""

import os
import sys
import tempfile
import unittest
from ctypes import wintypes
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from desktop_automation_mcp import errors, platform_win32, server, target, visibility

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


def _policy_yaml(exe, titles, actions, protected=None):
    quoted_exe = exe.replace("\\", "\\\\")
    lines = [
        "application:",
        f'  executable_path: "{quoted_exe}"',
        "  title_patterns:",
    ]
    lines.extend(f"    - {t}" for t in titles)
    lines.append("  allowed_actions:")
    lines.extend(f"    - {a}" for a in actions)
    if protected is not None:
        lines.append("  protected_actions:")
        lines.extend(f"    - {a}" for a in protected)
    return "\n".join(lines) + "\n"


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


class PolicyProtectedClickTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.sleep_patch = patch.object(server.time, "sleep")
        self.sleep_patch.start()
        self.addCleanup(self.sleep_patch.stop)
        self.window_rect_patch = patch.object(
            visibility, "_window_rect", return_value=wintypes.RECT(0, 0, 100, 100)
        )
        self.window_rect_patch.start()
        self.addCleanup(self.window_rect_patch.stop)

    def _write(self, name, text):
        path = Path(self._tmp.name) / name
        path.write_text(text, encoding="utf-8")
        return str(path)

    def _click_policy(self):
        return self._write(
            "click-protected.yaml",
            _policy_yaml(
                RUFFLE_EXE,
                ["*Ruffle*"],
                ["observe", "click"],
                protected=["click"],
            ),
        )

    def test_click_without_token_denied_when_policy_protects_click(self):
        path = self._click_policy()
        tgt = _target()
        with (
            _env_patch(DESKTOP_AUTOMATION_POLICY_FILE=path),
            patch.object(server, "_prepare_action_target", return_value=tgt),
            patch.object(server, "_verify_action_target"),
            patch.object(platform_win32.user32, "SetCursorPos", return_value=True),
            patch.object(platform_win32.user32, "mouse_event"),
        ):
            # Same taxonomy as the built-in text/close/drag flow: a missing
            # token is InputRejectedError (code input_rejected), a
            # wrong/used token would be PolicyDeniedError. Both are denials.
            with self.assertRaises(errors.InputRejectedError) as ctx:
                server.click_window(hwnd=tgt.hwnd, x=10, y=10)
        self.assertEqual(ctx.exception.code, errors.INPUT_REJECTED)

    def test_request_confirmation_issues_token_for_policy_protected_click(self):
        path = self._click_policy()
        tgt = _target()
        with (
            _env_patch(DESKTOP_AUTOMATION_POLICY_FILE=path),
            patch.object(server, "_prepare_action_target", return_value=tgt),
            patch.object(server, "_verify_action_target"),
        ):
            token = server.request_confirmation(action_class="click", hwnd=tgt.hwnd)
        self.assertEqual(token["action_class"], "click")
        self.assertTrue(token["token_id"])

    def test_click_with_valid_token_succeeds_when_policy_protects_click(self):
        path = self._click_policy()
        tgt = _target()
        with (
            _env_patch(DESKTOP_AUTOMATION_POLICY_FILE=path),
            patch.object(server, "_prepare_action_target", return_value=tgt),
            patch.object(server, "_verify_action_target"),
            patch.object(platform_win32.user32, "SetCursorPos", return_value=True),
            patch.object(platform_win32.user32, "mouse_event"),
        ):
            token = server.request_confirmation(action_class="click", hwnd=tgt.hwnd)
            result = server.click_window(
                hwnd=tgt.hwnd, x=10, y=10, confirmation_token=token["token_id"]
            )
        self.assertIn("Clicked", result)

    def test_env_mode_click_without_token_still_allowed(self):
        # Guard against over-enforcement: env mode has no per-app protected
        # list, so a plain click keeps working without a token.
        tgt = _target()
        with (
            _env_patch(
                DESKTOP_AUTOMATION_ALLOWED_TITLES="*Ruffle*",
                DESKTOP_AUTOMATION_ALLOWED_PROCESS_PATHS=RUFFLE_EXE,
                DESKTOP_AUTOMATION_ALLOWED_ACTIONS="observe,click",
            ),
            patch.object(server, "_prepare_action_target", return_value=tgt),
            patch.object(server, "_verify_action_target"),
            patch.object(platform_win32.user32, "SetCursorPos", return_value=True),
            patch.object(platform_win32.user32, "mouse_event"),
        ):
            result = server.click_window(hwnd=tgt.hwnd, x=10, y=10)
        self.assertIn("Clicked", result)

    def test_fixed_set_not_loosened_text_still_requires_token(self):
        # Union semantics: a file WITHOUT protected_actions must NOT lift
        # the built-in text/close/drag protection.
        path = self._write(
            "no-protected.yaml",
            _policy_yaml(RUFFLE_EXE, ["*Ruffle*"], ["observe", "click", "text"]),
        )
        tgt = _target()
        with (
            _env_patch(DESKTOP_AUTOMATION_POLICY_FILE=path),
            patch.object(server, "_prepare_action_target", return_value=tgt),
            patch.object(server, "_verify_action_target"),
        ):
            with self.assertRaises(ValueError) as ctx:
                server.send_text(hwnd=tgt.hwnd, text="hi")
        self.assertIn("confirmation_token", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
