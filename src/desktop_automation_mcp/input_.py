"""Mouse/keyboard/Unicode text primitives and safe cleanup after failure.

This module answers: how do we emit a single verified keyboard or Unicode
text event through `SendInput`, and how do we guarantee that a failure
mid-emission never leaves a physical key held down? It owns the raw Win32
`INPUT` struct layouts, the virtual-key table, and the per-character
revalidation loop used by `send_text`'s Unicode path. It does NOT resolve or
verify a target on its own — each unit of work here revalidates through
`visibility._verify_action_target` before every single emitted event, and the
caller (`server.py`) is responsible for the initial `_prepare_action_target`
gate and any outer `finally` cleanup for multi-step gestures (click, key).
"""

from __future__ import annotations

import ctypes
import time

from .errors import InputRejectedError, PlatformError
from .platform_win32 import get_win32_adapter
from .target import TargetSnapshot
from .visibility import _verify_action_target

# dwExtraInfo is ULONG_PTR on Windows. Using c_ulong misaligns the INPUT
# struct on 64-bit Windows and makes SendInput fail silently.
ULONG_PTR = ctypes.c_size_t
PUL = ctypes.POINTER(ULONG_PTR)


class _KeyBdInput(ctypes.Structure):
    _fields_ = [
        ("wVk", ctypes.c_ushort),
        ("wScan", ctypes.c_ushort),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", PUL),
    ]


class _MouseInput(ctypes.Structure):
    # The largest member of the INPUT union on 64-bit Windows. Even though
    # this field is never sent, INPUT.cbSize must match the Win32 ABI.
    _fields_ = [
        ("dx", ctypes.c_long),
        ("dy", ctypes.c_long),
        ("mouseData", ctypes.c_ulong),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", PUL),
    ]


class _Input_I(ctypes.Union):
    _fields_ = [("mi", _MouseInput), ("ki", _KeyBdInput)]


class _Input(ctypes.Structure):
    _fields_ = [("type", ctypes.c_ulong), ("ii", _Input_I)]


INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
TEXT_INTERCHAR_DELAY_S = 0.001
VK_MAP = {
    "backspace": 0x08,
    "tab": 0x09,
    "enter": 0x0D,
    "return": 0x0D,
    "shift": 0x10,
    "ctrl": 0x11,
    "control": 0x11,
    "alt": 0x12,
    "pause": 0x13,
    "capslock": 0x14,
    "esc": 0x1B,
    "escape": 0x1B,
    "space": 0x20,
    "pageup": 0x21,
    "pagedown": 0x22,
    "end": 0x23,
    "home": 0x24,
    "left": 0x25,
    "up": 0x26,
    "right": 0x27,
    "down": 0x28,
    "insert": 0x2D,
    "delete": 0x2E,
    "del": 0x2E,
}


def _user32():
    return get_win32_adapter().user32


for _i in range(10):
    VK_MAP[str(_i)] = 0x30 + _i
for _c in "abcdefghijklmnopqrstuvwxyz":
    VK_MAP[_c] = ord(_c.upper())
for _i in range(1, 13):
    VK_MAP[f"f{_i}"] = 0x70 + (_i - 1)


def _vk_code(key: str) -> int:
    normalized = key.strip().lower()
    if normalized not in VK_MAP:
        raise InputRejectedError(f"Unknown key name: {key!r}.")
    return VK_MAP[normalized]


def _send_vk(vk_code: int, *, key_up: bool = False) -> None:
    extra = ULONG_PTR(0)
    union = _Input_I()
    union.ki = _KeyBdInput(
        vk_code, 0, KEYEVENTF_KEYUP if key_up else 0, 0, ctypes.pointer(extra)
    )
    inp = _Input(ctypes.c_ulong(INPUT_KEYBOARD), union)
    if _user32().SendInput(1, ctypes.byref(inp), ctypes.sizeof(inp)) != 1:
        raise PlatformError("SendInput refused to send the keyboard event.")


def _send_unicode_unit(unit: int, *, key_up: bool = False) -> None:
    """Send one UTF-16 code unit to the focused control via SendInput."""
    extra = ULONG_PTR(0)
    flags = KEYEVENTF_UNICODE | (KEYEVENTF_KEYUP if key_up else 0)
    union = _Input_I()
    union.ki = _KeyBdInput(0, unit, flags, 0, ctypes.pointer(extra))
    inp = _Input(ctypes.c_ulong(INPUT_KEYBOARD), union)
    if _user32().SendInput(1, ctypes.byref(inp), ctypes.sizeof(inp)) != 1:
        raise PlatformError("SendInput refused to send the Unicode text event.")


def _utf16_code_units(text: str) -> list[int]:
    """Encode text as Win32's UTF-16 input units, including surrogate pairs."""
    try:
        encoded = text.encode("utf-16-le")
    except UnicodeEncodeError as exc:
        raise InputRejectedError("Text contains an invalid Unicode surrogate.") from exc
    return [
        int.from_bytes(encoded[index : index + 2], "little")
        for index in range(0, len(encoded), 2)
    ]


def _send_unicode_text(target: TargetSnapshot, text: str) -> None:
    """Send verified Unicode input without moving the shared mouse cursor."""
    for unit in _utf16_code_units(text):
        _verify_action_target(target)
        key_down = False
        try:
            _send_unicode_unit(unit)
            key_down = True
            time.sleep(TEXT_INTERCHAR_DELAY_S)
        finally:
            # A failed sleep or a later control-flow change must not leave a
            # physical Unicode key event pressed in the global input stream.
            if key_down:
                _send_unicode_unit(unit, key_up=True)
