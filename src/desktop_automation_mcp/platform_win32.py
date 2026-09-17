"""Win32 ctypes function signatures, DPI-awareness init, and raw constants.

This module answers one question: "how does this process talk to the Win32
API surface?" It owns the `user32`/`kernel32` handles, their `argtypes`/
`restype` declarations (getting these wrong causes silent ABI corruption on
64-bit Windows, not a clean exception), the one-time DPI-awareness call, the
context-scoped injection seam, and every raw Win32 numeric constant. It does
NOT decide policy, resolve targets, or perform input — callers resolve the
current adapter at call time and keep those decisions in their own modules.
"""

from __future__ import annotations

import ctypes
from contextlib import contextmanager
from contextvars import ContextVar
from ctypes import wintypes
from dataclasses import dataclass
from typing import Any, Iterator

from .errors import PlatformError

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
user32.WindowFromPoint.restype = wintypes.HWND
user32.WindowFromPoint.argtypes = [wintypes.POINT]
user32.GetAncestor.restype = wintypes.HWND
user32.GetAncestor.argtypes = [wintypes.HWND, ctypes.c_uint]
user32.GetForegroundWindow.restype = wintypes.HWND
user32.IsWindow.argtypes = [wintypes.HWND]
user32.IsWindow.restype = wintypes.BOOL
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetClassNameW.restype = ctypes.c_int
# A window class's maximum length per MSDN (WNDCLASSEX lpszClassName remarks).
MAX_WINDOW_CLASS_LENGTH = 256
user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
user32.SetCursorPos.restype = wintypes.BOOL
user32.PostMessageW.argtypes = [
    wintypes.HWND,
    ctypes.c_uint,
    wintypes.WPARAM,
    wintypes.LPARAM,
]
user32.PostMessageW.restype = wintypes.BOOL
user32.SendInput.restype = ctypes.c_uint
user32.GetSystemMetrics.argtypes = [ctypes.c_int]
user32.GetSystemMetrics.restype = ctypes.c_int
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE,
    wintypes.DWORD,
    wintypes.LPWSTR,
    ctypes.POINTER(wintypes.DWORD),
]
kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL
kernel32.GetProcessTimes.argtypes = [
    wintypes.HANDLE,
    ctypes.POINTER(wintypes.FILETIME),
    ctypes.POINTER(wintypes.FILETIME),
    ctypes.POINTER(wintypes.FILETIME),
    ctypes.POINTER(wintypes.FILETIME),
]
kernel32.GetProcessTimes.restype = wintypes.BOOL


@dataclass(frozen=True, slots=True)
class Win32Adapter:
    """Injectable holder for the two native API surfaces used by the app.

    The default instance wraps the real ``ctypes.windll`` handles. Tests and
    alternate hosts can install a scoped adapter without replacing process-
    global DLL attributes. Callers resolve the adapter at call time so the
    override also reaches already-imported modules.
    """

    user32: Any
    kernel32: Any


_WIN32_ADAPTER: ContextVar[Win32Adapter] = ContextVar(
    "desktop_automation_win32_adapter",
    default=Win32Adapter(user32=user32, kernel32=kernel32),
)


def get_win32_adapter() -> Win32Adapter:
    """Return the adapter scoped to the current execution context."""
    return _WIN32_ADAPTER.get()


@contextmanager
def use_win32_adapter(adapter: Win32Adapter) -> Iterator[None]:
    """Temporarily install ``adapter`` and restore the prior value safely."""
    if not isinstance(adapter, Win32Adapter):
        raise TypeError("adapter must be a Win32Adapter.")
    token = _WIN32_ADAPTER.set(adapter)
    try:
        yield
    finally:
        _WIN32_ADAPTER.reset(token)


try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass

SW_RESTORE = 9
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_WHEEL = 0x0800
WHEEL_DELTA = 120
# WM_CLOSE (0x0010) numerically collides with MOUSEEVENTF_RIGHTUP (0x0010);
# they are unrelated constant spaces (window message vs. mouse_event flag)
# consumed by different Win32 calls, so this is coincidence, not a bug.
WM_CLOSE = 0x0010
WM_CHAR = 0x0102
GA_ROOT = 2
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
# GetSystemMetrics virtual-desktop coordinates.  The four values describe the
# bounding rectangle of all attached monitors, not only the primary display.
SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79

# --- Read-only DPI-awareness query (for health.py) ---
#
# shcore.SetProcessDpiAwareness(2) is tried at the top of the module; this
# block only queries its RESULT, it does NOT select a new awareness level
# (read-only). shcore.dll does not exist on older Windows, so access is
# guarded; import is NOT broken (stays None, the query returns "unknown").
try:
    _shcore = ctypes.windll.shcore
except Exception:
    _shcore = None
if _shcore is not None:
    _shcore.GetProcessDpiAwareness.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(ctypes.c_int),
    ]
    _shcore.GetProcessDpiAwareness.restype = ctypes.c_long
    kernel32.GetCurrentProcess.argtypes = []
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE

# PROCESS_DPI_AWARENESS enum (shcore.h): the same numbers as the
# startup SetProcessDpiAwareness(2) value.
PROCESS_DPI_UNAWARE = 0
PROCESS_SYSTEM_DPI_AWARE = 1
PROCESS_PER_MONITOR_DPI_AWARE = 2


def _process_dpi_awareness() -> tuple[int | None, str]:
    """Reads the process's DPI-awareness with a REAL Win32 query.

    Returns: (value | None, note). Value is the shcore enum (0/1/2);
    None means "could not be queried" (NOT "not aware" — a broken probe
    must not be confused with absence). Never CHANGES anything, only reads.
    """
    if _shcore is None:
        return None, "shcore.dll missing; DPI query is not possible on this system"
    awareness = ctypes.c_int(-1)
    try:
        hresult = _shcore.GetProcessDpiAwareness(
            get_win32_adapter().kernel32.GetCurrentProcess(), ctypes.byref(awareness)
        )
    except Exception as exc:
        return None, f"query call failed: {exc}"
    if hresult != 0:
        return None, f"GetProcessDpiAwareness returned HRESULT={hresult:#x}"
    return int(awareness.value), ""


def require_process_dpi_awareness() -> None:
    """Fail closed before pixel work unless Win32 confirms an aware process."""
    value, note = _process_dpi_awareness()
    if value in (PROCESS_SYSTEM_DPI_AWARE, PROCESS_PER_MONITOR_DPI_AWARE):
        return
    detail = note or f"GetProcessDpiAwareness={value!r}"
    raise PlatformError(
        "Could not capture screenshot: process could not be verified as "
        f"DPI-aware ({detail})."
    )


def virtual_screen_bounds() -> tuple[int, int, int, int]:
    """Read the physical-pixel bounds of the complete virtual desktop.

    A zero origin is valid, so only non-positive width/height is a failed
    probe.  Callers must reject a requested capture outside these bounds;
    silently clamping would produce an image different from the approved
    named region.
    """
    user32_api = get_win32_adapter().user32
    try:
        left = int(user32_api.GetSystemMetrics(SM_XVIRTUALSCREEN))
        top = int(user32_api.GetSystemMetrics(SM_YVIRTUALSCREEN))
        width = int(user32_api.GetSystemMetrics(SM_CXVIRTUALSCREEN))
        height = int(user32_api.GetSystemMetrics(SM_CYVIRTUALSCREEN))
    except Exception as exc:
        raise PlatformError("Could not read virtual screen bounds via Win32.") from exc
    if width <= 0 or height <= 0:
        raise PlatformError(
            f"Invalid virtual screen bounds: width={width}, height={height}."
        )
    return left, top, left + width, top + height
