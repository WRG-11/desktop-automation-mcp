import base64
import ctypes
from ctypes import wintypes
from dataclasses import FrozenInstanceError
import io
import inspect
import os
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, call, patch

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from desktop_automation_mcp import (
    input_ as input_mod,
)
from desktop_automation_mcp import (
    audit,
    errors,
    memory_budget,
    platform_win32,
    policy,
    server,
    target,
    visibility,
)


def _target(
    *,
    hwnd: int = 42,
    title: str = "Ruffle player",
    pid: int = 1,
    rect: tuple[int, int, int, int] = (0, 0, 100, 100),
    process_started_at: int = 1,
    process_path: str = "",
    window_class: str = "",
) -> target.TargetSnapshot:
    return target.TargetSnapshot(
        hwnd=hwnd,
        title=title,
        pid=pid,
        rect=rect,
        process_started_at=process_started_at,
        process_path=process_path,
        window_class=window_class,
    )


class TargetSnapshotTests(unittest.TestCase):
    def test_snapshot_is_immutable_and_preserves_public_contract(self):
        snapshot = target.TargetSnapshot(
            hwnd=42,
            title="Ruffle player",
            pid=1,
            rect=(0, 0, 100, 100),
            process_path=r"c:\program files\ruffle\bin\ruffle.exe",
            window_class="RuffleWindowClass",
        )
        self.assertEqual(
            snapshot.as_public_dict(),
            {
                "hwnd": 42,
                "title": "Ruffle player",
                "pid": 1,
                "rect": [0, 0, 100, 100],
                "process_path": r"c:\program files\ruffle\bin\ruffle.exe",
                "window_class": "RuffleWindowClass",
            },
        )
        with self.assertRaises(FrozenInstanceError):
            snapshot.title = "other"  # type: ignore[misc]

    def test_snapshot_defaults_process_path_and_window_class_to_empty(self):
        snapshot = target.TargetSnapshot(
            hwnd=42, title="Ruffle player", pid=1, rect=(0, 0, 100, 100)
        )
        self.assertEqual(snapshot.process_path, "")
        self.assertEqual(snapshot.window_class, "")


class TargetPolicyTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(
            os.environ,
            {
                "DESKTOP_AUTOMATION_ALLOWED_TITLES": "*Ruffle*,Notepad",
                "DESKTOP_AUTOMATION_ALLOWED_PROCESS_PATHS": (
                    r"C:\Program Files\ruffle\bin\ruffle.exe;"
                    r"C:\Windows\System32\notepad.exe"
                ),
                "DESKTOP_AUTOMATION_ALLOWED_ACTIONS": "observe,hover,click,text,key,close",
            },
            clear=False,
        )
        self.environment.start()

    def tearDown(self):
        self.environment.stop()

    def test_allowlist_is_case_insensitive_and_does_not_match_unrelated_titles(self):
        self.assertTrue(policy._is_allowed_title("RUFFLE player"))
        self.assertTrue(policy._is_allowed_title("Notepad"))
        self.assertFalse(policy._is_allowed_title("Password Manager"))

    def test_policy_fails_closed_when_missing(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(PermissionError):
                policy._allowed_title_patterns()
            with self.assertRaises(PermissionError):
                policy._allowed_process_paths()

    def test_window_requires_both_title_and_process_policy(self):
        with patch.object(
            target,
            "_process_path",
            return_value=policy._normalise_process_path(
                r"C:\Program Files\ruffle\bin\ruffle.exe"
            ),
        ):
            self.assertTrue(target._is_allowed_window("Ruffle player", 123))
        with patch.object(
            target,
            "_process_path",
            return_value=policy._normalise_process_path(r"C:\Temp\ruffle.exe"),
        ):
            self.assertFalse(target._is_allowed_window("Ruffle player", 123))

    def test_windows_security_boundary_hosts_are_denied_even_if_allowlisted(self):
        for process_path in (
            r"C:\Windows\System32\consent.exe",
            r"C:\Windows\System32\LogonUI.exe",
            r"C:\Windows\SystemApps\Microsoft.LockApp\LockApp.exe",
        ):
            with patch.object(
                target,
                "_process_path",
                return_value=policy._normalise_process_path(process_path),
            ):
                with patch.object(
                    target,
                    "_allowed_process_paths",
                    return_value={target._process_path(123)},
                ):
                    self.assertFalse(
                        target._is_allowed_window("Windows security", 123),
                        msg=process_path,
                    )

    def test_actions_fail_closed_and_reject_unknown_names(self):
        with patch.dict(
            os.environ, {"DESKTOP_AUTOMATION_ALLOWED_ACTIONS": "observe"}, clear=False
        ):
            policy._require_action(policy.ACTION_OBSERVE)
            with self.assertRaises(PermissionError):
                policy._require_action(policy.ACTION_CLOSE)
        with patch.dict(
            os.environ,
            {"DESKTOP_AUTOMATION_ALLOWED_ACTIONS": "observe,teleport"},
            clear=False,
        ):
            with self.assertRaises(PermissionError):
                policy._allowed_actions()

    def test_actions_fail_closed_when_not_configured(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(PermissionError):
                policy._require_action(policy.ACTION_OBSERVE)
            with self.assertRaises(PermissionError):
                server.list_windows()

    def test_handle_resolution_requires_current_allowed_window(self):
        windows = [_target()]
        with patch.object(target, "_enum_windows", return_value=windows):
            self.assertEqual(target._resolve_window(None, 42).title, "Ruffle player")
            with self.assertRaises(ValueError):
                target._resolve_window(None, 99)

    def test_title_resolution_rejects_ambiguous_matches(self):
        windows = [
            _target(hwnd=1, title="Ruffle A"),
            _target(hwnd=2, title="Ruffle B", pid=2),
        ]
        with patch.object(target, "_enum_windows", return_value=windows):
            with self.assertRaises(ValueError):
                target._resolve_window("Ruffle", None)


class RateLimitTests(unittest.TestCase):
    """ROADMAP §9 closing item: per-application action/screenshot/text
    rate limit."""

    def setUp(self):
        server._reset_rate_limits_for_tests()
        self.addCleanup(server._reset_rate_limits_for_tests)

    def test_action_rate_limit_blocks_the_call_over_budget(self):
        tgt = _target()
        with (
            patch.dict(os.environ, {"DESKTOP_AUTOMATION_ALLOWED_ACTIONS": "observe"}),
            patch.object(server, "MAX_ACTIONS_PER_MINUTE", 2),
            patch.object(target, "_enum_windows", return_value=[tgt]),
            patch.object(server, "_focus_and_verify"),
            patch.object(server, "_verify_action_target"),
        ):
            server._prepare_action_target(policy.ACTION_OBSERVE, None, tgt.hwnd)
            server._prepare_action_target(policy.ACTION_OBSERVE, None, tgt.hwnd)
            with self.assertRaises(errors.PolicyDeniedError) as ctx:
                server._prepare_action_target(policy.ACTION_OBSERVE, None, tgt.hwnd)
        self.assertEqual(ctx.exception.code, errors.POLICY_DENIED)

    def test_action_rate_limit_is_isolated_per_process_path(self):
        tgt_a = _target(hwnd=1, process_path="a.exe")
        tgt_b = _target(hwnd=2, process_path="b.exe")
        with (
            patch.dict(os.environ, {"DESKTOP_AUTOMATION_ALLOWED_ACTIONS": "observe"}),
            patch.object(server, "MAX_ACTIONS_PER_MINUTE", 1),
            patch.object(server, "_focus_and_verify"),
            patch.object(server, "_verify_action_target"),
        ):
            with patch.object(target, "_enum_windows", return_value=[tgt_a]):
                server._prepare_action_target(policy.ACTION_OBSERVE, None, tgt_a.hwnd)
            # A different application's budget must be untouched by A's usage.
            with patch.object(target, "_enum_windows", return_value=[tgt_b]):
                server._prepare_action_target(policy.ACTION_OBSERVE, None, tgt_b.hwnd)

    def test_text_char_rate_limit_blocks_an_oversized_send(self):
        tgt = _target(title="Notepad")
        with (
            patch.dict(os.environ, {"DESKTOP_AUTOMATION_ALLOWED_ACTIONS": "text"}),
            patch.object(server, "MAX_TEXT_CHARS_PER_MINUTE", 5),
            patch.object(server, "_prepare_action_target", return_value=tgt),
        ):
            with self.assertRaises(PermissionError):
                server.send_text(hwnd=42, text="too long", confirmation_token="tok-1")

    def test_screenshot_pixel_rate_limit_blocks_an_oversized_capture(self):
        tgt = _target(rect=(0, 0, 1000, 1000))
        with (
            patch.dict(os.environ, {"DESKTOP_AUTOMATION_ALLOWED_ACTIONS": "observe"}),
            patch.object(server, "MAX_SCREENSHOT_PIXELS_PER_MINUTE", 10),
            patch.object(server, "_prepare_action_target", return_value=tgt),
            patch.object(
                server, "_window_rect", return_value=wintypes.RECT(0, 0, 1000, 1000)
            ),
        ):
            with self.assertRaises(PermissionError):
                server.screenshot_window(
                    hwnd=42, crop_left=0, crop_top=0, crop_right=0, crop_bottom=0
                )


class AuditEmissionTests(unittest.TestCase):
    """ROADMAP §9: `_prepare_action_target` records every attempt
    (success/deny/error) as a redacted audit event."""

    def setUp(self):
        audit.clear_events()
        server._reset_rate_limits_for_tests()
        self.addCleanup(audit.clear_events)
        self.addCleanup(server._reset_rate_limits_for_tests)

    def test_successful_resolution_records_an_allowed_event_with_target_identity(self):
        tgt = _target()
        with (
            patch.dict(os.environ, {"DESKTOP_AUTOMATION_ALLOWED_ACTIONS": "observe"}),
            patch.object(target, "_enum_windows", return_value=[tgt]),
            patch.object(server, "_focus_and_verify"),
            patch.object(server, "_verify_action_target"),
        ):
            server._prepare_action_target(policy.ACTION_OBSERVE, None, tgt.hwnd)
        events = audit.read_events()
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event["outcome"], audit.OUTCOME_ALLOWED)
        self.assertEqual(event["action_type"], policy.ACTION_OBSERVE)
        self.assertEqual(event["target_identity"], server._target_identity(tgt))
        self.assertNotIn("denial_reason", event)
        self.assertIn("policy_id", event)
        self.assertIn("correlation_id", event)
        self.assertGreaterEqual(event["duration_ms"], 0)

    def test_a_broken_audit_backend_never_masks_the_real_denial(self):
        # Chaos scenario: "audit write failure" (ROADMAP Phase 4). If
        # `audit.record_event` itself raises, the ORIGINAL security
        # decision (here, a policy denial) must still be what the caller
        # sees — not an audit-internal exception replacing it.
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(
                server,
                "_record_audit_event",
                side_effect=RuntimeError("disk full (simulated)"),
            ),
        ):
            with self.assertRaises(PermissionError):
                server._prepare_action_target(policy.ACTION_OBSERVE, None, 42)

    def test_policy_denial_records_a_denied_event_with_reason_and_no_target(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(PermissionError):
                server._prepare_action_target(policy.ACTION_OBSERVE, None, 42)
        events = audit.read_events()
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event["outcome"], audit.OUTCOME_DENIED)
        self.assertTrue(event["denial_reason"])
        self.assertNotIn("target_identity", event)

    def test_stale_target_is_recorded_as_denied_not_error(self):
        tgt = _target()
        with (
            patch.dict(os.environ, {"DESKTOP_AUTOMATION_ALLOWED_ACTIONS": "observe"}),
            patch.object(target, "_enum_windows", return_value=[tgt]),
            patch.object(server, "_focus_and_verify"),
            patch.object(
                server,
                "_verify_action_target",
                side_effect=errors.TargetStaleError("could not verify target identity"),
            ),
        ):
            with self.assertRaises(errors.TargetStaleError):
                server._prepare_action_target(policy.ACTION_OBSERVE, None, tgt.hwnd)
        events = audit.read_events()
        self.assertEqual(events[0]["outcome"], audit.OUTCOME_DENIED)

    def test_unexpected_platform_failure_is_recorded_as_error(self):
        tgt = _target()
        with (
            patch.dict(os.environ, {"DESKTOP_AUTOMATION_ALLOWED_ACTIONS": "observe"}),
            patch.object(target, "_enum_windows", return_value=[tgt]),
            patch.object(
                server,
                "_focus_and_verify",
                side_effect=errors.PlatformError("SetForegroundWindow failed"),
            ),
        ):
            with self.assertRaises(errors.PlatformError):
                server._prepare_action_target(policy.ACTION_OBSERVE, None, tgt.hwnd)
        events = audit.read_events()
        self.assertEqual(events[0]["outcome"], audit.OUTCOME_ERROR)

    def test_click_effect_failure_is_audited_as_error_not_allowed(self):
        """Public tool outcome, not just its pre-effect gate, owns the audit result."""
        tgt = _target(rect=(0, 0, 100, 100))
        with (
            patch.dict(os.environ, {"DESKTOP_AUTOMATION_ALLOWED_ACTIONS": "click"}),
            patch.object(target, "_enum_windows", return_value=[tgt]),
            patch.object(server, "_focus_and_verify"),
            patch.object(server, "_verify_action_target"),
            patch.object(
                visibility, "_window_rect", return_value=wintypes.RECT(0, 0, 100, 100)
            ),
            patch.object(platform_win32.user32, "SetCursorPos", return_value=False),
        ):
            with self.assertRaises(errors.PlatformError):
                server.click_window(hwnd=tgt.hwnd, x=10, y=10)

        events = audit.read_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["outcome"], audit.OUTCOME_ERROR)
        self.assertNotIn("target_identity", events[0])

    def test_click_result_and_audit_share_one_correlation_id(self):
        tgt = _target(rect=(0, 0, 100, 100))
        with (
            patch.dict(os.environ, {"DESKTOP_AUTOMATION_ALLOWED_ACTIONS": "click"}),
            patch.object(target, "_enum_windows", return_value=[tgt]),
            patch.object(server, "_focus_and_verify"),
            patch.object(server, "_verify_action_target"),
            patch.object(
                visibility, "_window_rect", return_value=wintypes.RECT(0, 0, 100, 100)
            ),
            patch.object(platform_win32.user32, "SetCursorPos", return_value=True),
            patch.object(platform_win32.user32, "mouse_event"),
            patch.object(server.time, "sleep"),
        ):
            result = server.click_window(hwnd=tgt.hwnd, x=10, y=10)

        result_correlation_id = result.rsplit("correlation_id=", 1)[1].rstrip(".")
        events = audit.read_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["outcome"], audit.OUTCOME_ALLOWED)
        self.assertEqual(events[0]["correlation_id"], result_correlation_id)

    def test_confirmation_rejection_is_audited_as_denied_not_allowed(self):
        tgt = _target(title="Notepad")
        with (
            patch.dict(os.environ, {"DESKTOP_AUTOMATION_ALLOWED_ACTIONS": "text"}),
            patch.object(target, "_enum_windows", return_value=[tgt]),
            patch.object(server, "_focus_and_verify"),
            patch.object(server, "_verify_action_target"),
            patch.object(
                server,
                "consume_confirmation",
                side_effect=errors.PolicyDeniedError(
                    "confirmation token already consumed (single_use violation)."
                ),
            ),
        ):
            with self.assertRaises(errors.PolicyDeniedError) as ctx:
                server.send_text(
                    hwnd=tgt.hwnd,
                    text="safe test",
                    confirmation_token="tok-used",
                )

        self.assertEqual(ctx.exception.code, errors.POLICY_DENIED)

        events = audit.read_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["outcome"], audit.OUTCOME_DENIED)
        self.assertNotIn("target_identity", events[0])
        self.assertNotIn("tok-used", events[0]["denial_reason"])

    def test_screenshot_metadata_and_audit_share_one_correlation_id(self):
        tgt = _target(rect=(0, 0, 100, 100))
        fake_image = MagicMock()
        with (
            patch.dict(
                os.environ,
                {
                    "DESKTOP_AUTOMATION_ALLOWED_ACTIONS": "observe",
                    "DESKTOP_AUTOMATION_ALLOW_UNRESTRICTED_SCREENSHOTS": "true",
                },
            ),
            patch.object(target, "_enum_windows", return_value=[tgt]),
            patch.object(server, "_focus_and_verify"),
            patch.object(server, "_verify_action_target"),
            patch.object(server, "_verify_observable"),
            patch.object(
                server, "_window_rect", return_value=wintypes.RECT(0, 0, 100, 100)
            ),
            patch.object(server.ImageGrab, "grab", return_value=fake_image),
        ):
            result = server.screenshot_window(
                hwnd=tgt.hwnd,
                crop_left=0,
                crop_top=0,
                crop_right=0,
                crop_bottom=0,
            )

        events = audit.read_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["outcome"], audit.OUTCOME_ALLOWED)
        self.assertIsNotNone(result.meta)
        self.assertEqual(events[0]["correlation_id"], result.meta["correlation_id"])
        self.assertEqual(events[0]["operation"], "screenshot_window")
        self.assertEqual(
            events[0]["privacy_context"],
            {
                "region_limited": False,
                "mask_count": 0,
                "pixel_area": 10_000,
                "png_bytes": 0,
            },
        )

    def test_policy_id_reflects_the_active_policy_file_without_leaking_the_full_path(
        self,
    ):
        # F-4b (independent security review 2026-09-14): the full path is
        # no longer WRITTEN to the audit — only basename + a short hash.
        with patch.dict(
            os.environ, {"DESKTOP_AUTOMATION_POLICY_FILE": r"C:\Users\op\p.yaml"}
        ):
            policy_id = server._policy_id()
        self.assertTrue(policy_id.startswith("file:p.yaml#"))
        self.assertNotIn("Users", policy_id)
        self.assertNotIn("op", policy_id.split("#")[0])
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(server._policy_id(), "env")

    def test_policy_id_distinguishes_same_basename_in_different_directories(self):
        with patch.dict(os.environ, {"DESKTOP_AUTOMATION_POLICY_FILE": r"C:\a\p.yaml"}):
            first = server._policy_id()
        with patch.dict(os.environ, {"DESKTOP_AUTOMATION_POLICY_FILE": r"C:\b\p.yaml"}):
            second = server._policy_id()
        self.assertNotEqual(first, second)


class GuardedActionArchitectureTests(unittest.TestCase):
    def test_every_targeted_effect_tool_uses_the_complete_guarded_executor(self):
        effect_tools = (
            server.check_coordinate_profile,
            server.screenshot_window,
            server.request_confirmation,
            server.click_window,
            server.double_click_window,
            server.right_click_window,
            server.hover_window,
            server.scroll_window,
            server.drag_window,
            server.send_text,
            server.send_key,
            server.close_window,
        )
        bypasses = [
            tool.__name__
            for tool in effect_tools
            if "_execute_guarded_action(" not in inspect.getsource(tool)
        ]
        self.assertEqual(bypasses, [])


class ConfirmationWiringTests(unittest.TestCase):
    """ROADMAP §9: the close/text protected classes do not work without a
    real confirmation token (`confirmation.py` is mocked here; the real
    flow is proven in `tests/test_confirmation.py`)."""

    def test_close_window_requires_a_confirmation_token(self):
        tgt = _target(title="Notepad")
        with (
            patch.dict(os.environ, {"DESKTOP_AUTOMATION_ALLOWED_ACTIONS": "close"}),
            patch.object(server, "_prepare_action_target", return_value=tgt),
        ):
            with self.assertRaises(errors.InputRejectedError):
                server.close_window(hwnd=42)

    def test_close_window_consumes_the_token_before_posting_wm_close(self):
        tgt = _target(title="Notepad")
        with (
            patch.dict(os.environ, {"DESKTOP_AUTOMATION_ALLOWED_ACTIONS": "close"}),
            patch.object(server, "_prepare_action_target", return_value=tgt),
            patch.object(server, "consume_confirmation") as consume,
            patch.object(server, "_verify_action_target"),
            patch.object(
                platform_win32.user32, "PostMessageW", return_value=True
            ) as post_message,
        ):
            server.close_window(hwnd=42, confirmation_token="tok-1")
        consume.assert_called_once_with(
            "tok-1", server._target_identity(tgt), server.ACTION_CLOSE
        )
        post_message.assert_called_once_with(42, server.WM_CLOSE, 0, 0)

    def test_close_window_never_posts_wm_close_if_confirmation_is_rejected(self):
        tgt = _target(title="Notepad")
        with (
            patch.dict(os.environ, {"DESKTOP_AUTOMATION_ALLOWED_ACTIONS": "close"}),
            patch.object(server, "_prepare_action_target", return_value=tgt),
            patch.object(
                server,
                "consume_confirmation",
                side_effect=PermissionError("token already consumed"),
            ),
            patch.object(platform_win32.user32, "PostMessageW") as post_message,
        ):
            with self.assertRaises(PermissionError):
                server.close_window(hwnd=42, confirmation_token="tok-1")
        post_message.assert_not_called()

    def test_close_window_verifies_action_target_immediately_before_wm_close(self):
        """Every other guarded effect (click/hover/scroll/drag/send_text)
        re-verifies identity/foreground/occlusion immediately before its
        raw Win32 call. close_window skipped this — a stale HWND (window
        closed and reused by another process between token-consume and the
        PostMessageW call) could receive WM_CLOSE with no TargetStaleError."""
        tgt = _target(title="Notepad")
        call_order: list[str] = []
        with (
            patch.dict(os.environ, {"DESKTOP_AUTOMATION_ALLOWED_ACTIONS": "close"}),
            patch.object(server, "_prepare_action_target", return_value=tgt),
            patch.object(
                server,
                "consume_confirmation",
                side_effect=lambda *a: call_order.append("consume"),
            ),
            patch.object(
                server,
                "_verify_action_target",
                side_effect=lambda *a, **kw: call_order.append("verify"),
            ) as verify,
            patch.object(
                platform_win32.user32,
                "PostMessageW",
                side_effect=lambda *a: call_order.append("post_message") or True,
            ),
        ):
            server.close_window(hwnd=42, confirmation_token="tok-1")
        verify.assert_called_once_with(tgt)
        # Order matters: verify must happen AFTER the token is consumed
        # and BEFORE the WM_CLOSE is posted (the whole point of the fix).
        self.assertEqual(call_order, ["consume", "verify", "post_message"])

    def test_drag_window_requires_a_confirmation_token(self):
        tgt = _target(title="Notepad", rect=(0, 0, 100, 100))
        with (
            patch.dict(os.environ, {"DESKTOP_AUTOMATION_ALLOWED_ACTIONS": "drag"}),
            patch.object(server, "_prepare_action_target", return_value=tgt),
        ):
            with self.assertRaises(errors.InputRejectedError):
                server.drag_window(hwnd=42, start_x=0, start_y=0, end_x=10, end_y=10)

    def test_drag_window_consumes_the_token_before_moving_the_cursor(self):
        tgt = _target(title="Notepad", rect=(0, 0, 100, 100))
        with (
            patch.dict(os.environ, {"DESKTOP_AUTOMATION_ALLOWED_ACTIONS": "drag"}),
            patch.object(server, "_prepare_action_target", return_value=tgt),
            patch.object(server, "consume_confirmation") as consume,
            patch.object(
                visibility, "_window_rect", return_value=wintypes.RECT(0, 0, 100, 100)
            ),
            patch.object(server, "_verify_action_target"),
            patch.object(platform_win32.user32, "SetCursorPos", return_value=True),
            patch.object(platform_win32.user32, "mouse_event"),
            patch.object(server.time, "sleep"),
        ):
            server.drag_window(
                hwnd=42,
                start_x=0,
                start_y=0,
                end_x=10,
                end_y=10,
                confirmation_token="tok-1",
            )
        consume.assert_called_once_with(
            "tok-1", server._target_identity(tgt), server.ACTION_DRAG
        )

    def test_request_confirmation_binds_the_token_to_the_resolved_target(self):
        tgt = _target(title="Notepad")
        with (
            patch.dict(os.environ, {"DESKTOP_AUTOMATION_ALLOWED_ACTIONS": "close"}),
            patch.object(server, "_prepare_action_target", return_value=tgt),
            patch.object(
                server, "issue_confirmation", return_value={"token_id": "tok-1"}
            ) as issue,
        ):
            result = server.request_confirmation("close", hwnd=42, ttl_seconds=30)
        issue.assert_called_once_with(server._target_identity(tgt), "close", 30)
        self.assertEqual(result["token_id"], "tok-1")
        self.assertIn("correlation_id", result)

    def test_preview_action_never_calls_issue_or_consume(self):
        tgt = _target(title="Notepad")
        with (
            patch.dict(os.environ, {"DESKTOP_AUTOMATION_ALLOWED_ACTIONS": "observe"}),
            patch.object(server, "_resolve_window", return_value=tgt),
            patch.object(server, "issue_confirmation") as issue,
            patch.object(server, "consume_confirmation") as consume,
            patch.object(
                server,
                "_preview_action",
                return_value={"action_class": "close", "requires_confirmation": True},
            ),
        ):
            result = server.preview_action("close", title_contains="Ruffle")
        issue.assert_not_called()
        consume.assert_not_called()
        self.assertTrue(result["requires_confirmation"])


class PerformanceBudgetTests(unittest.TestCase):
    """ROADMAP Phase 4: slowness is observed, no control is SKIPPED/RELAXED."""

    def test_check_budget_warns_only_when_exceeded(self):
        with patch("sys.stderr", new_callable=io.StringIO) as fake_err:
            server._check_budget("test-phase", 50, 100)
        self.assertEqual(fake_err.getvalue(), "")

        with patch("sys.stderr", new_callable=io.StringIO) as fake_err:
            server._check_budget("test-phase", 150, 100)
        self.assertIn("test-phase", fake_err.getvalue())
        self.assertIn("150ms > 100ms", fake_err.getvalue())

    def test_slow_target_resolution_is_observed_but_still_allowed(self):
        # A slow resolve must never be treated as a security failure — the
        # action still succeeds, only a warning is printed.
        tgt = _target()

        def slow_resolve(_title, _hwnd):
            time.sleep(0.01)
            return tgt

        with (
            patch.dict(os.environ, {"DESKTOP_AUTOMATION_ALLOWED_ACTIONS": "observe"}),
            patch.object(server, "BUDGET_RESOLVE_MS", 0),
            patch.object(server, "_resolve_window", side_effect=slow_resolve),
            patch.object(server, "_focus_and_verify"),
            patch.object(server, "_verify_action_target"),
            patch("sys.stderr", new_callable=io.StringIO) as fake_err,
        ):
            result = server._prepare_action_target(policy.ACTION_OBSERVE, None, 42)
        self.assertIs(result, tgt)
        self.assertIn("target resolution", fake_err.getvalue())

    def test_slow_window_listing_is_observed_but_still_returned(self):
        def slow_enumerate():
            time.sleep(0.01)
            return []

        with (
            patch.dict(os.environ, {"DESKTOP_AUTOMATION_ALLOWED_ACTIONS": "observe"}),
            patch.object(server, "BUDGET_LIST_WINDOWS_MS", 0),
            patch.object(server, "_enum_windows", side_effect=slow_enumerate),
            patch("sys.stderr", new_callable=io.StringIO) as fake_err,
        ):
            self.assertEqual(server.list_windows(), [])
        self.assertIn("window listing", fake_err.getvalue())

    def test_screenshot_memory_budget_env_reader_accepts_bounds_and_falls_back(self):
        name = "DESKTOP_AUTOMATION_SCREENSHOT_MEMORY_BUDGET_BYTES"
        with patch.dict(os.environ, {name: "1048576"}, clear=False):
            self.assertEqual(
                server._env_bounded_int(
                    name, 128 * 1024 * 1024, minimum=1024 * 1024, maximum=2**31
                ),
                1024 * 1024,
            )
        with (
            patch.dict(os.environ, {name: "1048575"}, clear=False),
            patch("sys.stderr", new_callable=io.StringIO) as fake_err,
        ):
            self.assertEqual(
                server._env_bounded_int(
                    name, 128 * 1024 * 1024, minimum=1024 * 1024, maximum=2**31
                ),
                128 * 1024 * 1024,
            )
        self.assertIn("default", fake_err.getvalue())


class ErrorTaxonomyTests(unittest.TestCase):
    """ROADMAP Phase 4: every denial belongs to a class carrying a name + code + message."""

    def test_policy_denial_carries_the_stable_policy_denied_code(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(errors.PolicyDeniedError) as ctx:
                policy._allowed_title_patterns()
        self.assertIsInstance(ctx.exception, PermissionError)
        self.assertEqual(ctx.exception.code, errors.POLICY_DENIED)

    def test_verify_action_target_distinguishes_stale_foreground_and_occluded(self):
        tgt = _target()
        recycled = _target(title="Other Ruffle player", pid=2)
        with (
            patch.object(visibility, "_current_window_snapshot", return_value=recycled),
            patch.object(platform_win32.user32, "GetForegroundWindow", return_value=42),
            patch.object(visibility, "_is_visibly_on_top", return_value=True),
        ):
            with self.assertRaises(errors.TargetStaleError) as ctx:
                visibility._verify_action_target(tgt)
        self.assertEqual(ctx.exception.code, errors.TARGET_STALE)

        with (
            patch.object(visibility, "_current_window_snapshot", return_value=tgt),
            patch.object(
                platform_win32.user32, "GetForegroundWindow", return_value=999
            ),
            patch.object(visibility, "_is_visibly_on_top", return_value=True),
        ):
            with self.assertRaises(errors.TargetNotForegroundError) as ctx:
                visibility._verify_action_target(tgt)
        self.assertEqual(ctx.exception.code, errors.TARGET_NOT_FOREGROUND)

        with (
            patch.object(visibility, "_current_window_snapshot", return_value=tgt),
            patch.object(
                platform_win32.user32, "GetForegroundWindow", return_value=tgt.hwnd
            ),
            patch.object(visibility, "_is_visibly_on_top", return_value=False),
        ):
            with self.assertRaises(errors.TargetOccludedError) as ctx:
                visibility._verify_action_target(tgt)
        self.assertEqual(ctx.exception.code, errors.TARGET_OCCLUDED)

    def test_passive_focus_rejection_carries_the_not_foreground_code(self):
        with patch.dict(os.environ, {"DESKTOP_AUTOMATION_FOCUS_MODE": "passive"}):
            with (
                patch.object(
                    platform_win32.user32, "GetForegroundWindow", return_value=999
                ),
            ):
                with self.assertRaises(errors.TargetNotForegroundError) as ctx:
                    visibility._focus_and_verify(42)
        self.assertEqual(ctx.exception.code, errors.TARGET_NOT_FOREGROUND)

    def test_bad_coordinate_carries_the_input_rejected_code(self):
        rect = wintypes.RECT(0, 0, 100, 100)
        with patch.object(visibility, "_window_rect", return_value=rect):
            with self.assertRaises(errors.InputRejectedError) as ctx:
                visibility._absolute_point(42, -1, 0)
        self.assertEqual(ctx.exception.code, errors.INPUT_REJECTED)

    def test_unknown_key_name_carries_the_input_rejected_code(self):
        with self.assertRaises(errors.InputRejectedError) as ctx:
            input_mod._vk_code("not-a-real-key")
        self.assertEqual(ctx.exception.code, errors.INPUT_REJECTED)

    def test_sendinput_failure_carries_the_platform_error_code(self):
        with patch.object(platform_win32.user32, "SendInput", return_value=0):
            with self.assertRaises(errors.PlatformError) as ctx:
                input_mod._send_vk(0x41)
        self.assertEqual(ctx.exception.code, errors.PLATFORM_ERROR)

    def test_wait_for_window_timeout_carries_the_timeout_code(self):
        with (
            patch.dict(os.environ, {"DESKTOP_AUTOMATION_ALLOWED_ACTIONS": "observe"}),
            patch.object(
                server,
                "_resolve_window",
                side_effect=ValueError(server._NOT_FOUND_MESSAGE),
            ),
            patch.object(server.time, "sleep"),
            patch.object(server.time, "monotonic", side_effect=[0.0, 0.0, 0.2]),
        ):
            with self.assertRaises(errors.ActionTimeoutError) as ctx:
                server.wait_for_window("Ruffle", timeout_ms=100, poll_interval_ms=50)
        self.assertEqual(ctx.exception.code, errors.TIMEOUT)

    def test_out_of_range_scroll_notches_carries_the_input_rejected_code(self):
        with self.assertRaises(errors.InputRejectedError) as ctx:
            server.scroll_window(hwnd=42, notches=0)
        self.assertEqual(ctx.exception.code, errors.INPUT_REJECTED)


class TargetIdentityEnrichmentTests(unittest.TestCase):
    """ROADMAP Phase 1: Target now carries the full process path + window class."""

    def test_window_class_reads_the_real_class_name(self):
        def fake_get_class_name(_hwnd, buffer, _size):
            buffer.value = "RuffleWindowClass"
            return len("RuffleWindowClass")

        with patch.object(
            platform_win32.user32, "GetClassNameW", side_effect=fake_get_class_name
        ):
            self.assertEqual(target._window_class(42), "RuffleWindowClass")

    def test_window_class_raises_when_win32_reports_failure(self):
        with patch.object(platform_win32.user32, "GetClassNameW", return_value=0):
            with self.assertRaises(OSError):
                target._window_class(42)


class FocusModeOptInTests(unittest.TestCase):
    """ROADMAP Phase 0.1.4: `activate` only engages via explicit opt-in."""

    def test_unset_focus_mode_defaults_to_passive(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(policy._focus_mode(), policy.FOCUS_MODE_PASSIVE)

    def test_unrecognized_focus_mode_value_fails_closed_not_silently_active(self):
        # A typo or a truthy-looking value ("true", "1", "aktif") must NOT be
        # silently treated as activate - that would defeat the opt-in
        # contract. It must raise, never fall through to either mode.
        for bad_value in ("true", "1", "aktif", "Activated"):
            with patch.dict(os.environ, {"DESKTOP_AUTOMATION_FOCUS_MODE": bad_value}):
                with self.assertRaises(ValueError):
                    policy._focus_mode()

    def test_activate_requires_the_exact_literal_value(self):
        with patch.dict(os.environ, {"DESKTOP_AUTOMATION_FOCUS_MODE": "activate"}):
            self.assertEqual(policy._focus_mode(), policy.FOCUS_MODE_ACTIVATE)
        with patch.dict(os.environ, {"DESKTOP_AUTOMATION_FOCUS_MODE": "  ACTIVATE  "}):
            self.assertEqual(policy._focus_mode(), policy.FOCUS_MODE_ACTIVATE)


class MouseActionTests(unittest.TestCase):
    """Faz 1: double_click/right_click/scroll/drag, hepsi guaranteed-release."""

    def setUp(self):
        self.actions = patch.dict(
            os.environ,
            {"DESKTOP_AUTOMATION_ALLOWED_ACTIONS": "click,hover,drag"},
        )
        self.actions.start()
        self.addCleanup(self.actions.stop)
        self.sleep_patch = patch.object(server.time, "sleep")
        self.sleep_patch.start()
        self.addCleanup(self.sleep_patch.stop)
        # Every test below targets a window at rect (0, 0, 100, 100); patching
        # the real GetWindowRect call once here (rather than per test) keeps
        # `_absolute_point`'s bounds-checking logic itself under real test,
        # only the Win32 syscall it depends on is faked.
        self.window_rect_patch = patch.object(
            visibility, "_window_rect", return_value=wintypes.RECT(0, 0, 100, 100)
        )
        self.window_rect_patch.start()
        self.addCleanup(self.window_rect_patch.stop)

    def test_click_window_presses_and_releases_the_left_button(self):
        tgt = _target(rect=(0, 0, 100, 100))
        with (
            patch.object(server, "_prepare_action_target", return_value=tgt),
            patch.object(server, "_verify_action_target"),
            patch.object(
                platform_win32.user32, "SetCursorPos", return_value=True
            ) as set_pos,
            patch.object(platform_win32.user32, "mouse_event") as mouse_event,
        ):
            server.click_window(hwnd=tgt.hwnd, x=10, y=10)
        set_pos.assert_called_once_with(10, 10)
        mouse_event.assert_has_calls(
            [
                call(server.MOUSEEVENTF_MOVE, 0, 0, 0, 0),
                call(server.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0),
                call(server.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0),
            ]
        )

    def test_click_window_still_releases_the_button_if_verify_fails_after_down(self):
        tgt = _target(rect=(0, 0, 100, 100))
        with (
            patch.object(server, "_prepare_action_target", return_value=tgt),
            patch.object(
                server,
                "_verify_action_target",
                side_effect=[None, RuntimeError("target could not be verified")],
            ),
            patch.object(platform_win32.user32, "SetCursorPos", return_value=True),
            patch.object(platform_win32.user32, "mouse_event") as mouse_event,
        ):
            with self.assertRaises(RuntimeError):
                server.click_window(hwnd=tgt.hwnd, x=10, y=10)
        mouse_event.assert_called_once_with(server.MOUSEEVENTF_MOVE, 0, 0, 0, 0)

    def test_double_click_window_sends_two_press_release_cycles(self):
        tgt = _target(rect=(0, 0, 100, 100))
        with (
            patch.object(server, "_prepare_action_target", return_value=tgt),
            patch.object(server, "_verify_action_target"),
            patch.object(platform_win32.user32, "SetCursorPos", return_value=True),
            patch.object(platform_win32.user32, "mouse_event") as mouse_event,
        ):
            server.double_click_window(hwnd=tgt.hwnd, x=5, y=5)
        down_up = [
            call(server.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0),
            call(server.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0),
        ]
        mouse_event.assert_has_calls(down_up + down_up)

    def test_right_click_window_uses_right_button_flags(self):
        tgt = _target(rect=(0, 0, 100, 100))
        with (
            patch.object(server, "_prepare_action_target", return_value=tgt),
            patch.object(server, "_verify_action_target"),
            patch.object(platform_win32.user32, "SetCursorPos", return_value=True),
            patch.object(platform_win32.user32, "mouse_event") as mouse_event,
        ):
            server.right_click_window(hwnd=tgt.hwnd, x=5, y=5)
        mouse_event.assert_any_call(server.MOUSEEVENTF_RIGHTDOWN, 0, 0, 0, 0)
        mouse_event.assert_any_call(server.MOUSEEVENTF_RIGHTUP, 0, 0, 0, 0)

    def test_scroll_window_sends_wheel_delta_scaled_by_notches(self):
        tgt = _target(rect=(0, 0, 100, 100))
        with (
            patch.object(server, "_prepare_action_target", return_value=tgt),
            patch.object(server, "_verify_action_target"),
            patch.object(platform_win32.user32, "SetCursorPos", return_value=True),
            patch.object(platform_win32.user32, "mouse_event") as mouse_event,
        ):
            server.scroll_window(hwnd=tgt.hwnd, x=5, y=5, notches=-2)
        mouse_event.assert_any_call(
            server.MOUSEEVENTF_WHEEL, 0, 0, -2 * server.WHEEL_DELTA, 0
        )

    def test_scroll_window_rejects_zero_and_out_of_bounds_notches(self):
        with self.assertRaises(ValueError):
            server.scroll_window(hwnd=42, notches=0)
        with self.assertRaises(ValueError):
            server.scroll_window(hwnd=42, notches=server.MAX_SCROLL_NOTCHES + 1)
        with self.assertRaises(ValueError):
            server.scroll_window(hwnd=42, notches=-server.MAX_SCROLL_NOTCHES - 1)

    def test_drag_window_checks_start_and_end_bounds_independently(self):
        tgt = _target(rect=(0, 0, 100, 100))
        with (
            patch.object(server, "_prepare_action_target", return_value=tgt),
            patch.object(server, "consume_confirmation"),
            patch.object(server, "_verify_action_target"),
            patch.object(platform_win32.user32, "SetCursorPos", return_value=True),
            patch.object(platform_win32.user32, "mouse_event"),
        ):
            with self.assertRaises(ValueError):
                server.drag_window(
                    hwnd=tgt.hwnd,
                    start_x=-1,
                    start_y=0,
                    end_x=10,
                    end_y=10,
                    confirmation_token="tok-1",
                )
            with self.assertRaises(ValueError):
                server.drag_window(
                    hwnd=tgt.hwnd,
                    start_x=0,
                    start_y=0,
                    end_x=999,
                    end_y=10,
                    confirmation_token="tok-1",
                )

    def test_drag_window_releases_the_button_if_verify_fails_mid_drag(self):
        tgt = _target(rect=(0, 0, 100, 100))
        with (
            patch.object(server, "_prepare_action_target", return_value=tgt),
            patch.object(server, "consume_confirmation"),
            patch.object(
                server,
                "_verify_action_target",
                # 1st: before moving to start (ok) - 2nd: right before
                # LEFTDOWN (ok, button goes down) - 3rd: before moving to
                # end (fails) - the button must still be released here.
                side_effect=[None, None, RuntimeError("target could not be verified")],
            ),
            patch.object(platform_win32.user32, "SetCursorPos", return_value=True),
            patch.object(platform_win32.user32, "mouse_event") as mouse_event,
        ):
            with self.assertRaises(RuntimeError):
                server.drag_window(
                    hwnd=tgt.hwnd,
                    start_x=0,
                    start_y=0,
                    end_x=50,
                    end_y=50,
                    confirmation_token="tok-1",
                )
        mouse_event.assert_has_calls(
            [
                call(server.MOUSEEVENTF_MOVE, 0, 0, 0, 0),
                call(server.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0),
                call(server.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0),
            ]
        )

    def test_drag_window_verifies_target_again_before_releasing_the_button(self):
        """After moving to the end point, drag_window sleeps 150ms+ before
        releasing the mouse button. If the target closes, is occluded, or
        another window appears at those screen coordinates during that
        window, the existing code still fires LEFTUP unconditionally with
        no re-verification — unlike every verify-then-act step earlier in
        the same function. This locks a 4th _verify_action_target call,
        immediately before the release."""
        tgt = _target(rect=(0, 0, 100, 100))
        with (
            patch.object(server, "_prepare_action_target", return_value=tgt),
            patch.object(server, "consume_confirmation"),
            patch.object(server, "_verify_action_target") as verify,
            patch.object(platform_win32.user32, "SetCursorPos", return_value=True),
            patch.object(platform_win32.user32, "mouse_event"),
        ):
            server.drag_window(
                hwnd=tgt.hwnd,
                start_x=0,
                start_y=0,
                end_x=50,
                end_y=50,
                confirmation_token="tok-1",
            )
        # Existing 3 calls (before start move, before LEFTDOWN, before end
        # move) plus one more immediately before LEFTUP.
        self.assertEqual(verify.call_count, 4)

    def test_drag_window_moves_to_both_points_and_releases_on_success(self):
        tgt = _target(rect=(0, 0, 100, 100))
        with (
            patch.object(server, "_prepare_action_target", return_value=tgt),
            patch.object(server, "consume_confirmation"),
            patch.object(server, "_verify_action_target"),
            patch.object(
                platform_win32.user32, "SetCursorPos", return_value=True
            ) as set_pos,
            patch.object(platform_win32.user32, "mouse_event") as mouse_event,
        ):
            server.drag_window(
                hwnd=tgt.hwnd,
                start_x=0,
                start_y=0,
                end_x=50,
                end_y=50,
                confirmation_token="tok-1",
            )
        set_pos.assert_has_calls([call(0, 0), call(50, 50)])
        mouse_event.assert_any_call(server.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)

    def test_drag_requires_its_own_action_permission_separate_from_click(self):
        with patch.dict(os.environ, {"DESKTOP_AUTOMATION_ALLOWED_ACTIONS": "click"}):
            with self.assertRaises(PermissionError):
                server.drag_window(
                    hwnd=42,
                    start_x=0,
                    start_y=0,
                    end_x=1,
                    end_y=1,
                    confirmation_token="tok-1",
                )


class CoordinateValidationTests(unittest.TestCase):
    def test_rejects_coordinates_outside_the_window(self):
        rect = wintypes.RECT(10, 20, 110, 220)
        with patch.object(visibility, "_window_rect", return_value=rect):
            self.assertEqual(visibility._absolute_point(42, 0, 0), (10, 20))
            self.assertEqual(visibility._absolute_point(42, 99, 199), (109, 219))
            with self.assertRaises(ValueError):
                visibility._absolute_point(42, -1, 0)
            with self.assertRaises(ValueError):
                visibility._absolute_point(42, 100, 0)
            with self.assertRaises(ValueError):
                visibility._absolute_point(42, 0, 200)


class TargetRevalidationTests(unittest.TestCase):
    def test_diagnostic_policy_rejection_has_stable_code_and_never_leaks_title(self):
        api = MagicMock()
        api.IsWindow.return_value = True
        api.GetWindowTextLengthW.return_value = len("private window title")

        def set_title(_hwnd, buffer, _size):
            buffer.value = "private window title"
            return len(buffer.value)

        def set_pid(_hwnd, pid_pointer):
            ctypes.cast(pid_pointer, ctypes.POINTER(wintypes.DWORD)).contents.value = 99
            return 1

        api.GetWindowTextW.side_effect = set_title
        api.GetWindowThreadProcessId.side_effect = set_pid
        with (
            patch.object(target, "_user32", return_value=api),
            patch.object(target, "_is_allowed_window", return_value=False),
        ):
            with self.assertRaises(errors.PolicyDeniedError) as ctx:
                target._window_diagnostic_state(42)

        self.assertEqual(ctx.exception.code, errors.POLICY_DENIED)
        self.assertNotIn("private window title", str(ctx.exception))

    def test_prepare_action_target_uses_the_shared_pre_effect_gate(self):
        tgt = _target()
        with (
            patch.object(server, "_require_action") as require_action,
            patch.object(server, "_resolve_window", return_value=tgt) as resolve,
            patch.object(server, "_focus_and_verify") as focus,
            patch.object(server, "_verify_action_target") as verify,
        ):
            self.assertIs(
                server._prepare_action_target(policy.ACTION_CLICK, None, tgt.hwnd),
                tgt,
            )

        require_action.assert_called_once_with(policy.ACTION_CLICK)
        resolve.assert_called_once_with(None, tgt.hwnd)
        focus.assert_called_once_with(tgt.hwnd)
        verify.assert_called_once_with(tgt, require_foreground=True)

    def test_default_focus_mode_does_not_take_foreground_control(self):
        with patch.dict(os.environ, {"DESKTOP_AUTOMATION_FOCUS_MODE": "passive"}):
            with (
                patch.object(platform_win32.user32, "ShowWindow") as show_window,
                patch.object(
                    platform_win32.user32, "SetForegroundWindow"
                ) as set_foreground,
                patch.object(
                    platform_win32.user32, "GetForegroundWindow", return_value=42
                ),
                patch.object(visibility, "_is_visibly_on_top", return_value=True),
                patch.object(visibility.time, "sleep") as sleep,
            ):
                visibility._focus_and_verify(42)
        show_window.assert_not_called()
        set_foreground.assert_not_called()
        sleep.assert_not_called()

    def test_activate_failure_uses_the_bounded_retry_contract(self):
        """A failed activation must retry exactly as long as the documented cap.

        The real Windows smoke for a disabled, visible temporary window proves
        the negative path reaches this contract.  This deterministic test
        protects the cap against an accidental unbounded retry regression.
        """
        with patch.dict(os.environ, {"DESKTOP_AUTOMATION_FOCUS_MODE": "activate"}):
            with (
                patch.object(platform_win32.user32, "ShowWindow") as show_window,
                patch.object(
                    platform_win32.user32, "SetForegroundWindow"
                ) as set_foreground,
                patch.object(
                    platform_win32.user32, "GetForegroundWindow", return_value=999
                ),
                patch.object(visibility.time, "sleep") as sleep,
            ):
                with self.assertRaises(errors.TargetNotForegroundError):
                    visibility._focus_and_verify(42, retries=3, delay_s=0.05)

        show_window.assert_called_once_with(42, visibility.SW_RESTORE)
        self.assertEqual(set_foreground.call_count, 3)
        self.assertEqual(sleep.call_count, 3)
        sleep.assert_called_with(0.05)

    def test_revalidation_does_not_rescan_every_window(self):
        tgt = _target()
        with (
            patch.object(visibility, "_current_window_snapshot", return_value=tgt),
            patch.object(
                target,
                "_enum_windows",
                side_effect=AssertionError(
                    "action revalidation must not enumerate every window"
                ),
            ),
            patch.object(platform_win32.user32, "GetForegroundWindow", return_value=42),
            patch.object(visibility, "_is_visibly_on_top", return_value=True),
        ):
            visibility._verify_action_target(tgt)

    def test_rejects_recycled_handle_with_different_identity(self):
        tgt = _target()
        recycled = _target(title="Other Ruffle player", pid=2)
        with (
            patch.object(visibility, "_current_window_snapshot", return_value=recycled),
            patch.object(platform_win32.user32, "GetForegroundWindow", return_value=42),
            patch.object(visibility, "_is_visibly_on_top", return_value=True),
        ):
            with self.assertRaises(RuntimeError):
                visibility._verify_action_target(tgt)

    def test_rejects_reused_pid_with_a_new_process_generation(self):
        tgt = _target(process_started_at=100)
        recycled = _target(process_started_at=200)
        with (
            patch.object(visibility, "_current_window_snapshot", return_value=recycled),
            patch.object(platform_win32.user32, "GetForegroundWindow", return_value=42),
            patch.object(visibility, "_is_visibly_on_top", return_value=True),
        ):
            with self.assertRaises(RuntimeError):
                visibility._verify_action_target(tgt)

    def test_accepts_a_self_inflicted_title_change_from_the_same_process(self):
        # A target app rewriting its own title (e.g. Notepad's "*" unsaved
        # marker) mid-send_text must not read as an identity swap when the
        # PID and process generation are unchanged.
        tgt = _target(title="Untitled, Not Defteri")
        retitled = _target(title="*Untitled, Not Defteri")
        with (
            patch.object(visibility, "_current_window_snapshot", return_value=retitled),
            patch.object(platform_win32.user32, "GetForegroundWindow", return_value=42),
            patch.object(visibility, "_is_visibly_on_top", return_value=True),
        ):
            visibility._verify_action_target(tgt)

    def test_rejects_when_the_renamed_title_escapes_policy(self):
        # If a rename takes the window outside the configured title allowlist,
        # `_current_window_snapshot` (via `_is_allowed_window`) returns None
        # regardless of PID/generation, and the gate must still reject.
        tgt = _target()
        with (
            patch.object(visibility, "_current_window_snapshot", return_value=None),
            patch.object(platform_win32.user32, "GetForegroundWindow", return_value=42),
            patch.object(visibility, "_is_visibly_on_top", return_value=True),
        ):
            with self.assertRaises(RuntimeError):
                visibility._verify_action_target(tgt)


class UnicodeInputTests(unittest.TestCase):
    def test_default_send_text_uses_unicode_sendinput_path(self):
        tgt = _target(title="Notepad")
        with (
            patch.dict(os.environ, {"DESKTOP_AUTOMATION_ALLOWED_ACTIONS": "text"}),
            patch.object(server, "_prepare_action_target", return_value=tgt),
            patch.object(server, "consume_confirmation") as consume,
            patch.object(server, "_send_unicode_text") as send_unicode,
        ):
            result = server.send_text(
                hwnd=42, text="Merhaba", confirmation_token="tok-1"
            )
        consume.assert_called_once_with(
            "tok-1", server._target_identity(tgt), server.ACTION_TEXT
        )
        send_unicode.assert_called_once_with(tgt, "Merhaba")
        self.assertIn("visibility in the target control was not verified", result)

    def test_send_text_requires_a_confirmation_token(self):
        tgt = _target(title="Notepad")
        with (
            patch.dict(os.environ, {"DESKTOP_AUTOMATION_ALLOWED_ACTIONS": "text"}),
            patch.object(server, "_prepare_action_target", return_value=tgt),
        ):
            with self.assertRaises(errors.InputRejectedError):
                server.send_text(hwnd=42, text="Merhaba")

    def test_send_text_propagates_confirmation_rejection(self):
        tgt = _target(title="Notepad")
        with (
            patch.dict(os.environ, {"DESKTOP_AUTOMATION_ALLOWED_ACTIONS": "text"}),
            patch.object(server, "_prepare_action_target", return_value=tgt),
            patch.object(
                server,
                "consume_confirmation",
                side_effect=PermissionError("token has expired"),
            ),
            patch.object(server, "_send_unicode_text") as send_unicode,
        ):
            with self.assertRaises(PermissionError):
                server.send_text(hwnd=42, text="Merhaba", confirmation_token="tok-1")
        send_unicode.assert_not_called()

    def test_utf16_units_preserve_astral_characters(self):
        self.assertEqual(input_mod._utf16_code_units("A😀"), [0x0041, 0xD83D, 0xDE00])

    def test_unicode_input_uses_utf16_scan_code_not_virtual_key(self):
        observed: list[tuple[int, int, int]] = []

        def capture_input(_count, input_pointer, _input_size):
            inp = ctypes.cast(input_pointer, ctypes.POINTER(input_mod._Input)).contents
            observed.append((inp.ii.ki.wVk, inp.ii.ki.wScan, inp.ii.ki.dwFlags))
            return 1

        with patch.object(
            platform_win32.user32, "SendInput", side_effect=capture_input
        ):
            input_mod._send_unicode_unit(0x00E7)
            input_mod._send_unicode_unit(0x00E7, key_up=True)

        self.assertEqual(
            observed,
            [
                (0, 0x00E7, input_mod.KEYEVENTF_UNICODE),
                (0, 0x00E7, input_mod.KEYEVENTF_UNICODE | input_mod.KEYEVENTF_KEYUP),
            ],
        )

    def test_wm_char_is_available_only_as_explicit_compatibility_mode(self):
        tgt = _target(title="Notepad")
        with (
            patch.dict(
                os.environ,
                {
                    "DESKTOP_AUTOMATION_ALLOWED_ACTIONS": "text",
                    "DESKTOP_AUTOMATION_TEXT_MODE": "wm_char",
                },
            ),
            patch.object(server, "_resolve_window", return_value=tgt),
            patch.object(server, "_focus_and_verify"),
            patch.object(server, "_verify_action_target"),
            patch.object(server, "consume_confirmation"),
            patch.object(
                platform_win32.user32, "PostMessageW", return_value=True
            ) as post_message,
        ):
            server.send_text(hwnd=42, text="A", confirmation_token="tok-1")
        post_message.assert_called_once_with(42, server.WM_CHAR, ord("A"), 0)

    def test_unicode_keyup_is_sent_when_intercharacter_wait_fails(self):
        tgt = _target(title="Notepad", rect=(0, 0, 1, 1))
        with (
            patch.object(input_mod, "_verify_action_target"),
            patch.object(input_mod, "_send_unicode_unit") as send_unit,
            patch.object(
                input_mod.time, "sleep", side_effect=RuntimeError("interrupted")
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "interrupted"):
                input_mod._send_unicode_text(tgt, "A")
        send_unit.assert_has_calls([call(0x0041), call(0x0041, key_up=True)])


class Win32StructureTests(unittest.TestCase):
    def test_input_structure_matches_the_running_architecture(self):
        expected_size = 40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28
        self.assertEqual(ctypes.sizeof(input_mod._Input), expected_size)


class InputDeliveryContractTests(unittest.TestCase):
    def test_key_result_does_not_claim_target_behavior_was_verified(self):
        tgt = _target(title="Notepad")
        with (
            patch.dict(os.environ, {"DESKTOP_AUTOMATION_ALLOWED_ACTIONS": "key"}),
            patch.object(server, "_resolve_window", return_value=tgt),
            patch.object(server, "_focus_and_verify"),
            patch.object(server, "_verify_action_target"),
            patch.object(server, "_send_vk"),
            patch.object(server.time, "sleep"),
        ):
            result = server.send_key(hwnd=42, key="enter")
        self.assertIn("input events sent", result)
        self.assertIn("not verified", result)


class ScreenshotPrivacyTests(unittest.TestCase):
    def _patch_target(self, image, constraints):
        tgt = _target(rect=(0, 0, 100, 100))
        return (
            patch.object(server, "_prepare_action_target", return_value=tgt),
            patch.object(
                server, "_window_rect", return_value=wintypes.RECT(0, 0, 100, 100)
            ),
            patch.object(server, "_verify_action_target"),
            patch.object(server, "_screenshot_constraints", return_value=constraints),
            patch.object(server.ImageGrab, "grab", return_value=image),
        )

    def test_named_region_is_required_when_policy_defines_safe_regions(self):
        image = MagicMock()
        constraints = {
            "safe_regions": [{"name": "content", "rect": [10, 20, 70, 80]}],
            "screenshot_masks": [],
            "max_screenshot_bytes": 1_000_000,
        }
        patches = self._patch_target(image, constraints)
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            with self.assertRaises(errors.PolicyDeniedError):
                server.screenshot_window(hwnd=42)
        image.save.assert_not_called()

    def test_dpi_unaware_process_is_rejected_before_pixel_grab(self):
        image = MagicMock()
        constraints = {
            "safe_regions": None,
            "screenshot_masks": [],
            "max_screenshot_bytes": 1_000_000,
        }
        patches = self._patch_target(image, constraints)
        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4] as grab,
            patch.object(
                platform_win32,
                "_process_dpi_awareness",
                return_value=(platform_win32.PROCESS_DPI_UNAWARE, ""),
            ),
        ):
            with self.assertRaisesRegex(errors.PlatformError, "DPI"):
                server.screenshot_window(hwnd=42)
        grab.assert_not_called()

    def test_boolean_crop_is_rejected_as_input_not_an_integer_offset(self):
        image = MagicMock()
        constraints = {
            "safe_regions": None,
            "screenshot_masks": [],
            "max_screenshot_bytes": 1_000_000,
        }
        patches = self._patch_target(image, constraints)
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            with self.assertRaises(errors.InputRejectedError):
                server.screenshot_window(hwnd=42, crop_left=True)
        image.save.assert_not_called()

    def test_named_region_is_captured_and_policy_masks_are_blacked_out(self):
        image = Image.new("RGBA", (60, 60), (255, 255, 255, 255))
        constraints = {
            "safe_regions": [{"name": "content", "rect": [10, 20, 70, 80]}],
            "screenshot_masks": [{"rect": [20, 30, 30, 40]}],
            "max_screenshot_bytes": 1_000_000,
        }
        patches = self._patch_target(image, constraints)
        with patches[0], patches[1], patches[2], patches[3], patches[4] as grab:
            result = server.screenshot_window(hwnd=42, region_name="content")

        grab.assert_called_once_with(bbox=(10, 20, 70, 80), all_screens=True)
        decoded = Image.open(io.BytesIO(base64.b64decode(result.data)))
        self.assertEqual(decoded.getpixel((0, 0)), (255, 255, 255, 255))
        self.assertEqual(decoded.getpixel((10, 10)), (0, 0, 0, 255))

    def test_grid_overlay_is_off_by_default_and_leaves_pixels_unchanged(self):
        """Session dogfooding finding: picking a click target from a
        screenshot required visually guessing pixel coordinates. Grid
        overlay is opt-in so it never changes the default capture."""
        image = Image.new("RGBA", (60, 60), (255, 255, 255, 255))
        constraints = {
            "safe_regions": None,
            "screenshot_masks": [],
            "max_screenshot_bytes": 1_000_000,
        }
        patches = self._patch_target(image, constraints)
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            result = server.screenshot_window(
                hwnd=42, crop_left=0, crop_top=0, crop_right=0, crop_bottom=0
            )
        decoded = Image.open(io.BytesIO(base64.b64decode(result.data)))
        for x, y in ((0, 0), (20, 0), (0, 20), (40, 40), (59, 59)):
            self.assertEqual(decoded.getpixel((x, y)), (255, 255, 255, 255))

    def test_grid_overlay_draws_lines_at_the_configured_spacing(self):
        image = Image.new("RGBA", (60, 60), (255, 255, 255, 255))
        constraints = {
            "safe_regions": None,
            "screenshot_masks": [],
            "max_screenshot_bytes": 1_000_000,
        }
        patches = self._patch_target(image, constraints)
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            result = server.screenshot_window(
                hwnd=42,
                crop_left=0,
                crop_top=0,
                crop_right=0,
                crop_bottom=0,
                grid=True,
                grid_spacing=20,
            )
        decoded = Image.open(io.BytesIO(base64.b64decode(result.data)))
        # A vertical line at x=20 and a horizontal line at y=20 must exist;
        # a point far from any line or axis label (e.g. (30, 30), roughly
        # centered between the x=20/x=40 and y=20/y=40 gridlines) must
        # remain untouched white.
        self.assertNotEqual(decoded.getpixel((20, 30)), (255, 255, 255, 255))
        self.assertNotEqual(decoded.getpixel((30, 20)), (255, 255, 255, 255))
        self.assertEqual(decoded.getpixel((30, 30)), (255, 255, 255, 255))

    def test_grid_spacing_must_be_a_positive_integer(self):
        image = Image.new("RGBA", (60, 60), (255, 255, 255, 255))
        constraints = {
            "safe_regions": None,
            "screenshot_masks": [],
            "max_screenshot_bytes": 1_000_000,
        }
        patches = self._patch_target(image, constraints)
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            with self.assertRaises(errors.InputRejectedError):
                server.screenshot_window(hwnd=42, grid=True, grid_spacing=0)
            with self.assertRaises(errors.InputRejectedError):
                server.screenshot_window(hwnd=42, grid=True, grid_spacing=-5)
            with self.assertRaises(errors.InputRejectedError):
                server.screenshot_window(hwnd=42, grid=True, grid_spacing=True)

    def test_named_region_bbox_preserves_negative_screen_origins(self):
        constraints = {
            "safe_regions": [{"name": "content", "rect": [10, 20, 70, 80]}],
            "screenshot_masks": [],
            "max_screenshot_bytes": 1_000_000,
        }
        cases = (
            (100, (-1920, 0, -920, 800), (-1910, 20, -1850, 80)),
            (125, (-2560, -200, -1310, 800), (-2550, -180, -2490, -120)),
            (150, (-3840, -300, -2340, 900), (-3830, -280, -3770, -220)),
        )
        for scale_percent, raw_rect, expected_bbox in cases:
            with self.subTest(scale_percent=scale_percent):
                tgt = _target(rect=raw_rect)
                with (
                    patch.object(
                        server, "_window_rect", return_value=wintypes.RECT(*raw_rect)
                    ),
                    patch.object(
                        server, "_screenshot_constraints", return_value=constraints
                    ),
                    patch.object(
                        server,
                        "virtual_screen_bounds",
                        return_value=(-4000, -400, 3000, 2000),
                    ),
                    patch.object(server._screenshot_rate_limiter, "consume"),
                ):
                    plan = server._build_screenshot_plan(
                        tgt,
                        region_name="content",
                        crop_left=0,
                        crop_top=0,
                        crop_right=0,
                        crop_bottom=0,
                    )
                self.assertEqual(plan["bbox"], expected_bbox)

    def test_region_outside_virtual_screen_is_rejected_without_clamp(self):
        constraints = {
            "safe_regions": [{"name": "content", "rect": [0, 0, 60, 60]}],
            "screenshot_masks": [],
            "max_screenshot_bytes": 1_000_000,
        }
        target_snapshot = _target(rect=(90, 20, 190, 120))
        with (
            patch.object(
                server, "_window_rect", return_value=wintypes.RECT(90, 20, 190, 120)
            ),
            patch.object(server, "_screenshot_constraints", return_value=constraints),
            patch.object(
                server, "virtual_screen_bounds", return_value=(0, 0, 100, 100)
            ),
            patch.object(server._screenshot_rate_limiter, "consume") as consume,
        ):
            with self.assertRaisesRegex(errors.PolicyDeniedError, "clamp"):
                server._build_screenshot_plan(
                    target_snapshot,
                    region_name="content",
                    crop_left=0,
                    crop_top=0,
                    crop_right=0,
                    crop_bottom=0,
                )
        consume.assert_not_called()

    def test_png_larger_than_policy_byte_cap_is_rejected(self):
        image = MagicMock()
        image.save.side_effect = lambda stream, format: stream.write(b"x" * 2_000)
        constraints = {
            "safe_regions": None,
            "screenshot_masks": [],
            "max_screenshot_bytes": 1_024,
        }
        patches = self._patch_target(image, constraints)
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            with self.assertRaisesRegex(errors.PolicyDeniedError, "byte"):
                server.screenshot_window(
                    hwnd=42,
                    crop_left=0,
                    crop_top=0,
                    crop_right=0,
                    crop_bottom=0,
                )

    def test_in_flight_memory_budget_rejects_before_pixel_grab(self):
        image = MagicMock()
        constraints = {
            "safe_regions": None,
            "screenshot_masks": [],
            "max_screenshot_bytes": 1_024,
        }
        patches = self._patch_target(image, constraints)
        tiny_budget = memory_budget.InFlightMemoryBudget(1_000)
        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4] as grab,
            patch.object(server, "_screenshot_memory_budget", tiny_budget),
        ):
            with self.assertRaisesRegex(errors.PolicyDeniedError, "memory budget"):
                server.screenshot_window(
                    hwnd=42,
                    crop_left=0,
                    crop_top=0,
                    crop_right=0,
                    crop_bottom=0,
                )
        grab.assert_not_called()


class CoordinateProfileCapturePrivacyTests(unittest.TestCase):
    @staticmethod
    def _profile(process_path=r"C:\Apps\Ruffle\ruffle.exe"):
        return {
            "profile_id": "ruffle-100-v2",
            "application": {"process_path": process_path},
            "window_size": {"width": 100, "height": 100},
            "reference_region_name": "content",
            "reference_image_hash": "a" * 64,
            "safe_regions": [{"name": "content", "rect": [10, 20, 70, 80]}],
            "verification_points": [
                {"point": [20, 30], "expected_color": [255, 255, 255]}
            ],
            "created_at": "2026-09-14T12:00:00+03:00",
        }

    def test_profile_check_uses_same_named_region_mask_and_bounds(self):
        process_path = r"C:\Apps\Ruffle\ruffle.exe"
        tgt = _target(rect=(0, 0, 100, 100), process_path=process_path)
        image = Image.new("RGBA", (60, 60), (255, 255, 255, 255))
        constraints = {
            "safe_regions": [{"name": "content", "rect": [10, 20, 70, 80]}],
            "screenshot_masks": [{"rect": [20, 30, 30, 40]}],
            "max_screenshot_bytes": 1_000_000,
        }
        with (
            patch.object(server, "_prepare_action_target", return_value=tgt),
            patch.object(server, "_load_profile_file", return_value=self._profile()),
            patch.object(
                server, "_window_rect", return_value=wintypes.RECT(0, 0, 100, 100)
            ),
            patch.object(server, "_screenshot_constraints", return_value=constraints),
            patch.object(server, "_verify_action_target"),
            patch.object(server.ImageGrab, "grab", return_value=image) as grab,
            patch.object(
                server,
                "_profile_matches_live_window",
                return_value=(True, "matched"),
            ) as match,
        ):
            result = server.check_coordinate_profile("profile.json", hwnd=42)

        grab.assert_called_once_with(bbox=(10, 20, 70, 80), all_screens=True)
        live_png = match.call_args.args[3]
        decoded = Image.open(io.BytesIO(live_png))
        self.assertEqual(decoded.getpixel((10, 10)), (0, 0, 0, 255))
        self.assertTrue(result["matches"])

    def test_profile_check_rejects_unrestricted_legacy_capture(self):
        process_path = r"C:\Apps\Ruffle\ruffle.exe"
        tgt = _target(rect=(0, 0, 100, 100), process_path=process_path)
        with (
            patch.object(server, "_prepare_action_target", return_value=tgt),
            patch.object(server, "_load_profile_file", return_value=self._profile()),
            patch.object(
                server, "_window_rect", return_value=wintypes.RECT(0, 0, 100, 100)
            ),
            patch.object(
                server,
                "_screenshot_constraints",
                return_value={
                    "safe_regions": None,
                    "screenshot_masks": [],
                    "max_screenshot_bytes": 1_000_000,
                },
            ),
            patch.object(server.ImageGrab, "grab") as grab,
        ):
            with self.assertRaises(errors.PolicyDeniedError):
                server.check_coordinate_profile("profile.json", hwnd=42)
        grab.assert_not_called()

    def test_profile_check_fails_closed_if_injected_profile_lacks_reference_region(
        self,
    ):
        process_path = r"C:\Apps\Ruffle\ruffle.exe"
        tgt = _target(rect=(0, 0, 100, 100), process_path=process_path)
        profile = self._profile()
        profile["safe_regions"] = []
        with (
            patch.object(server, "_prepare_action_target", return_value=tgt),
            patch.object(server, "_load_profile_file", return_value=profile),
            patch.object(server.ImageGrab, "grab") as grab,
        ):
            with self.assertRaisesRegex(errors.PolicyDeniedError, "not found"):
                server.check_coordinate_profile("profile.json", hwnd=42)
        grab.assert_not_called()

    def test_profile_check_rejects_process_or_region_policy_drift(self):
        process_path = r"C:\Apps\Ruffle\ruffle.exe"
        tgt = _target(rect=(0, 0, 100, 100), process_path=process_path)
        with (
            patch.object(server, "_prepare_action_target", return_value=tgt),
            patch.object(
                server,
                "_load_profile_file",
                return_value=self._profile(r"C:\Apps\Other\other.exe"),
            ),
            patch.object(server.ImageGrab, "grab") as grab,
        ):
            with self.assertRaises(errors.PolicyDeniedError):
                server.check_coordinate_profile("profile.json", hwnd=42)
        grab.assert_not_called()

        with (
            patch.object(server, "_prepare_action_target", return_value=tgt),
            patch.object(server, "_load_profile_file", return_value=self._profile()),
            patch.object(
                server, "_window_rect", return_value=wintypes.RECT(0, 0, 100, 100)
            ),
            patch.object(
                server,
                "_screenshot_constraints",
                return_value={
                    "safe_regions": [{"name": "content", "rect": [11, 20, 70, 80]}],
                    "screenshot_masks": [],
                    "max_screenshot_bytes": 1_000_000,
                },
            ),
            patch.object(server.ImageGrab, "grab") as grab,
        ):
            with self.assertRaises(errors.PolicyDeniedError):
                server.check_coordinate_profile("profile.json", hwnd=42)
        grab.assert_not_called()


class ScreenshotToctouTests(unittest.TestCase):
    """TASK 2: if the target changes after the grab, the image must never be returned."""

    @staticmethod
    def _unrestricted_constraints():
        return {
            "safe_regions": None,
            "screenshot_masks": [],
            "max_screenshot_bytes": 1_000_000,
        }

    def test_screenshot_rejects_geometry_change_before_grab(self):
        tgt = _target()
        original = wintypes.RECT(0, 0, 200, 200)
        moved = wintypes.RECT(20, 0, 220, 200)
        with (
            patch.object(server, "_prepare_action_target", return_value=tgt),
            patch.object(server, "_window_rect", side_effect=[original, moved]),
            patch.object(
                server,
                "_screenshot_constraints",
                return_value=self._unrestricted_constraints(),
            ),
            patch.object(server, "_verify_action_target"),
            patch.object(server.ImageGrab, "grab") as grab,
        ):
            with self.assertRaises(errors.TargetStaleError):
                server.screenshot_window(hwnd=42)
        grab.assert_not_called()

    def test_screenshot_discards_pixels_if_geometry_changes_during_grab(self):
        tgt = _target()
        original = wintypes.RECT(0, 0, 200, 200)
        resized = wintypes.RECT(0, 0, 210, 200)
        fake_image = MagicMock()
        with (
            patch.object(server, "_prepare_action_target", return_value=tgt),
            patch.object(
                server, "_window_rect", side_effect=[original, original, resized]
            ),
            patch.object(
                server,
                "_screenshot_constraints",
                return_value=self._unrestricted_constraints(),
            ),
            patch.object(server, "_verify_action_target"),
            patch.object(server.ImageGrab, "grab", return_value=fake_image),
        ):
            with self.assertRaises(errors.TargetStaleError):
                server.screenshot_window(hwnd=42)
        fake_image.save.assert_not_called()
        fake_image.close.assert_called_once()

    def test_screenshot_discards_captured_image_if_target_changes_during_grab(self):
        tgt = _target()
        rect = wintypes.RECT(0, 0, 200, 200)
        fake_image = MagicMock()
        with (
            patch.object(server, "_prepare_action_target", return_value=tgt),
            patch.object(server, "_window_rect", return_value=rect),
            patch.object(
                server,
                "_screenshot_constraints",
                return_value={
                    "safe_regions": None,
                    "screenshot_masks": [],
                    "max_screenshot_bytes": 1_000_000,
                },
            ),
            patch.object(
                server,
                "_verify_action_target",
                side_effect=[
                    None,
                    RuntimeError(
                        "Could not verify the target window at the moment of the action; no action was sent."
                    ),
                ],
            ) as verify,
            patch.object(server.ImageGrab, "grab", return_value=fake_image) as grab,
        ):
            with self.assertRaises(RuntimeError):
                server.screenshot_window(hwnd=42)

        self.assertEqual(verify.call_count, 2)
        grab.assert_called_once()
        fake_image.save.assert_not_called()
        fake_image.close.assert_called_once()

    def test_screenshot_returns_image_when_target_stays_stable(self):
        tgt = _target()
        rect = wintypes.RECT(0, 0, 200, 200)
        fake_image = MagicMock()
        with (
            patch.object(server, "_prepare_action_target", return_value=tgt),
            patch.object(server, "_window_rect", return_value=rect),
            patch.object(
                server,
                "_screenshot_constraints",
                return_value={
                    "safe_regions": None,
                    "screenshot_masks": [],
                    "max_screenshot_bytes": 1_000_000,
                },
            ),
            patch.object(server, "_verify_action_target", return_value=None),
            patch.object(server.ImageGrab, "grab", return_value=fake_image),
        ):
            result = server.screenshot_window(hwnd=42)
        self.assertEqual(result.mimeType, "image/png")
        fake_image.save.assert_called_once()
        fake_image.close.assert_called_once()


class HealthCheckToolTests(unittest.TestCase):
    """The `health_check` MCP tool delegates to the real `health.check_health()`
    and requires NO action permission at all (see ROADMAP Phase 4)."""

    def test_health_check_requires_no_action_permission(self):
        with patch.dict(os.environ, {}, clear=True):
            result = server.health_check()
        self.assertIn("ok", result)
        self.assertIn("policy", result)
        self.assertIn("dependencies", result)
        self.assertIn("dpi_awareness", result)

    def test_health_check_delegates_to_the_real_module_function(self):
        sentinel = {"ok": True, "policy": {}, "dependencies": {}, "dpi_awareness": {}}
        with patch.object(server, "_check_health", return_value=sentinel) as check:
            self.assertIs(server.health_check(), sentinel)
        check.assert_called_once_with()


class WindowStateDiagnosticsTests(unittest.TestCase):
    """Phase 1 read-only tools: get_window_state, wait_for_window,
    wait_for_title_change — all require ACTION_OBSERVE, none require
    focus/foreground, and the two waiters carry a mandatory upper-bounded
    timeout."""

    def setUp(self):
        self.actions = patch.dict(
            os.environ, {"DESKTOP_AUTOMATION_ALLOWED_ACTIONS": "observe"}
        )
        self.actions.start()
        self.addCleanup(self.actions.stop)

    def test_get_window_state_reports_minimized_target_instead_of_not_found(self):
        raw_state = {
            "hwnd": 42,
            "title": "Ruffle player",
            "pid": 1,
            "rect": [0, 0, 100, 100],
            "visible": True,
            "iconic": True,
        }
        with (
            patch.object(server, "_window_diagnostic_state", return_value=raw_state),
            patch.object(platform_win32.user32, "GetForegroundWindow", return_value=99),
            patch.object(visibility, "_is_visibly_on_top", return_value=False),
        ):
            state = server.get_window_state(hwnd=42)
        self.assertTrue(state["iconic"])
        self.assertFalse(state["foreground"])
        self.assertFalse(state["on_top"])

    def test_get_window_state_requires_observe_permission(self):
        with patch.dict(os.environ, {"DESKTOP_AUTOMATION_ALLOWED_ACTIONS": ""}):
            with self.assertRaises(PermissionError):
                server.get_window_state(hwnd=42)

    def test_get_window_state_propagates_policy_denial_for_disallowed_window(self):
        with patch.object(
            server,
            "_window_diagnostic_state",
            side_effect=PermissionError(
                "hwnd=42 is not within the allowed policy scope."
            ),
        ):
            with self.assertRaises(PermissionError):
                server.get_window_state(hwnd=42)

    def test_wait_for_window_returns_as_soon_as_the_target_appears(self):
        tgt = _target()
        with (
            patch.object(
                server,
                "_resolve_window",
                side_effect=[ValueError(server._NOT_FOUND_MESSAGE), tgt],
            ) as resolve,
            patch.object(server.time, "sleep") as sleep,
        ):
            result = server.wait_for_window(
                "Ruffle", timeout_ms=1_000, poll_interval_ms=50
            )
        self.assertEqual(result["hwnd"], tgt.hwnd)
        self.assertEqual(resolve.call_count, 2)
        sleep.assert_called_once_with(0.05)

    def test_wait_for_window_times_out_without_hanging(self):
        with (
            patch.object(
                server,
                "_resolve_window",
                side_effect=ValueError(server._NOT_FOUND_MESSAGE),
            ),
            patch.object(server.time, "sleep"),
            patch.object(
                server.time,
                "monotonic",
                side_effect=[0.0, 0.0, 0.05, 0.2],
            ),
        ):
            with self.assertRaises(TimeoutError):
                server.wait_for_window("Ruffle", timeout_ms=100, poll_interval_ms=50)

    def test_wait_for_window_does_not_retry_an_ambiguous_match(self):
        with patch.object(
            server,
            "_resolve_window",
            side_effect=ValueError("Multiple allowed windows matched; ..."),
        ):
            with self.assertRaises(ValueError):
                server.wait_for_window("Ruffle", timeout_ms=1_000)

    def test_wait_for_window_rejects_timeout_outside_bounds(self):
        with self.assertRaises(ValueError):
            server.wait_for_window("Ruffle", timeout_ms=0)
        with self.assertRaises(ValueError):
            server.wait_for_window("Ruffle", timeout_ms=server.MAX_WAIT_MS + 1)

    def test_wait_for_title_change_returns_once_the_title_differs(self):
        before = _target(title="Untitled, Not Defteri")
        after = _target(title="*Untitled, Not Defteri")
        with (
            patch.object(
                server, "_current_window_snapshot", side_effect=[before, before, after]
            ),
            patch.object(server.time, "sleep") as sleep,
        ):
            result = server.wait_for_title_change(
                42, timeout_ms=1_000, poll_interval_ms=50
            )
        self.assertEqual(result["title"], "*Untitled, Not Defteri")
        sleep.assert_called_once_with(0.05)

    def test_wait_for_title_change_fails_fast_if_target_disappears(self):
        before = _target(title="Untitled, Not Defteri")
        with patch.object(
            server, "_current_window_snapshot", side_effect=[before, None]
        ):
            with self.assertRaises(RuntimeError):
                server.wait_for_title_change(42, timeout_ms=1_000)

    def test_wait_for_title_change_rejects_an_already_invisible_target(self):
        with patch.object(server, "_current_window_snapshot", return_value=None):
            with self.assertRaises(ValueError):
                server.wait_for_title_change(42, timeout_ms=1_000)


if __name__ == "__main__":
    unittest.main()
