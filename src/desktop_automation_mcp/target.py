"""Window/process identity: hwnd, PID, executable path, rect, generation.

This module answers: which live HWNDs currently satisfy policy, and what is
the verifiable identity (`TargetSnapshot`) of one such window right now? It
enumerates top-level windows, resolves an operator-supplied `title_contains`
or `hwnd` to exactly one allowed window, and reads a window's current
identity without a full re-scan. It does NOT decide focus/visibility/z-order
(`visibility.py`) and does NOT read the environment policy itself — it calls
into `policy.py` for that decision.
"""

from __future__ import annotations

import ctypes
import fnmatch
from ctypes import wintypes
from dataclasses import dataclass

from .errors import InputRejectedError, PlatformError, PolicyDeniedError
from .platform_win32 import (
    MAX_WINDOW_CLASS_LENGTH,
    PROCESS_QUERY_LIMITED_INFORMATION,
    get_win32_adapter,
)
from .policy import (
    _allowed_process_paths,
    _allowed_title_patterns,
    _is_allowed_title,
    _normalise_process_path,
)


def _user32():
    return get_win32_adapter().user32


def _kernel32():
    return get_win32_adapter().kernel32


@dataclass(frozen=True, slots=True)
class TargetSnapshot:
    """Identity captured for an allowed visible top-level window.

    `process_path` and `window_class` complete the Target model ROADMAP §7
    calls for ("full process path, window class") — both are read once at
    resolution time alongside the rest of the identity, not derived lazily,
    so a snapshot is fully self-describing without a second Win32 round-trip.
    """

    hwnd: int
    title: str
    pid: int
    rect: tuple[int, int, int, int]
    process_started_at: int = 0
    process_path: str = ""
    window_class: str = ""

    def as_public_dict(self) -> dict:
        """The MCP `list_windows` response shape.

        `process_path`/`window_class` were added additively (existing keys
        and their meaning are unchanged) alongside the original `hwnd`,
        `title`, `pid`, `rect` fields.
        """
        return {
            "hwnd": self.hwnd,
            "title": self.title,
            "pid": self.pid,
            "rect": list(self.rect),
            "process_path": self.process_path,
            "window_class": self.window_class,
        }


def _process_path(pid: int) -> str:
    handle = _kernel32().OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        raise PermissionError(f"Could not verify target process identity (pid={pid}).")
    try:
        buffer = ctypes.create_unicode_buffer(32_768)
        length = wintypes.DWORD(len(buffer))
        if not _kernel32().QueryFullProcessImageNameW(
            handle, 0, buffer, ctypes.byref(length)
        ):
            raise PermissionError(
                f"Could not read target process executable name (pid={pid})."
            )
        return _normalise_process_path(buffer.value)
    finally:
        _kernel32().CloseHandle(handle)


def _process_started_at(pid: int) -> int:
    """Return the process creation FILETIME as a stable PID-generation marker."""
    handle = _kernel32().OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        raise PermissionError(
            f"Could not verify target process start time (pid={pid})."
        )
    try:
        created = wintypes.FILETIME()
        exited = wintypes.FILETIME()
        kernel_time = wintypes.FILETIME()
        user_time = wintypes.FILETIME()
        if not _kernel32().GetProcessTimes(
            handle,
            ctypes.byref(created),
            ctypes.byref(exited),
            ctypes.byref(kernel_time),
            ctypes.byref(user_time),
        ):
            raise PermissionError(
                f"Could not read target process start time (pid={pid})."
            )
        return (int(created.dwHighDateTime) << 32) | int(created.dwLowDateTime)
    finally:
        _kernel32().CloseHandle(handle)


def _window_class(hwnd: int) -> str:
    buffer = ctypes.create_unicode_buffer(MAX_WINDOW_CLASS_LENGTH)
    length = _user32().GetClassNameW(hwnd, buffer, MAX_WINDOW_CLASS_LENGTH)
    if length <= 0:
        raise PlatformError(f"Could not read window class for hwnd={hwnd}.")
    return buffer.value


def _is_allowed_window(title: str, pid: int) -> bool:
    return _is_allowed_title(title) and _process_path(pid) in _allowed_process_paths()


def _enum_windows() -> list[TargetSnapshot]:
    # If policy is missing, do not swallow that silently inside the
    # callback; reject the call explicitly instead.
    title_patterns = _allowed_title_patterns()
    process_paths = _allowed_process_paths()
    windows: list[TargetSnapshot] = []
    callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

    def callback(hwnd: int, _lparam: int) -> bool:
        if not _user32().IsWindowVisible(hwnd) or _user32().IsIconic(hwnd):
            return True
        length = _user32().GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        _user32().GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value
        if not any(
            fnmatch.fnmatchcase(title.casefold(), pattern.casefold())
            for pattern in title_patterns
        ):
            return True
        pid, rect = wintypes.DWORD(), wintypes.RECT()
        if not _user32().GetWindowThreadProcessId(hwnd, ctypes.byref(pid)):
            return True
        try:
            process_path = _process_path(int(pid.value))
            process_started_at = _process_started_at(int(pid.value))
        except PermissionError:
            return True
        if process_path not in process_paths:
            return True
        if not _user32().GetWindowRect(hwnd, ctypes.byref(rect)):
            return True
        try:
            window_class = _window_class(hwnd)
        except OSError:
            window_class = ""
        windows.append(
            TargetSnapshot(
                hwnd=int(hwnd),
                title=title,
                pid=int(pid.value),
                rect=(rect.left, rect.top, rect.right, rect.bottom),
                process_started_at=process_started_at,
                process_path=process_path,
                window_class=window_class,
            )
        )
        return True

    if not _user32().EnumWindows(callback_type(callback), 0):
        raise PlatformError("Could not list open windows.")
    return windows


def _current_window_snapshot(hwnd: int) -> TargetSnapshot | None:
    """Read one HWND's current identity without scanning unrelated windows.

    This is used only after target resolution.  It retains the full identity
    and policy checks while avoiding an ``EnumWindows`` + ``OpenProcess`` pass
    for every visible candidate on each input character.
    """
    if not _user32().IsWindowVisible(hwnd) or _user32().IsIconic(hwnd):
        return None
    length = _user32().GetWindowTextLengthW(hwnd)
    if length <= 0:
        return None
    title_buffer = ctypes.create_unicode_buffer(length + 1)
    _user32().GetWindowTextW(hwnd, title_buffer, length + 1)
    title = title_buffer.value
    pid = wintypes.DWORD()
    if not _user32().GetWindowThreadProcessId(hwnd, ctypes.byref(pid)):
        return None
    try:
        if not _is_allowed_window(title, int(pid.value)):
            return None
        process_path = _process_path(int(pid.value))
        process_started_at = _process_started_at(int(pid.value))
    except PermissionError:
        return None
    rect = _window_rect(hwnd)
    try:
        window_class = _window_class(hwnd)
    except OSError:
        window_class = ""
    return TargetSnapshot(
        hwnd=int(hwnd),
        title=title,
        pid=int(pid.value),
        rect=(rect.left, rect.top, rect.right, rect.bottom),
        process_started_at=process_started_at,
        process_path=process_path,
        window_class=window_class,
    )


def _window_diagnostic_state(hwnd: int) -> dict:
    """Report an allowed window's live state for read-only diagnostics.

    `_current_window_snapshot` deliberately reads a minimized or hidden
    window as gone (`None`), because that is the correct answer for the
    pre-effect revalidation gate: a hidden target must never receive input.
    This function answers a different question — "why can't I act on this
    hwnd right now?" — which needs to distinguish minimized, hidden,
    policy-denied, and truly-gone rather than collapsing all four into the
    same "not found".  It is read-only and still enforces the title+process
    allowlist; it does not expose state for a window outside that policy.
    """
    if not _user32().IsWindow(hwnd):
        raise ValueError(f"hwnd={hwnd} no longer exists.")
    length = _user32().GetWindowTextLengthW(hwnd)
    title_buffer = ctypes.create_unicode_buffer(max(length, 0) + 1)
    if length > 0:
        _user32().GetWindowTextW(hwnd, title_buffer, length + 1)
    title = title_buffer.value
    pid = wintypes.DWORD()
    if not _user32().GetWindowThreadProcessId(hwnd, ctypes.byref(pid)):
        raise PlatformError(f"Could not read process identity for hwnd={hwnd}.")
    if not _is_allowed_window(title, int(pid.value)):
        # The caller supplied an HWND but did not earn access to its title.
        # Returning the title in a rejection turns a diagnostic guard into a
        # content-disclosure oracle, so preserve only the opaque handle and
        # use the common stable policy-denial taxonomy.
        raise PolicyDeniedError(f"hwnd={hwnd} is not within the allowed policy scope.")
    rect = _window_rect(hwnd)
    try:
        window_class = _window_class(hwnd)
    except OSError:
        window_class = ""
    return {
        "hwnd": int(hwnd),
        "title": title,
        "pid": int(pid.value),
        "process_path": _process_path(int(pid.value)),
        "window_class": window_class,
        "rect": [rect.left, rect.top, rect.right, rect.bottom],
        "visible": bool(_user32().IsWindowVisible(hwnd)),
        "iconic": bool(_user32().IsIconic(hwnd)),
    }


def _window_rect(hwnd: int) -> wintypes.RECT:
    rect = wintypes.RECT()
    if not _user32().GetWindowRect(hwnd, ctypes.byref(rect)):
        raise PlatformError("Could not read the target window's bounds.")
    return rect


def _resolve_window(title_contains: str | None, hwnd: int | None) -> TargetSnapshot:
    if (title_contains is None) == (hwnd is None):
        raise InputRejectedError("Give exactly one target: title_contains or hwnd.")
    windows = _enum_windows()
    if hwnd is not None:
        match = next((window for window in windows if window.hwnd == hwnd), None)
        if match is None:
            raise InputRejectedError(
                "This hwnd is not visible, not allowed, or no longer belongs to the same window."
            )
        return match
    if not title_contains or not title_contains.strip():
        raise InputRejectedError("title_contains must not be empty.")
    matches = [
        window
        for window in windows
        if title_contains.casefold() in window.title.casefold()
    ]
    if not matches:
        raise InputRejectedError("No target found among the allowed visible windows.")
    if len(matches) > 1:
        raise InputRejectedError(
            "Multiple allowed windows matched; use the hwnd from list_windows() output."
        )
    return matches[0]
