"""send_key_sequence tests (ROADMAP backlog item).

Scope: multi-step shortcuts (e.g. ctrl+a then delete, or a 3+ key chord
split into steps) need one MCP round-trip per key today. `send_key_sequence`
sends a bounded list of {key, modifiers?} steps in a single guarded call,
under the same `key`-class permission/confirmation rules as `send_key`.

Safety pinned here: at most MAX_KEY_SEQUENCE_LENGTH steps
(`InputRejectedError` beyond it), and a mid-sequence failure releases
EVERYTHING pressed so far — no key is ever left held (multiset equality
of down/up events even when the 3rd press explodes).

File layout note: separate from `tests/test_server.py` so the red test
lands without touching existing suites.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from desktop_automation_mcp import errors, server, target

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


def _key_env(*actions):
    return _env_patch(
        DESKTOP_AUTOMATION_ALLOWED_TITLES="*Ruffle*",
        DESKTOP_AUTOMATION_ALLOWED_PROCESS_PATHS=RUFFLE_EXE,
        DESKTOP_AUTOMATION_ALLOWED_ACTIONS=",".join(actions),
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


def _send_patches(tgt):
    return (
        patch.object(server, "_prepare_action_target", return_value=tgt),
        patch.object(server, "_verify_action_target"),
        patch.object(server.time, "sleep"),
    )


class SendKeySequenceTests(unittest.TestCase):
    def test_sends_two_step_shortcut_in_press_order(self):
        tgt = _target()
        calls: list[tuple[int, bool]] = []

        def fake_send_vk(code, *, key_up=False):
            calls.append((code, key_up))

        prepare, verify, sleep = _send_patches(tgt)
        with _key_env("key"):
            with (
                prepare,
                verify,
                sleep,
                patch.object(server, "_send_vk", side_effect=fake_send_vk),
            ):
                result = server.send_key_sequence(
                    keys=[
                        {"key": "a", "modifiers": ["ctrl"]},
                        {"key": "delete"},
                    ],
                    hold_ms=0,
                    hwnd=tgt.hwnd,
                )
        from desktop_automation_mcp.input_ import VK_MAP

        ctrl, a, delete = VK_MAP["ctrl"], VK_MAP["a"], VK_MAP["delete"]
        self.assertEqual(
            calls,
            [
                (ctrl, False),
                (a, False),
                (a, True),
                (ctrl, True),
                (delete, False),
                (delete, True),
            ],
        )
        self.assertIn("2 step", result)
        self.assertIn("correlation_id=", result)

    def test_rejects_overlong_sequence_without_touching_input(self):
        tgt = _target()
        prepare, verify, sleep = _send_patches(tgt)
        too_many = [{"key": "a"}] * (server.MAX_KEY_SEQUENCE_LENGTH + 1)
        with _key_env("key"):
            with prepare, verify, sleep, patch.object(server, "_send_vk") as send_vk:
                with self.assertRaises(errors.InputRejectedError):
                    server.send_key_sequence(keys=too_many, hwnd=tgt.hwnd)
        send_vk.assert_not_called()

    def test_rejects_empty_and_malformed_sequences(self):
        tgt = _target()
        prepare, verify, sleep = _send_patches(tgt)
        bad_sequences = (
            [],
            "ctrl+a",
            [{"modifiers": ["ctrl"]}],
            [{"key": ""}],
            [{"key": "a", "modifiers": "ctrl"}],
            [{"key": "a", "modifiers": ["ctrl", "shift", "alt"]}],
            [{"key": "a", "hold_ms": 5}],
            [{"key": 42}],
            ["a"],
        )
        with _key_env("key"):
            with prepare, verify, sleep, patch.object(server, "_send_vk") as send_vk:
                for bad in bad_sequences:
                    with self.assertRaises(errors.InputRejectedError, msg=repr(bad)):
                        server.send_key_sequence(keys=bad, hwnd=tgt.hwnd)
        send_vk.assert_not_called()

    def test_unknown_key_name_rejected_before_any_input(self):
        tgt = _target()
        prepare, verify, sleep = _send_patches(tgt)
        with _key_env("key"):
            with prepare, verify, sleep, patch.object(server, "_send_vk") as send_vk:
                with self.assertRaises(errors.InputRejectedError):
                    server.send_key_sequence(
                        keys=[{"key": "a"}, {"key": "no-such-key"}],
                        hwnd=tgt.hwnd,
                    )
        send_vk.assert_not_called()

    def test_mid_sequence_failure_releases_everything_pressed(self):
        tgt = _target()
        downs: list[int] = []
        ups: list[int] = []
        state = {"downs": 0}

        def flaky_send_vk(code, *, key_up=False):
            if key_up:
                ups.append(code)
                return None
            state["downs"] += 1
            if state["downs"] == 3:
                raise errors.PlatformError("SendInput refused mid-sequence.")
            downs.append(code)
            return None

        prepare, verify, sleep = _send_patches(tgt)
        with _key_env("key"):
            with (
                prepare,
                verify,
                sleep,
                patch.object(server, "_send_vk", side_effect=flaky_send_vk),
            ):
                with self.assertRaises(errors.PlatformError):
                    server.send_key_sequence(
                        keys=[
                            {"key": "a", "modifiers": ["ctrl"]},
                            {"key": "delete"},
                        ],
                        hold_ms=0,
                        hwnd=tgt.hwnd,
                    )
        # Steps before the explosion were fully released; the exploding
        # press never landed. Multiset equality = nothing held.
        self.assertEqual(sorted(downs), sorted(ups))
        self.assertEqual(len(downs), 2)

    def test_hold_ms_bounds_checked_like_send_key(self):
        tgt = _target()
        prepare, verify, sleep = _send_patches(tgt)
        with _key_env("key"):
            with prepare, verify, sleep, patch.object(server, "_send_vk"):
                with self.assertRaises(errors.InputRejectedError):
                    server.send_key_sequence(
                        keys=[{"key": "a"}], hold_ms=-1, hwnd=tgt.hwnd
                    )
                with self.assertRaises(errors.InputRejectedError):
                    server.send_key_sequence(
                        keys=[{"key": "a"}],
                        hold_ms=server.MAX_HOLD_MS + 1,
                        hwnd=tgt.hwnd,
                    )

    def test_requires_confirmation_when_policy_protects_key(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        quoted = RUFFLE_EXE.replace("\\", "\\\\")
        path = Path(tmp.name) / "key-protected.yaml"
        path.write_text(
            "application:\n"
            f'  executable_path: "{quoted}"\n'
            "  title_patterns:\n"
            "    - *Ruffle*\n"
            "  allowed_actions:\n"
            "    - observe\n"
            "    - key\n"
            "  protected_actions:\n"
            "    - key\n",
            encoding="utf-8",
        )
        tgt = _target()
        prepare, verify, sleep = _send_patches(tgt)
        with _env_patch(DESKTOP_AUTOMATION_POLICY_FILE=str(path)):
            with prepare, verify, sleep, patch.object(server, "_send_vk") as send_vk:
                with self.assertRaises(errors.InputRejectedError):
                    server.send_key_sequence(keys=[{"key": "a"}], hwnd=tgt.hwnd)
                send_vk.assert_not_called()
                token = server.request_confirmation(action_class="key", hwnd=tgt.hwnd)
                result = server.send_key_sequence(
                    keys=[{"key": "a"}],
                    hwnd=tgt.hwnd,
                    confirmation_token=token["token_id"],
                )
        self.assertIn("1 step", result)

    def test_env_mode_without_token_still_allowed(self):
        tgt = _target()
        prepare, verify, sleep = _send_patches(tgt)
        with _key_env("key"):
            with prepare, verify, sleep, patch.object(server, "_send_vk"):
                result = server.send_key_sequence(keys=[{"key": "a"}], hwnd=tgt.hwnd)
        self.assertIn("1 step", result)


if __name__ == "__main__":
    unittest.main()
