"""Shared fake Win32 platform for chaos tests (tests only, never shipped).

`FakeWin32Platform` holds a REALISTIC fake desktop state — several windows,
each with hwnd/pid/title/rect/process generation/visibility flags plus one
shared z-order — and answers the raw Win32 calls the production code makes
(`GetForegroundWindow`, `GetWindowRect`, `SendInput`, ...) from that state.
Chaos tests drive the REAL `target.py` / `visibility.py` / `server.py`
functions with ONLY this surface faked, so a mid-action close, PID
recycling or occlusion exercises the genuine revalidation gates instead of
a mocked-out `_verify_action_target`.

Cleanup contract (no leaks): `patch_into(test_case)` starts every patch
and registers `addCleanup` stops, so the real Win32 handles come back even
when an assertion fails. Use exactly one active fake per test.

DPI approximation — READ THIS, it is deliberately limited: a real DPI
change cannot be simulated here (process-wide `SetProcessDpiAwareness`
runs once; per-monitor DPI plus the `ImageGrab` coordinate transform are
outside any fake). `set_rect()` therefore models only the OBSERVABLE
SYMPTOM the safety logic must survive: successive rect reads disagreeing.
What the chaos test then proves is honest and narrow: (1) geometry
instability is visible across reads, (2) `_verify_action_target` does not
consult geometry at all (identity-gated), and (3) out-of-window effects
stay rejected because bounds are checked against a FRESH rect on every
call. This is NOT a real 100/125/150 % DPI matrix (ROADMAP Faz 4 item
stays open) and must not be cited as one.
"""

import ctypes
import sys
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from desktop_automation_mcp import input_ as input_mod
from desktop_automation_mcp import platform_win32

KEYEVENTF_KEYUP = input_mod.KEYEVENTF_KEYUP


@dataclass
class FakeWindow:
    """One fake top-level window; all fields mutable mid-test (chaos)."""

    hwnd: int
    pid: int
    title: str
    rect: tuple[int, int, int, int]
    process_started_at: int
    exe_path: str
    window_class: str = "FakeWindow"
    visible: bool = True
    iconic: bool = False
    exists: bool = True


@dataclass
class _SendRecord:
    vk: int
    scan: int
    flags: int
    accepted: bool


class FakeWin32Platform:
    """Mutable fake desktop + patch binder for chaos tests."""

    def __init__(self) -> None:
        self._windows: dict[int, FakeWindow] = {}
        self._z_order: list[int] = []  # front-first hwnds
        self.foreground_hwnd: int = 0
        self._next_handle = 4096
        self._handles: dict[int, int] = {}  # fake handle -> pid
        self._fail_skip = 0
        self._fail_times = 0
        self._after_hooks: list[list] = []  # [remaining_successes, func]
        self.sendinput_calls: list[_SendRecord] = []
        self.mouse_events: list[int] = []
        self.cursor_moves: list[tuple[int, int]] = []
        self.posted_messages: list[tuple[int, int, int, int]] = []

    # ------------------------------------------------------------------
    # state setup / mutation (chaos choreography)
    # ------------------------------------------------------------------
    def add_window(
        self,
        hwnd: int,
        pid: int,
        title: str,
        rect: tuple[int, int, int, int],
        process_started_at: int,
        exe_path: str,
        **overrides,
    ) -> int:
        """Append a window at the BACK of the z-order; return its hwnd."""
        self._windows[hwnd] = FakeWindow(
            hwnd=hwnd,
            pid=pid,
            title=title,
            rect=rect,
            process_started_at=process_started_at,
            exe_path=exe_path,
            **overrides,
        )
        self._z_order.append(hwnd)
        if self.foreground_hwnd == 0:
            self.foreground_hwnd = hwnd
        return hwnd

    def close_window(self, hwnd: int) -> None:
        """The window is gone: enumeration, visibility and rect reads fail."""
        if hwnd in self._windows:
            self._windows[hwnd].exists = False

    def set_foreground(self, hwnd: int) -> None:
        self.foreground_hwnd = hwnd

    def set_pid(
        self, hwnd: int, new_pid: int, new_started_at: int | None = None
    ) -> None:
        """PID recycling simulation: same handle, new process generation."""
        window = self._windows[hwnd]
        window.pid = new_pid
        if new_started_at is not None:
            window.process_started_at = new_started_at

    def bring_to_front(self, hwnd: int) -> None:
        """Move a window to the FRONT of the z-order (occlusion)."""
        self._z_order = [hwnd] + [h for h in self._z_order if h != hwnd]

    def set_rect(self, hwnd: int, rect: tuple[int, int, int, int]) -> None:
        """Replace a window's rect (DPI-scale / move approximation)."""
        self._windows[hwnd].rect = rect

    def raise_next_sendinput_failure(self, *, after: int = 0, times: int = 1) -> None:
        """Fail upcoming SendInput calls: `after` succeed, then `times` fail.

        A failed call returns 0 (the production code raises PlatformError);
        the attempt is still recorded with `accepted=False`.
        """
        self._fail_skip = after
        self._fail_times = times

    def after_sends(self, n: int, func) -> None:
        """Run `func(platform)` after `n` further SUCCESSFUL SendInputs.

        Deterministic mid-action trigger: the production send loop keeps
        calling the fake, which evolves the desktop underneath it.
        """
        if n < 1:
            raise ValueError("after_sends(n) requires n >= 1")
        self._after_hooks.append([n, func])

    def held_keys(self) -> list[tuple[int, int]]:
        """(vk, scan) pairs with a successful down but no successful up.

        Only SUCCESSFUL sends count: a rejected down never held anything,
        so the invariant that matters is accepted-downs == accepted-ups.
        Empty means no key is physically stuck.
        """
        balance: dict[tuple[int, int], int] = {}
        for record in self.sendinput_calls:
            if not record.accepted:
                continue
            key = (record.vk, record.scan)
            delta = -1 if record.flags & KEYEVENTF_KEYUP else 1
            balance[key] = balance.get(key, 0) + delta
        return sorted(key for key, count in balance.items() if count > 0)

    # ------------------------------------------------------------------
    # patch binder
    # ------------------------------------------------------------------
    _USER32_FUNCS = (
        "GetForegroundWindow",
        "IsWindowVisible",
        "IsIconic",
        "IsWindow",
        "GetWindowRect",
        "GetWindowTextLengthW",
        "GetWindowTextW",
        "GetWindowThreadProcessId",
        "WindowFromPoint",
        "GetAncestor",
        "EnumWindows",
        "SendInput",
        "SetCursorPos",
        "PostMessageW",
        "mouse_event",
        "GetClassNameW",
        "ShowWindow",
        "SetForegroundWindow",
    )
    _KERNEL32_FUNCS = (
        "OpenProcess",
        "QueryFullProcessImageNameW",
        "GetProcessTimes",
        "CloseHandle",
    )

    def patch_into(self, test_case):
        """Patch the whole Win32 surface; register cleanup stops.

        Returns self for chaining. Exactly one active fake per test.
        """
        started = []
        try:
            for name in self._USER32_FUNCS:
                patcher = patch.object(
                    platform_win32.user32,
                    name,
                    side_effect=getattr(self, f"_fake_{name}"),
                )
                patcher.start()
                started.append(patcher)
            for name in self._KERNEL32_FUNCS:
                patcher = patch.object(
                    platform_win32.kernel32,
                    name,
                    side_effect=getattr(self, f"_fake_{name}"),
                )
                patcher.start()
                started.append(patcher)
        except Exception:
            for patcher in reversed(started):
                patcher.stop()
            raise
        for patcher in started:
            test_case.addCleanup(patcher.stop)
        return self

    # ------------------------------------------------------------------
    # lookup helpers
    # ------------------------------------------------------------------
    def _live(self, hwnd: int) -> FakeWindow | None:
        window = self._windows.get(hwnd)
        return window if window is not None and window.exists else None

    def _window_by_pid(self, pid: int) -> FakeWindow | None:
        for window in self._windows.values():
            if window.exists and window.pid == pid:
                return window
        return None

    # ------------------------------------------------------------------
    # user32 fakes
    # ------------------------------------------------------------------
    def _fake_GetForegroundWindow(self):
        return self.foreground_hwnd

    def _fake_IsWindowVisible(self, hwnd):
        window = self._live(hwnd)
        return bool(window is not None and window.visible)

    def _fake_IsIconic(self, hwnd):
        window = self._live(hwnd)
        return bool(window is not None and window.iconic)

    def _fake_IsWindow(self, hwnd):
        return self._live(hwnd) is not None

    def _fake_GetWindowRect(self, hwnd, lp_rect):
        window = self._live(hwnd)
        if window is None:
            return False
        rect = ctypes.cast(lp_rect, ctypes.POINTER(wintypes.RECT)).contents
        rect.left, rect.top, rect.right, rect.bottom = window.rect
        return True

    def _fake_GetWindowTextLengthW(self, hwnd):
        window = self._live(hwnd)
        return len(window.title) if window is not None else 0

    def _fake_GetWindowTextW(self, hwnd, buf, _maxlen):
        window = self._live(hwnd)
        if window is None:
            return 0
        buf.value = window.title
        return len(window.title)

    def _fake_GetWindowThreadProcessId(self, hwnd, lp_pid):
        window = self._live(hwnd)
        if window is None:
            return False
        ctypes.cast(lp_pid, ctypes.POINTER(wintypes.DWORD)).contents.value = window.pid
        return True

    def _fake_WindowFromPoint(self, point):
        x, y = point.x, point.y
        for hwnd in self._z_order:
            window = self._live(hwnd)
            if window is None or not window.visible or window.iconic:
                continue
            left, top, right, bottom = window.rect
            if left <= x < right and top <= y < bottom:
                return hwnd
        return 0

    def _fake_GetAncestor(self, hwnd, _flags):
        return hwnd  # all fake windows are top-level; root is self

    def _fake_EnumWindows(self, callback, lparam):
        for hwnd in list(self._z_order):
            if self._live(hwnd) is None:
                continue
            if not callback(hwnd, lparam):
                break
        return True

    def _fake_SendInput(self, count, pointer, _size):
        event = ctypes.cast(pointer, ctypes.POINTER(input_mod._Input)).contents
        vk, scan, flags = (
            event.ii.ki.wVk,
            event.ii.ki.wScan,
            event.ii.ki.dwFlags,
        )
        if self._fail_skip > 0:
            self._fail_skip -= 1
            accepted = True
        elif self._fail_times > 0:
            self._fail_times -= 1
            accepted = False
        else:
            accepted = True
        self.sendinput_calls.append(_SendRecord(vk, scan, flags, accepted))
        if accepted:
            for hook in list(self._after_hooks):
                hook[0] -= 1
                if hook[0] <= 0:
                    self._after_hooks.remove(hook)
                    hook[1](self)
        return count if accepted else 0

    def _fake_SetCursorPos(self, x, y):
        self.cursor_moves.append((x, y))
        return True

    def _fake_PostMessageW(self, hwnd, msg, wparam, lparam):
        self.posted_messages.append((hwnd, msg, wparam, lparam))
        return self._live(hwnd) is not None

    def _fake_mouse_event(self, *args):
        self.mouse_events.append(args[0] if args else 0)
        return None

    def _fake_GetClassNameW(self, hwnd, buf, _maxlen):
        window = self._live(hwnd)
        if window is None:
            return 0
        buf.value = window.window_class
        return len(window.window_class)

    def _fake_ShowWindow(self, _hwnd, _cmd):
        return True

    def _fake_SetForegroundWindow(self, hwnd):
        self.foreground_hwnd = hwnd
        return True

    # ------------------------------------------------------------------
    # kernel32 fakes
    # ------------------------------------------------------------------
    def _fake_OpenProcess(self, _access, _inherit, pid):
        if self._window_by_pid(pid) is None:
            return 0
        handle = self._next_handle
        self._next_handle += 1
        self._handles[handle] = pid
        return handle

    def _fake_QueryFullProcessImageNameW(self, handle, _flags, buf, lp_length):
        window = self._window_by_pid(self._handles.get(handle, -1))
        if window is None:
            return False
        buf.value = window.exe_path
        ctypes.cast(lp_length, ctypes.POINTER(wintypes.DWORD)).contents.value = len(
            window.exe_path
        )
        return True

    def _fake_GetProcessTimes(self, handle, lp_created, *_rest):
        window = self._window_by_pid(self._handles.get(handle, -1))
        if window is None:
            return False
        created = ctypes.cast(lp_created, ctypes.POINTER(wintypes.FILETIME)).contents
        created.dwLowDateTime = window.process_started_at & 0xFFFFFFFF
        created.dwHighDateTime = (window.process_started_at >> 32) & 0xFFFFFFFF
        return True

    def _fake_CloseHandle(self, handle):
        self._handles.pop(handle, None)
        return True
