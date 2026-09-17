"""Focus, z-order, and pre-effect target re-verification.

This module answers: is the target window actually the frontmost, unobscured
thing on screen right now, and is it still the same process/window it was
when it was resolved? It owns the "verify, then act" gate that every
state-changing tool (and `screenshot_window`) must pass through immediately
before it touches Win32 input or pixel APIs. It does NOT decide which windows
are allowed (`policy.py`) or how to enumerate/resolve them (`target.py`) —
it only re-checks a `TargetSnapshot` that has already been resolved.

`_window_rect` is imported (not duplicated) from `target.py`, which is the
module that owns rect-reading as part of window identity; keeping a single
definition avoids a target<->visibility import cycle.
"""

from __future__ import annotations

import ctypes
import time
from ctypes import wintypes

from .errors import (
    InputRejectedError,
    TargetNotForegroundError,
    TargetOccludedError,
    TargetStaleError,
)
from .platform_win32 import GA_ROOT, SW_RESTORE, get_win32_adapter
from .policy import FOCUS_MODE_PASSIVE, _focus_mode
from .target import TargetSnapshot, _current_window_snapshot, _window_rect

# SetForegroundWindow normally takes effect synchronously.  A short settle
# interval makes the common path responsive while retries retain the prior
# two-second verification budget for slower desktops.
FOCUS_SETTLE_DELAY_S = 0.05
FOCUS_RETRIES = 40


def _user32():
    return get_win32_adapter().user32


__all__ = [
    "FOCUS_RETRIES",
    "FOCUS_SETTLE_DELAY_S",
    "_absolute_point",
    "_focus_and_verify",
    "_is_visibly_on_top",
    "_verify_action_target",
    "_verify_observable",
    "_window_rect",
]


def _is_visibly_on_top(hwnd: int) -> bool:
    rect = wintypes.RECT()
    if not _user32().GetWindowRect(hwnd, ctypes.byref(rect)):
        return False
    width, height = rect.right - rect.left, rect.bottom - rect.top
    if width <= 0 or height <= 0:
        return False
    points = (
        (rect.left + width // 2, rect.top + height // 2),
        (rect.left + width // 4, rect.top + height // 4),
        (rect.left + 3 * width // 4, rect.top + 3 * height // 4),
    )
    return all(
        (hit := _user32().WindowFromPoint(wintypes.POINT(x, y)))
        and _user32().GetAncestor(hit, GA_ROOT) == hwnd
        for x, y in points
    )


def _focus_and_verify(
    hwnd: int,
    *,
    retries: int = FOCUS_RETRIES,
    delay_s: float = FOCUS_SETTLE_DELAY_S,
) -> None:
    """Verifies focus without stealing it unless activation is explicitly enabled."""
    if _focus_mode() == FOCUS_MODE_PASSIVE:
        if _user32().GetForegroundWindow() == hwnd and _is_visibly_on_top(hwnd):
            return
        raise TargetNotForegroundError(
            "Target window is not foreground; passive mode never changes focus. "
            "Have the operator bring the window forward, or set DESKTOP_AUTOMATION_FOCUS_MODE=activate."
        )
    _user32().ShowWindow(hwnd, SW_RESTORE)
    for _ in range(retries):
        _user32().SetForegroundWindow(hwnd)
        time.sleep(delay_s)
        if _user32().GetForegroundWindow() == hwnd and _is_visibly_on_top(hwnd):
            return
    raise TargetNotForegroundError(
        "Could not verify the window as foreground and visible; no action was taken for safety."
    )


def _verify_observable(hwnd: int) -> None:
    """Verifies a target is visible and unobscured WITHOUT requiring foreground.

    Used only by observation tools (`screenshot_window`,
    `check_coordinate_profile`) so a screenshot can be taken of a window
    the operator has not brought to the front — reading pixels does not
    steal keyboard focus or change window order, unlike every other
    guarded action. Occlusion is still enforced: a window fully covered by
    another window is not "observable" even though it is not foreground.
    """
    if not _is_visibly_on_top(hwnd):
        raise TargetOccludedError(
            "Another window is covering the target; no image was captured."
        )


def _verify_action_target(
    target: TargetSnapshot, *, require_foreground: bool = True
) -> None:
    """Rebind the HWND immediately before an effect is emitted.

    HWND values are recyclable.  Foreground/occlusion checks alone therefore
    cannot prove that the window focused earlier is still the same process at
    the moment an input event is sent — PID plus process-creation time is what
    actually pins identity across a PID/HWND reuse.

    The window's CURRENT title is deliberately not compared against the title
    captured at resolution time.  Many real targets rewrite their own title as
    a side effect of the very input this gate is guarding (e.g. Notepad
    prefixing an unsaved document with "*"); comparing for exact equality
    would make `_verify_action_target` reject the tool's own prior effect
    mid-`send_text`, aborting a multi-character send after the first
    character. The title policy is not weakened by dropping this comparison:
    `_current_window_snapshot` already re-runs `_is_allowed_window`, which
    re-checks the CURRENT title against the configured allow-glob. A rename
    that escapes the policy still yields `current is None` below.

    `require_foreground=False` is used only by observation tools
    (`screenshot_window`, `check_coordinate_profile`): reading pixels does
    not steal keyboard focus, so those tools may observe a window the
    operator has not brought to the front. Occlusion is still checked in
    both modes — a covered window is never observable or actionable.
    """
    hwnd = target.hwnd
    current = _current_window_snapshot(hwnd)
    if (
        current is None
        or current.pid != target.pid
        or (current.process_started_at != target.process_started_at)
    ):
        raise TargetStaleError(
            "Could not verify target identity at the moment of the action "
            "(window closed, moved outside policy, or HWND/PID reused); "
            "no action was sent."
        )
    if require_foreground and _user32().GetForegroundWindow() != hwnd:
        raise TargetNotForegroundError(
            "Target is not foreground at the moment of the action; no action was sent."
        )
    if not _is_visibly_on_top(hwnd):
        raise TargetOccludedError(
            "Another window is covering the target at the moment of the action; "
            "no action was sent."
        )


def _absolute_point(hwnd: int, x: int, y: int) -> tuple[int, int]:
    if not isinstance(x, int) or not isinstance(y, int):
        raise InputRejectedError("x and y must be integers.")
    rect = _window_rect(hwnd)
    width, height = rect.right - rect.left, rect.bottom - rect.top
    if not 0 <= x < width or not 0 <= y < height:
        raise InputRejectedError(
            f"Out-of-window coordinate rejected: ({x}, {y}); "
            f"valid range 0..{width - 1}, 0..{height - 1}."
        )
    return rect.left + x, rect.top + y
