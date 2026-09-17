"""Secure, window-scoped Windows desktop automation MCP server.

This module owns only the MCP tool surface (`list_windows`, `health_check`,
`get_rate_limit_state`, `get_audit_events`, `get_virtual_screen_bounds`,
`resolve_coordinate_profile_point`, `check_coordinate_profile`,
`record_coordinate_profile`,
`get_window_state`, `restore_window`, `wait_for_window`, `wait_for_title_change`,
`request_confirmation`, `preview_action`, `screenshot_window`,
`click_window`, `double_click_window`, `right_click_window`,
`hover_window`, `scroll_window`, `drag_window`, `send_text`, `send_key`,
`send_key_sequence`, `close_window`), the shared `_execute_guarded_action` lifecycle that every
targeted effect tool goes through, and `main()`. Policy decisions
live in `policy.py`, window/process identity in `target.py`, focus/z-order
re-verification in `visibility.py`, and raw input emission in `input_.py` —
this module composes them but does not duplicate their logic.
"""

from __future__ import annotations

import base64
import hashlib
import io
import os
import sys
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from typing import TypeVar

import mcp.types as types
from mcp.server.fastmcp import FastMCP
from PIL import Image, ImageDraw, ImageGrab

from .audit import OUTCOME_ALLOWED, OUTCOME_DENIED, OUTCOME_ERROR
from .audit import read_events as _read_audit_events
from .audit import record_event as _record_audit_event
from .confirmation import consume_confirmation
from .confirmation import is_protected_action, issue_confirmation
from .confirmation import preview_action as _preview_action
from .coordinate_profile import load_profile_file as _load_profile_file
from .coordinate_profile import (
    profile_matches_live_window as _profile_matches_live_window,
)
from .coordinate_profile import resolve_point as _resolve_profile_point
from .coordinate_profile import validate_profile as _validate_profile
from .health import check_health as _check_health
from .memory_budget import InFlightMemoryBudget
from .rate_limit import SlidingWindowLimiter
from .errors import (
    ActionTimeoutError,
    InputRejectedError,
    PlatformError,
    PolicyDeniedError,
    TargetNotForegroundError,
    TargetOccludedError,
    TargetStaleError,
)
from .input_ import _send_unicode_text, _send_vk, _vk_code
from .platform_win32 import (
    MOUSEEVENTF_LEFTDOWN,
    MOUSEEVENTF_LEFTUP,
    MOUSEEVENTF_MOVE,
    MOUSEEVENTF_RIGHTDOWN,
    MOUSEEVENTF_RIGHTUP,
    MOUSEEVENTF_WHEEL,
    SW_RESTORE,
    WHEEL_DELTA,
    WM_CHAR,
    WM_CLOSE,
    get_win32_adapter,
    require_process_dpi_awareness,
    virtual_screen_bounds,
)
from .policy import (
    ACTION_CLICK,
    ACTION_CLOSE,
    ACTION_DRAG,
    ACTION_HOVER,
    ACTION_KEY,
    ACTION_OBSERVE,
    ACTION_TEXT,
    _protected_actions,
    _require_action,
    _screenshot_constraints,
    _text_mode,
)
from .target import (
    TargetSnapshot,
    _current_window_snapshot,
    _enum_windows,
    _resolve_window,
    _window_diagnostic_state,
    _window_rect,
)
from .visibility import (
    _absolute_point,
    _focus_and_verify,
    _is_visibly_on_top,
    _verify_action_target,
    _verify_observable,
)


def _user32():
    return get_win32_adapter().user32


MAX_TEXT_LENGTH = 4_096
MAX_HOLD_MS = 5_000
MAX_DWELL_MS = 10_000
MAX_SCREENSHOT_PIXELS = 16_000_000
DEFAULT_MAX_SCREENSHOT_BYTES = 8 * 1_024 * 1_024
MAX_WAIT_MS = 30_000
MIN_POLL_INTERVAL_MS = 50
MAX_POLL_INTERVAL_MS = 2_000
MAX_SCROLL_NOTCHES = 20
# One drag call holds the mouse button down across every leg and is
# recorded as a SINGLE audit row — so the route is bounded: 16 waypoints
# cover real obstacle-avoidance routes (each leg costs a verify + move +
# settle ≈ 0.35 s, so the worst case stays a single few-seconds gesture),
# while an accidental giant list can never become one unbounded button-held
# sweep. Longer routes belong in separate, separately audited drags.
MAX_DRAG_WAYPOINTS = 16
# One sequence call holds the shared OS input focus for every step, and is
# recorded as a SINGLE audit row — so its size is bounded: 10 steps cover
# realistic chains (select-all, then delete, then a shortcut or two) while
# an accidental megabyte-long list can never become one unbounded input
# burst. Longer macros belong in separate, separately audited calls.
MAX_KEY_SEQUENCE_LENGTH = 10
# One sequence call holds the shared OS input focus for every step, so the
# PRODUCT is bounded too: hold_ms x steps may not exceed 10 s in a single
# call (longer holdings belong in separate, separately audited calls).
MAX_SEQUENCE_TOTAL_HOLD_MS = 10_000
# One record call captures, hashes, samples, and returns the whole profile
# in a SINGLE audit row — so both lists are bounded: 64 regions and 256
# points far exceed any real canvas map (the shipped example carries 2
# regions and 1 point), while an accidental giant list can never become
# an unbounded sampling loop or MCP response. Larger maps belong in
# separate profiles.
MAX_PROFILE_REGIONS = 64
MAX_PROFILE_POINTS = 256
DOUBLE_CLICK_GAP_S = 0.05
# Dogfooding finding (2026-09-17): picking a click_window/drag_window target
# from a screenshot required visually guessing pixel coordinates against the
# raw image. grid=True draws a light coordinate overlay so a target's (x, y)
# can be read directly instead of estimated -- purely a rendering aid, never
# changes the captured pixels' policy/permission handling and adds no new
# action permission (it stays inside `observe`).
DEFAULT_GRID_SPACING = 50
MIN_GRID_SPACING = 5
MAX_GRID_SPACING = 2_000
GRID_LINE_COLOR = (255, 0, 255, 160)
GRID_LABEL_COLOR = (255, 0, 255, 255)
# ROADMAP Phase 4: "Define performance budgets: window list, target
# resolution, ..., screenshot capture ... duration is measured. Slowness is
# not a reason to disable a security control." These four thresholds are
# for OBSERVATION only — they never skip/relax a control; exceeding one only
# prints a warning to stderr (see `_check_budget`). `FOCUS_MS` is far wider
# than the other two because `activate` mode may deliberately do 40 retries
# + 50ms waits (~2s) — that is not SLOWNESS, it's a designed retry budget;
# a narrow threshold would stamp every `activate` call as "slow" and drown
# the signal in noise.
# A narrow-policy local baseline measured listing well below this threshold.
# The margin leaves room for normal Windows jitter while making a substantial
# deviation visible. This is telemetry only, not a permission decision.
BUDGET_LIST_WINDOWS_MS = 100
BUDGET_RESOLVE_MS = 200
BUDGET_FOCUS_MS = 2_500
BUDGET_SCREENSHOT_MS = 500


def _check_budget(phase: str, elapsed_ms: int, budget_ms: int) -> None:
    """Compares a phase's duration to its budget; on overrun only prints
    to STDERR. Never raises, never skips/relaxes any security control —
    this function's only job is observation."""
    if elapsed_ms > budget_ms:
        print(
            f"[desktop-automation-mcp] performance budget exceeded: {phase} "
            f"{elapsed_ms}ms > {budget_ms}ms",
            file=sys.stderr,
        )


# Must match `_resolve_window`'s "no match at all" message byte-for-byte:
# wait_for_window should only retry on THIS condition; it must stop
# immediately on an error that waiting cannot resolve, like "multiple
# matches".
_NOT_FOUND_MESSAGE = "No target found among the allowed visible windows."


def _env_int(var_name: str, default: int) -> int:
    """Converts the environment variable to an integer; falls back to
    DEFAULT if invalid.

    Independent security review F-7 (2026-09-14): these three limits used
    to be read with a bare `int(os.environ.get(...))` while the module was
    LOADING — a single operator typo (e.g. `..._PER_MINUTE=abc`) blocked
    the module from being IMPORTED, which killed ALL 20 tools on the MCP
    server with a cryptic Python traceback (fail-closed but crude: the
    diagnosis was a stack trace instead of policy's own clear messages).
    An invalid value no longer BRINGS DOWN the server — it prints an
    explicit warning to stderr and falls back to the default; `health_check`
    does not surface this fallback either — a known, accepted limitation,
    since this function's only job is "keep the server up".
    """
    raw = os.environ.get(var_name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        print(
            f"[desktop-automation-mcp] {var_name}={raw!r} is not an integer; "
            f"using the default ({default}).",
            file=sys.stderr,
        )
        return default


def _env_bounded_int(var_name: str, default: int, *, minimum: int, maximum: int) -> int:
    """Read a bounded integer once at server startup, falling back visibly."""
    value = _env_int(var_name, default)
    if minimum <= value <= maximum:
        return value
    print(
        f"[desktop-automation-mcp] {var_name}={value!r} is outside "
        f"{minimum}..{maximum}; using the default ({default}).",
        file=sys.stderr,
    )
    return default


# ROADMAP §9 closing item: "Add a per-application rate limit for actions,
# screenshot size, and input characters." The key is the target's
# normalized `process_path` — one application's noise cannot consume
# another application's budget. The window is fixed at 60 seconds; the
# defaults are wide enough to never stress the operator's real single-user,
# single-MCP-client usage (Ruffle/Notepad demos), yet not unbounded.
MAX_ACTIONS_PER_MINUTE = _env_int("DESKTOP_AUTOMATION_MAX_ACTIONS_PER_MINUTE", 120)
MAX_TEXT_CHARS_PER_MINUTE = _env_int(
    "DESKTOP_AUTOMATION_MAX_TEXT_CHARS_PER_MINUTE", 20_000
)
MAX_SCREENSHOT_PIXELS_PER_MINUTE = _env_int(
    "DESKTOP_AUTOMATION_MAX_SCREENSHOT_PIXELS_PER_MINUTE",
    MAX_SCREENSHOT_PIXELS * 4,
)
_action_rate_limiter = SlidingWindowLimiter()
_text_rate_limiter = SlidingWindowLimiter()
_screenshot_rate_limiter = SlidingWindowLimiter()
# Read/enumeration budget: `list_windows`, `get_window_state`,
# `preview_action` and the `wait_for_*` loops hit Win32 enumeration per
# call with no budget until now — a tight polling loop burns unbounded
# EnumWindows+OpenProcess work. One unit per read call, keyed by the
# active policy source (target-less reads have no process path to key
# on). 600/min stays far above legitimate polling (a maxed-out 30 s wait
# at 50 ms polls is ~600 iterations) while bounding runaway loops.
# Self-diagnostics stay EXEMPT on purpose (`health_check`, the file-only
# profile resolve, `get_rate_limit_state`/`get_audit_events`/
# `get_virtual_screen_bounds`): diagnosing a rate denial must never
# itself be rate-denied.
MAX_READS_PER_MINUTE = _env_int("DESKTOP_AUTOMATION_MAX_READS_PER_MINUTE", 600)
_read_rate_limiter = SlidingWindowLimiter()
SCREENSHOT_MEMORY_BUDGET_BYTES = _env_bounded_int(
    "DESKTOP_AUTOMATION_SCREENSHOT_MEMORY_BUDGET_BYTES",
    128 * 1_024 * 1_024,
    minimum=1 * 1_024 * 1_024,
    maximum=2 * 1_024 * 1_024 * 1_024,
)
_screenshot_memory_budget = InFlightMemoryBudget(SCREENSHOT_MEMORY_BUDGET_BYTES)


def _reset_rate_limits_for_tests() -> None:
    """Wipe all four limiters' tracked usage. Tests only; never called by
    any MCP tool — production state persists for the server's lifetime."""
    _action_rate_limiter.reset()
    _text_rate_limiter.reset()
    _screenshot_rate_limiter.reset()
    _read_rate_limiter.reset()


def _consume_read_budget() -> None:
    """Charge one unit of the shared read/enumeration budget.

    Call AFTER `_require_action` (permission before budget, same order as
    `_prepare_action_target`). Overruns raise `PolicyDeniedError` like any
    other rate rejection.
    """
    _read_rate_limiter.consume(_policy_id(), 1, MAX_READS_PER_MINUTE)


mcp = FastMCP("desktop-automation")


def _target_identity(target: TargetSnapshot) -> dict:
    """Bridges `TargetSnapshot` to the plain triple `confirmation.py` expects.

    `confirmation.py` deliberately does not import `TargetSnapshot` (see its
    module docstring); this is the one place that translates between the
    two, so the bridge exists exactly once.
    """
    return {
        "hwnd": target.hwnd,
        "pid": target.pid,
        "process_started_at": target.process_started_at,
    }


def _requires_confirmation(action: str) -> bool:
    """Does this action class need a confirmation token right now?

    Union of the fixed `confirmation.PROTECTED_ACTION_CLASSES`
    (`text`/`close`/`drag`, always) and the active policy file's
    per-application `protected_actions` list (file mode only; env mode
    contributes nothing). The file can only ADD protection, never remove
    the built-in set. Note: an `observe` entry never gates reads —
    observation is side-effect-free by design and `list_windows` has no
    target to bind a token to; only input classes take effect.
    """
    return is_protected_action(action, _protected_actions())


def _require_confirmation_token(
    action: str, confirmation_token: str, target: TargetSnapshot
) -> None:
    """Consume the token if the action is currently confirmation-protected.

    No-op when the action needs no token (env mode, or file mode without
    the action in `protected_actions`). When protection applies, an empty
    token is `InputRejectedError` and a wrong/used/expired token is
    `PolicyDeniedError` via `consume_confirmation` — same taxonomy as the
    built-in `text`/`close`/`drag` flow.
    """
    if not _requires_confirmation(action):
        return
    if not isinstance(confirmation_token, str) or not confirmation_token.strip():
        raise InputRejectedError(
            "confirmation_token is required; first get a token via "
            f"request_confirmation(action_class={action!r}, ...)."
        )
    consume_confirmation(confirmation_token, _target_identity(target), action)


def _new_correlation_id() -> str:
    """Opaque per-call id for correlating a tool's result with future audit
    tooling (ROADMAP §9's `correlation_id` field). Carries no identity or
    content — it does not need to be predictable or reversible, only unique
    per call, so a short random hex slice is enough."""
    return uuid.uuid4().hex[:12]


def _safe_record_audit_event(**kwargs) -> None:
    """Record an audit event, but never let a FAILURE to record change the
    real control flow.

    Audit is best-effort observability, not a security control itself: the
    actual allow/deny decision has already happened by the time this is
    called. If `audit.record_event` itself raised (a full disk, a future
    durable-storage backend down, a bug in `audit.py`), letting that
    exception replace the original `PermissionError`/`TargetStaleError`/etc.
    would be strictly worse than a missing audit row — the caller's
    `except PermissionError` would stop matching, silently changing
    behaviour for a reason that has nothing to do with the security
    decision. Swallowing is intentional and printed to stderr so the
    failure is still OBSERVABLE, not silently lost (ROADMAP's own
    "mechanism exists but doesn't reach" lesson: a failure nobody can see
    is as bad as no mechanism at all).
    """
    try:
        _record_audit_event(**kwargs)
    except Exception as exc:  # noqa: BLE001 - audit must never mask the real outcome
        print(f"[desktop-automation-mcp] audit record failed: {exc}", file=sys.stderr)


def _policy_id() -> str:
    """Opaque identifier for the CURRENTLY ACTIVE policy source.

    Names *which* source is in effect (a file's basename + a short hash of
    its full path, or "env" for the legacy environment-variable path) —
    never the policy's content, and never the full path.

    Independent security review F-4b (2026-09-14): this function used to
    return the FULL file path (`f"file:{file_path}"`), and this value was
    written to EVERY audit event (`audit.py`) — since the path is typically
    under `C:\\Users\\<name>\\...`, every row carried the operator's
    username + directory structure. Now only the basename + a short hash
    of the full path is carried: it still distinguishes two files with the
    same name in different directories, but it does not leak the
    directory tree/username.
    """
    file_path = os.environ.get("DESKTOP_AUTOMATION_POLICY_FILE", "").strip()
    if not file_path:
        return "env"
    digest = hashlib.sha256(file_path.encode("utf-8")).hexdigest()[:8]
    return f"file:{os.path.basename(file_path)}#{digest}"


# Security-relevant refusals: the gate worked as designed and said no.
# (TargetStaleError/TargetNotForegroundError/TargetOccludedError are all
# RuntimeError subclasses but must be classified as denials here, not the
# `except Exception` catch-all below, which is reserved for genuine
# platform/technical faults.)
_DENIAL_EXCEPTIONS = (
    PermissionError,
    TargetStaleError,
    TargetNotForegroundError,
    TargetOccludedError,
    ValueError,
)

_ActionResult = TypeVar("_ActionResult")


def _read_foreground_hwnd():
    """Best-effort foreground read; None when the probe itself fails.

    The probe must never break the result path (a fake platform without
    the probe, a transient Win32 error) — failure reads as "unknown",
    which simply disables the post-effect witness below.
    """
    try:
        return _user32().GetForegroundWindow()
    except Exception:
        return None


def _audit_post_effect_foreground_shift(
    action: str,
    target: TargetSnapshot,
    correlation_id: str,
    start: float,
    operation: str | None,
    privacy_context: dict | None,
    fg_before,
) -> None:
    """Witness a foreground steal observed DURING this effect (TOCTOU).

    `_verify_action_target` proves foreground BEFORE emission, but a rival
    `SetForegroundWindow` can land between that check and the Win32 input
    call — two activate-mode instances racing on different windows hit
    exactly this live (2026-09-17: one's action came back `interrupted`).
    The input cannot be un-sent, so this records a second audit row (same
    correlation_id, outcome=error,
    denial_reason=post_action_foreground_mismatch) instead of failing:
    silent misdelivery becomes visible.

    Fires ONLY on an observed transition (ours right after preparation,
    someone else's right after the effect) — never on a single mismatched
    read, so foreign/mocked foreground states cannot false-positive. The
    row carries no titles, paths, or pixels (hwnds are plain numbers).
    """
    if fg_before is None or target is None:
        return
    if fg_before != target.hwnd:
        return
    fg_after = _read_foreground_hwnd()
    if fg_after is None or fg_after == target.hwnd:
        return
    _safe_record_audit_event(
        correlation_id=correlation_id,
        policy_id=_policy_id(),
        action_type=action,
        outcome=OUTCOME_ERROR,
        duration_ms=int((time.perf_counter() - start) * 1000),
        denial_reason=(
            "post_action_foreground_mismatch: effects were emitted for "
            f"hwnd={target.hwnd} after foreground verification, but "
            f"foreground is now hwnd={fg_after}; input may have reached "
            "the wrong window."
        ),
        operation=operation,
        privacy_context=privacy_context,
    )


def _prepare_action_target(
    action: str,
    title_contains: str | None,
    hwnd: int | None,
    *,
    _record_audit: bool = True,
    require_foreground: bool = True,
) -> TargetSnapshot:
    """Resolve and prove a target immediately before a UI-side effect.

    Keeping this entry gate shared prevents a new mutating MCP tool from
    accidentally omitting capability, focus, or stale-target checks. Tools may
    add action-specific validation and must revalidate again after any wait.

    Direct test/compatibility calls record the preparation outcome. Production
    effect tools pass `_record_audit=False`; `_execute_guarded_action` owns the
    complete effect outcome and the correlation id returned to the caller.

    `require_foreground=False` is used only by observation tools
    (`screenshot_window`, `check_coordinate_profile`): reading pixels does
    not steal keyboard focus or change window order, so those tools may
    observe a window the operator has not brought forward. Occlusion is
    still enforced in both modes via `_verify_observable`/`_verify_action_target`.
    """
    start = time.perf_counter()
    audit_cid = _new_correlation_id() if _record_audit else ""
    try:
        _require_action(action)
        resolve_start = time.perf_counter()
        target = _resolve_window(title_contains, hwnd)
        _check_budget(
            "target resolution",
            int((time.perf_counter() - resolve_start) * 1000),
            BUDGET_RESOLVE_MS,
        )
        _action_rate_limiter.consume(target.process_path, 1, MAX_ACTIONS_PER_MINUTE)
        if require_foreground:
            focus_start = time.perf_counter()
            _focus_and_verify(target.hwnd)
            _check_budget(
                "focus verification",
                int((time.perf_counter() - focus_start) * 1000),
                BUDGET_FOCUS_MS,
            )
        else:
            _verify_observable(target.hwnd)
        _verify_action_target(target, require_foreground=require_foreground)
    except _DENIAL_EXCEPTIONS as exc:
        if _record_audit:
            _safe_record_audit_event(
                correlation_id=audit_cid,
                policy_id=_policy_id(),
                action_type=action,
                outcome=OUTCOME_DENIED,
                duration_ms=int((time.perf_counter() - start) * 1000),
                denial_reason=str(exc),
            )
        raise
    except Exception as exc:
        if _record_audit:
            _safe_record_audit_event(
                correlation_id=audit_cid,
                policy_id=_policy_id(),
                action_type=action,
                outcome=OUTCOME_ERROR,
                duration_ms=int((time.perf_counter() - start) * 1000),
                denial_reason=str(exc),
            )
        raise
    if _record_audit:
        _safe_record_audit_event(
            correlation_id=audit_cid,
            policy_id=_policy_id(),
            action_type=action,
            outcome=OUTCOME_ALLOWED,
            duration_ms=int((time.perf_counter() - start) * 1000),
            target_identity=_target_identity(target),
        )
    return target


def _execute_guarded_action(
    action: str,
    title_contains: str | None,
    hwnd: int | None,
    effect: Callable[[TargetSnapshot, str], _ActionResult],
    *,
    operation: str | None = None,
    privacy_context: dict | None = None,
    require_foreground: bool = True,
) -> _ActionResult:
    """Run one complete guarded effect and audit its real final outcome."""
    start = time.perf_counter()
    correlation_id = _new_correlation_id()
    target: TargetSnapshot | None = None
    try:
        target = _prepare_action_target(
            action,
            title_contains,
            hwnd,
            _record_audit=False,
            require_foreground=require_foreground,
        )
        # Baseline for the post-effect TOCTOU witness: observation tools
        # (require_foreground=False) skip the probe entirely.
        fg_before = _read_foreground_hwnd() if require_foreground else None
        result = effect(target, correlation_id)
        _audit_post_effect_foreground_shift(
            action,
            target,
            correlation_id,
            start,
            operation,
            privacy_context,
            fg_before,
        )
    except _DENIAL_EXCEPTIONS as exc:
        _safe_record_audit_event(
            correlation_id=correlation_id,
            policy_id=_policy_id(),
            action_type=action,
            outcome=OUTCOME_DENIED,
            duration_ms=int((time.perf_counter() - start) * 1000),
            denial_reason=str(exc),
            operation=operation,
            privacy_context=privacy_context,
        )
        raise
    except Exception as exc:
        _safe_record_audit_event(
            correlation_id=correlation_id,
            policy_id=_policy_id(),
            action_type=action,
            outcome=OUTCOME_ERROR,
            duration_ms=int((time.perf_counter() - start) * 1000),
            denial_reason=str(exc),
            operation=operation,
            privacy_context=privacy_context,
        )
        raise
    _safe_record_audit_event(
        correlation_id=correlation_id,
        policy_id=_policy_id(),
        action_type=action,
        outcome=OUTCOME_ALLOWED,
        duration_ms=int((time.perf_counter() - start) * 1000),
        target_identity=_target_identity(target),
        operation=operation,
        privacy_context=privacy_context,
    )
    return result


@mcp.tool()
def list_windows() -> list[dict]:
    """Lists only the allowed visible windows.

    Each entry carries a `foreground` boolean (one `GetForegroundWindow`
    read per call, not per window) so callers need no follow-up
    `get_window_state` round trip to find the front window. Booleans
    only — no new content beyond what listing already discloses.
    """
    _require_action(ACTION_OBSERVE)
    _consume_read_budget()
    start = time.perf_counter()
    foreground_hwnd = _user32().GetForegroundWindow()
    windows = [
        {**window.as_public_dict(), "foreground": window.hwnd == foreground_hwnd}
        for window in _enum_windows()
    ]
    _check_budget(
        "window listing",
        int((time.perf_counter() - start) * 1000),
        BUDGET_LIST_WINDOWS_MS,
    )
    return windows


@mcp.tool()
def health_check() -> dict:
    """Checks in a read-only way whether the server is configured and
    operable (policy, dependencies, DPI-awareness).

    DELIBERATELY REQUIRES no action permission: it must be able to run
    even when policy is not configured at all (exactly the state this
    tool is meant to diagnose). Never collects screen content (title,
    image, text, window lists) — see `health.py`'s module docstring.
    """
    return _check_health()


MAX_AUDIT_EVENTS_PER_READ = 100


def _redact_audit_event_for_read(event: dict) -> dict:
    """Project a stored audit event for `get_audit_events` readers.

    The stored event is already redacted (no title/text/image/coords by
    the `audit.py` contract), but the live `target_identity`
    (hwnd/pid/process-start-time) triple is dropped here: diagnosing
    "what happened and why" needs outcome + `denial_reason` + operation +
    privacy counters, not a handle onto a live window. Everything
    returned is a copy; the ring buffer is never exposed by reference.
    """
    projected = {
        field: event[field]
        for field in (
            "timestamp",
            "correlation_id",
            "policy_id",
            "action_type",
            "outcome",
            "duration_ms",
        )
    }
    for optional in ("denial_reason", "operation", "privacy_context"):
        if optional in event:
            value = event[optional]
            projected[optional] = dict(value) if isinstance(value, dict) else value
    return projected


@mcp.tool()
def get_rate_limit_state() -> dict:
    """Reports current in-memory rate-limit and screenshot-memory usage.

    Read-only `observe`-class diagnostic: per-resource limit, live usage
    within the 60 s window, and tracked-key COUNT — never the keys
    themselves (limiter keys are application exe paths). Writes nothing,
    audits nothing (a read must not consume the budgets it reports or
    fill the log it helps diagnose).
    """
    _require_action(ACTION_OBSERVE)
    return {
        "actions_per_minute": {
            "limit": MAX_ACTIONS_PER_MINUTE,
            **_action_rate_limiter.snapshot(),
        },
        "text_chars_per_minute": {
            "limit": MAX_TEXT_CHARS_PER_MINUTE,
            **_text_rate_limiter.snapshot(),
        },
        "screenshot_pixels_per_minute": {
            "limit": MAX_SCREENSHOT_PIXELS_PER_MINUTE,
            **_screenshot_rate_limiter.snapshot(),
        },
        "screenshot_memory": {
            "limit_bytes": _screenshot_memory_budget.limit_bytes,
            "in_use_bytes": _screenshot_memory_budget.in_use,
        },
    }


@mcp.tool()
def get_audit_events(limit: int = 20) -> dict:
    """Returns the most recent redacted audit events, newest first.

    Read-only `observe`-class diagnostic over the in-memory ring buffer
    (at most `MAX_EVENTS` retained; a restart wipes history). Each event
    carries timestamp/correlation/action/outcome/duration plus
    `denial_reason` (already redaction-reviewed, numbers-only for rate
    rejections), `operation`, and numeric/boolean `privacy_context` — but
    never the live `target_identity` triple. This tool itself writes no
    audit event: reading must not fill the log it reads.
    """
    _require_action(ACTION_OBSERVE)
    if (
        not isinstance(limit, int)
        or isinstance(limit, bool)
        or not 1 <= limit <= MAX_AUDIT_EVENTS_PER_READ
    ):
        raise InputRejectedError(
            f"limit must be an integer between 1 and {MAX_AUDIT_EVENTS_PER_READ}."
        )
    retained = _read_audit_events()
    recent = retained[-limit:][::-1]
    return {
        "events": [_redact_audit_event_for_read(event) for event in recent],
        "total_retained": len(retained),
        "limit": limit,
    }


@mcp.tool()
def get_virtual_screen_bounds() -> dict:
    """Reports the live virtual-desktop bounds for multi-monitor debugging.

    Debug-only `observe`-class read over the real Win32 system metrics
    (`SM_X/YVIRTUALSCREEN`, `SM_CX/CYVIRTUALSCREEN` via
    `platform_win32.virtual_screen_bounds`): the same union rect every
    screenshot plan is validated against. `origin` is explicitly surfaced
    because a left-hand monitor makes it negative (e.g. `[-1920, 0]`) —
    the case that silently shifts captures on DPI-unaware callers. No
    per-monitor detail (no `EnumDisplayMonitors` wiring — deliberately out
    of scope for a debug read), no parameters, no audit row.
    """
    _require_action(ACTION_OBSERVE)
    left, top, right, bottom = virtual_screen_bounds()
    return {
        "bounds": [left, top, right, bottom],
        "origin": [left, top],
        "size": [right - left, bottom - top],
    }


@mcp.tool()
def resolve_coordinate_profile_point(profile_path: str, region_name: str) -> dict:
    """Resolves the center coordinate of a named region in a
    coordinate-profile file.

    Read-only, makes no Win32/screen calls — only reads the file and
    validates it against the schema (`coordinate_profile.py`). For the
    same reason as `health_check`, REQUIRES no action permission:
    inspecting a profile is not access to any window. The returned
    `(x, y)` is a window-relative RAW coordinate — this tool does not
    click automatically; pass it to `click_window`/etc. manually. Raises
    `PermissionError` if the profile is invalid or the region name is
    unknown.
    """
    profile = _load_profile_file(profile_path)
    x, y = _resolve_profile_point(profile, region_name)
    return {
        "profile_id": profile["profile_id"],
        "region_name": region_name,
        "x": x,
        "y": y,
    }


@mcp.tool()
def check_coordinate_profile(
    profile_path: str,
    title_contains: str | None = None,
    hwnd: int | None = None,
) -> dict:
    """Verifies whether a coordinate-profile matches the live window
    (window size + reference image SHA-256 hash).

    Uses the `observe` permission and goes through the SAME focus, named
    region, mask, pixel/byte/rate/memory and geometry-TOCTOU chain as
    `screenshot_window` (in `passive` mode the operator must have already
    brought the window forward). Captures only the profile's
    `reference_region_name` region; never hands the image to disk or to the
    caller, releases it after comparing the hash. Result is
    `{"matches": bool, "reason": str}`: if it does NOT match, this
    profile's `safe_regions`/`verification_points` MUST NOT BE TRUSTED —
    the application may have updated, the window may have been resized, or
    the wrong application may be running.
    """

    privacy_context: dict = {}

    def effect(target: TargetSnapshot, correlation_id: str) -> dict:
        profile = _load_profile_file(profile_path)
        profile_path_normalized = os.path.normcase(
            os.path.abspath(profile["application"]["process_path"])
        )
        target_path_normalized = os.path.normcase(os.path.abspath(target.process_path))
        if profile_path_normalized != target_path_normalized:
            raise PolicyDeniedError(
                "Coordinate profile process path does not match the live target."
            )

        reference_region_name = profile["reference_region_name"]
        profile_region = next(
            (
                region
                for region in profile["safe_regions"]
                if region["name"] == reference_region_name
            ),
            None,
        )
        if profile_region is None:
            # Normal file loading rejects this before reaching the server.
            # Keep this boundary fail-closed even for an injected/custom loader.
            raise PolicyDeniedError(
                "Coordinate profile reference region not found in the profile."
            )
        plan = _build_screenshot_plan(
            target,
            region_name=reference_region_name,
            crop_left=0,
            crop_top=0,
            crop_right=0,
            crop_bottom=0,
            require_named_region=True,
        )
        if tuple(profile_region["rect"]) != plan["relative_bounds"]:
            raise PolicyDeniedError(
                "Coordinate profile reference region does not exactly match "
                "the active policy region."
            )
        privacy_context.update(
            region_limited=plan["region_limited"],
            mask_count=plan["mask_count"],
            pixel_area=plan["pixel_area"],
        )
        with _screenshot_memory_budget.reserve(plan["reservation_bytes"]):
            live_bytes = _grab_screenshot_as_png(target, plan)
            privacy_context["png_bytes"] = len(live_bytes)
            matches, reason = _profile_matches_live_window(
                profile,
                plan["window_width"],
                plan["window_height"],
                live_bytes,
            )
        return {
            "profile_id": profile["profile_id"],
            "matches": matches,
            "reason": reason,
            "correlation_id": correlation_id,
        }

    return _execute_guarded_action(
        ACTION_OBSERVE,
        title_contains,
        hwnd,
        effect,
        operation="check_coordinate_profile",
        privacy_context=privacy_context,
        require_foreground=False,
    )


def _checked_rect(value: object, where: str) -> list[int]:
    """Validate a caller-supplied window-relative [l, t, r, b] rect.

    Caller bug (bad shape/order/negatives) is `InputRejectedError`, the
    same class as bad `crop_*`/timeout inputs — not a policy decision.
    """
    if (
        not isinstance(value, list)
        or len(value) != 4
        or not all(isinstance(v, int) and not isinstance(v, bool) for v in value)
    ):
        raise InputRejectedError(
            f"{where} must be a [left, top, right, bottom] integer quadruple."
        )
    left, top, right, bottom = value
    if not (left < right and top < bottom):
        raise InputRejectedError(f"{where} must be ordered (left<right, top<bottom).")
    if left < 0 or top < 0:
        raise InputRejectedError(f"{where} must not carry negative coordinates.")
    return value


@mcp.tool()
def record_coordinate_profile(
    profile_id: str,
    reference_region_name: str,
    safe_regions: list[dict],
    verification_points: list[list[int]],
    title_contains: str | None = None,
    hwnd: int | None = None,
    app_version: str | None = None,
) -> dict:
    """Records a coordinate profile from the live window and returns it.

    The missing producer for `resolve_coordinate_profile_point`/
    `check_coordinate_profile`: hand-writing profiles is replaced by
    sampling the real window. Captures ONLY the reference region through
    the same named-region, mask, pixel/byte/rate/memory and pre/post
    geometry-verification pipeline `check_coordinate_profile` verifies
    with (uses the `observe` permission; foreground is not required).
    `verification_points` ([x, y] raw window-relative, inside the
    reference rect) are sampled from the very PNG bytes being hashed, so
    colors and hash can never describe different pixels.

    Returns `{"profile": <complete profile dict>, "correlation_id": ...}` —
    the nested profile holds exactly the eight schema fields (`profile_id`,
    `application`, `window_size`, `reference_region_name`,
    `reference_image_hash`, `safe_regions`, `verification_points`,
    `created_at`), validated with `coordinate_profile.validate_profile`
    before returning, so `json.dump(result["profile"])` is directly
    loadable by `check_coordinate_profile`. Writes NO
    file: saving stays with the operator. `reference_region_name` must
    name exactly one entry of `safe_regions` (its rect is the hashed
    region); keep policy `screenshot_masks` disjoint from it, otherwise a
    later masked verification hashes different pixels than recorded here.
    """
    if not isinstance(profile_id, str) or not profile_id.strip():
        raise InputRejectedError("profile_id must be a non-empty string.")
    if not isinstance(reference_region_name, str) or not reference_region_name.strip():
        raise InputRejectedError("reference_region_name must be a non-empty string.")
    if not isinstance(safe_regions, list) or not safe_regions:
        raise InputRejectedError("safe_regions must be a non-empty list.")
    if len(safe_regions) > MAX_PROFILE_REGIONS:
        raise InputRejectedError(
            f"safe_regions must contain at most {MAX_PROFILE_REGIONS} regions."
        )
    checked_regions: list[dict] = []
    for pos, region in enumerate(safe_regions):
        where = f"safe_regions[{pos}]"
        if not isinstance(region, dict):
            raise InputRejectedError(f"{where} must be an object.")
        unknown = sorted(set(region) - {"name", "rect", "description"})
        if unknown:
            raise InputRejectedError(f"{where} unknown field(s): {', '.join(unknown)}.")
        name = region.get("name")
        if not isinstance(name, str) or not name.strip():
            raise InputRejectedError(f"{where}.name must be a non-empty string.")
        entry: dict = {
            "name": name,
            "rect": _checked_rect(region.get("rect"), f"{where}.rect"),
        }
        if "description" in region:
            if not isinstance(region["description"], str):
                raise InputRejectedError(f"{where}.description must be a string.")
            entry["description"] = region["description"]
        checked_regions.append(entry)
    matching = [r for r in checked_regions if r["name"] == reference_region_name]
    if len(matching) != 1:
        available = ", ".join(sorted(r["name"] for r in checked_regions)) or "(none)"
        raise InputRejectedError(
            f"reference_region_name {reference_region_name!r} must occur exactly "
            f"once inside safe_regions; available names: {available}."
        )
    ref_left, ref_top, ref_right, ref_bottom = matching[0]["rect"]
    if not isinstance(verification_points, list) or not verification_points:
        raise InputRejectedError("verification_points must be a non-empty list.")
    if len(verification_points) > MAX_PROFILE_POINTS:
        raise InputRejectedError(
            f"verification_points must contain at most {MAX_PROFILE_POINTS} points."
        )
    checked_points: list[list[int]] = []
    for pos, coords in enumerate(verification_points):
        where = f"verification_points[{pos}]"
        if (
            not isinstance(coords, list)
            or len(coords) != 2
            or not all(isinstance(v, int) and not isinstance(v, bool) for v in coords)
        ):
            raise InputRejectedError(f"{where} must be an [x, y] integer pair.")
        x, y = coords
        if not (ref_left <= x < ref_right and ref_top <= y < ref_bottom):
            raise InputRejectedError(
                f"{where} {coords} lies outside the reference region "
                f"{matching[0]['rect']}."
            )
        checked_points.append([x, y])
    if app_version is not None and (
        not isinstance(app_version, str) or not app_version
    ):
        raise InputRejectedError("app_version must be a non-empty string when given.")

    privacy_context: dict = {}

    def effect(target: TargetSnapshot, correlation_id: str) -> dict:
        plan = _build_screenshot_plan(
            target,
            region_name=None,
            crop_left=0,
            crop_top=0,
            crop_right=0,
            crop_bottom=0,
            explicit_rect=(ref_left, ref_top, ref_right, ref_bottom),
        )
        privacy_context.update(
            region_limited=True,
            mask_count=plan["mask_count"],
            pixel_area=plan["pixel_area"],
        )
        with _screenshot_memory_budget.reserve(plan["reservation_bytes"]):
            png_bytes = _grab_screenshot_as_png(target, plan)
            privacy_context["png_bytes"] = len(png_bytes)
            digest = hashlib.sha256(png_bytes).hexdigest()
            with Image.open(io.BytesIO(png_bytes)) as captured:
                rgb = captured.convert("RGB")
                sampled = [
                    {
                        "point": [x, y],
                        "expected_color": list(
                            rgb.getpixel((x - ref_left, y - ref_top))
                        ),
                    }
                    for x, y in checked_points
                ]
        profile = {
            "profile_id": profile_id,
            "application": {"process_path": target.process_path},
            "window_size": {
                "width": plan["window_width"],
                "height": plan["window_height"],
            },
            "reference_region_name": reference_region_name,
            "reference_image_hash": digest,
            "safe_regions": checked_regions,
            "verification_points": sampled,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        if app_version is not None:
            profile["application"]["app_version"] = app_version
        # Fail closed: never hand out a profile the real validator rejects.
        # The profile stays pure (exactly the eight schema fields) so it
        # can be saved as-is; the correlation id rides alongside it.
        _validate_profile(profile)
        return {"profile": profile, "correlation_id": correlation_id}

    return _execute_guarded_action(
        ACTION_OBSERVE,
        title_contains,
        hwnd,
        effect,
        operation="record_coordinate_profile",
        privacy_context=privacy_context,
        require_foreground=False,
    )


def _rect_tuple(rect) -> tuple[int, int, int, int]:
    return (rect.left, rect.top, rect.right, rect.bottom)


def _verify_window_geometry(
    target: TargetSnapshot, expected_rect: tuple[int, int, int, int]
) -> None:
    """Bind a pixel capture to the same live geometry used to plan it."""
    current = _rect_tuple(_window_rect(target.hwnd))
    if current != expected_rect:
        raise TargetStaleError(
            "Window geometry changed after the capture was planned; the "
            "action was rejected to avoid capturing neighboring screen pixels."
        )


def _build_screenshot_plan(
    target: TargetSnapshot,
    *,
    region_name: str | None,
    crop_left: int,
    crop_top: int,
    crop_right: int,
    crop_bottom: int,
    require_named_region: bool = False,
    explicit_rect: tuple[int, int, int, int] | None = None,
) -> dict:
    """Resolve one policy-bounded capture without touching screen pixels.

    `explicit_rect` is a caller-validated window-relative
    [left, top, right, bottom] used ONLY by `record_coordinate_profile`
    (profiling a region that does not exist in any policy yet); it
    bypasses the policy region/crop lookup but keeps every downstream
    gate (window fit, virtual-screen fit, pixel/byte/rate/memory
    budgets). `None` (all existing callers) behaves exactly as before.
    """
    rect = _window_rect(target.hwnd)
    expected_rect = _rect_tuple(rect)
    window_width = rect.right - rect.left
    window_height = rect.bottom - rect.top
    constraints = _screenshot_constraints()
    safe_regions = constraints["safe_regions"]
    if explicit_rect is not None:
        rel_left, rel_top, rel_right, rel_bottom = explicit_rect
        if rel_right > window_width or rel_bottom > window_height:
            raise PolicyDeniedError(
                "Explicit capture region exceeds the live window bounds."
            )
    elif safe_regions is not None:
        allowed_names = (
            ", ".join(
                sorted(
                    region["name"]
                    for region in safe_regions
                    if isinstance(region, dict) and isinstance(region.get("name"), str)
                )
            )
            or "(none)"
        )
        if not isinstance(region_name, str) or not region_name:
            raise PolicyDeniedError(
                "Policy requires an allowed screenshot region; region_name "
                "is mandatory for screenshot_window. "
                f"Allowed region_name values: {allowed_names}."
            )
        matching = [region for region in safe_regions if region["name"] == region_name]
        if len(matching) != 1:
            raise PolicyDeniedError(
                f"unknown region_name {region_name!r}; "
                f"Allowed region_name values: {allowed_names}."
            )
        rel_left, rel_top, rel_right, rel_bottom = matching[0]["rect"]
        if rel_right > window_width or rel_bottom > window_height:
            raise PolicyDeniedError(
                "Allowed screenshot region exceeds the live window bounds."
            )
    else:
        if require_named_region:
            raise PolicyDeniedError(
                "Coordinate profile verification requires a policy file "
                "with named safe_regions."
            )
        if region_name is not None:
            raise PolicyDeniedError(
                "region_name can only be used with a policy file that has safe_regions. "
                "Omit region_name and use the crop_* margins instead "
                "(e.g. all four as 0 for the full window)."
            )
        rel_left, rel_top = crop_left, crop_top
        rel_right = window_width - crop_right
        rel_bottom = window_height - crop_bottom
    left, top = rect.left + rel_left, rect.top + rel_top
    right, bottom = rect.left + rel_right, rect.top + rel_bottom
    if right <= left or bottom <= top:
        raise InputRejectedError(
            f"Crop bounds produced an invalid area for a {window_width}x{window_height} "
            f"window (need crop_left+crop_right < {window_width} and "
            f"crop_top+crop_bottom < {window_height}); reduce the crop_* margins "
            "or pass all four as 0."
        )
    virtual_left, virtual_top, virtual_right, virtual_bottom = virtual_screen_bounds()
    if (
        left < virtual_left
        or top < virtual_top
        or right > virtual_right
        or bottom > virtual_bottom
    ):
        raise PolicyDeniedError(
            "Allowed screenshot region exceeds the virtual screen bounds; "
            "the crop was not silently clamped."
        )
    pixel_area = (right - left) * (bottom - top)
    if pixel_area > MAX_SCREENSHOT_PIXELS:
        raise InputRejectedError(
            f"Screenshot area {pixel_area} px exceeds the safety limit of "
            f"{MAX_SCREENSHOT_PIXELS} px; crop further (larger crop_* margins "
            "or a smaller region_name) and retry."
        )
    _screenshot_rate_limiter.consume(
        target.process_path, pixel_area, MAX_SCREENSHOT_PIXELS_PER_MINUTE
    )
    max_output_bytes = (
        constraints["max_screenshot_bytes"] or DEFAULT_MAX_SCREENSHOT_BYTES
    )
    # Conservative peak: live BGRA pixels, BytesIO + getvalue PNG copies,
    # base64 bytes/string copies and modest encoder overhead. The returned
    # payload is separately bounded by max_output_bytes.
    reservation_bytes = pixel_area * 4 + max_output_bytes * 5
    mask_count = sum(
        1
        for mask in constraints["screenshot_masks"]
        if max(mask["rect"][0], rel_left) < min(mask["rect"][2], rel_right)
        and max(mask["rect"][1], rel_top) < min(mask["rect"][3], rel_bottom)
    )
    return {
        "expected_rect": expected_rect,
        "window_width": window_width,
        "window_height": window_height,
        "bbox": (left, top, right, bottom),
        "relative_bounds": (rel_left, rel_top, rel_right, rel_bottom),
        "masks": constraints["screenshot_masks"],
        "max_output_bytes": max_output_bytes,
        "pixel_area": pixel_area,
        "mask_count": mask_count,
        "reservation_bytes": reservation_bytes,
        "region_limited": safe_regions is not None,
    }


def _draw_coordinate_grid(image, spacing: int) -> None:
    """Draw a light, semi-transparent coordinate overlay in place.

    Purely a rendering aid over already-masked pixels: it does not read
    policy, does not touch the capture plan, and cannot reveal anything
    a mask already blacked out (lines are drawn AFTER masking). It only
    exists so a caller can read a click_window/drag_window target's
    (x, y) directly off the image instead of estimating it.
    """
    draw = ImageDraw.Draw(image, "RGBA")
    width, height = image.size
    for x in range(0, width, spacing):
        draw.line([(x, 0), (x, height)], fill=GRID_LINE_COLOR, width=1)
        draw.text((x + 2, 2), str(x), fill=GRID_LABEL_COLOR)
    for y in range(0, height, spacing):
        draw.line([(0, y), (width, y)], fill=GRID_LINE_COLOR, width=1)
        draw.text((2, y + 2), str(y), fill=GRID_LABEL_COLOR)


def _grab_screenshot_as_png(
    target: TargetSnapshot,
    plan: dict,
    *,
    grid: bool = False,
    grid_spacing: int = DEFAULT_GRID_SPACING,
) -> bytes:
    """Grab, geometry-revalidate, mask and bound one PNG in memory."""
    require_process_dpi_awareness()
    _verify_action_target(target, require_foreground=False)
    _verify_window_geometry(target, plan["expected_rect"])
    grab_start = time.perf_counter()
    image = ImageGrab.grab(bbox=plan["bbox"], all_screens=True)
    _check_budget(
        "screenshot capture",
        int((time.perf_counter() - grab_start) * 1000),
        BUDGET_SCREENSHOT_MS,
    )
    try:
        _verify_action_target(target, require_foreground=False)
        _verify_window_geometry(target, plan["expected_rect"])
        rel_left, rel_top, rel_right, rel_bottom = plan["relative_bounds"]
        for mask in plan["masks"]:
            mask_left, mask_top, mask_right, mask_bottom = mask["rect"]
            intersection = (
                max(mask_left, rel_left),
                max(mask_top, rel_top),
                min(mask_right, rel_right),
                min(mask_bottom, rel_bottom),
            )
            if intersection[0] < intersection[2] and intersection[1] < intersection[3]:
                image.paste(
                    (0, 0, 0),
                    (
                        intersection[0] - rel_left,
                        intersection[1] - rel_top,
                        intersection[2] - rel_left,
                        intersection[3] - rel_top,
                    ),
                )
        if grid:
            _draw_coordinate_grid(image, grid_spacing)
        buffer = io.BytesIO()
        try:
            image.save(buffer, format="PNG")
            png_bytes = buffer.getvalue()
            if len(png_bytes) > plan["max_output_bytes"]:
                raise PolicyDeniedError(
                    "PNG output exceeds the policy byte limit: "
                    f"{len(png_bytes)} > {plan['max_output_bytes']}."
                )
            return png_bytes
        finally:
            buffer.close()
    finally:
        image.close()


@mcp.tool()
def screenshot_window(
    title_contains: str | None = None,
    hwnd: int | None = None,
    region_name: str | None = None,
    crop_left: int = 12,
    crop_top: int = 40,
    crop_right: int = 12,
    crop_bottom: int = 12,
    grid: bool = False,
    grid_spacing: int = DEFAULT_GRID_SPACING,
) -> types.ImageContent:
    """Returns a screenshot of the verified target without writing to disk.

    If focus cannot be verified, OR another window steps in at the moment
    of capture: an error, no image taken. The second condition is ensured
    by re-verifying not just RIGHT BEFORE the capture but also AFTER it: if
    verification fails after the grab, whatever pixel data was captured
    until then is released immediately and is never encoded and returned
    to the caller.

    `grid=True` draws a light coordinate overlay (vertical/horizontal
    lines every `grid_spacing` pixels, with axis labels) so a
    click_window/drag_window target's (x, y) can be read directly off the
    image instead of estimated. Purely a rendering aid applied AFTER
    policy masks: it never changes which pixels are captured, uses no new
    action permission (stays inside `observe`), and cannot reveal
    anything a mask already blacked out.

    Coordinate contract: the returned image is CROPPED (default
    `crop_*` margins, or the named `region_name` rect in policy-file
    mode) while `click_window`/`drag_window`/`hover_window` expect RAW
    window-relative coordinates. The response `_meta` therefore carries
    `window_offset: [x, y]` (add to an image pixel to get the raw
    window-relative point) and `image_size: [w, h]` (the cropped image
    dimensions, numbers only — no titles, pixels, or paths).
    """
    if any(
        not isinstance(v, int) or isinstance(v, bool) or v < 0
        for v in (crop_left, crop_top, crop_right, crop_bottom)
    ):
        raise InputRejectedError(
            "crop_* values must be non-negative integers; pass integers >= 0 "
            "for all four (e.g. all four as 0 captures the full window)."
        )
    if not isinstance(grid, bool):
        raise InputRejectedError(
            "grid must be a boolean; pass grid=True or grid=False."
        )
    if (
        not isinstance(grid_spacing, int)
        or isinstance(grid_spacing, bool)
        or not MIN_GRID_SPACING <= grid_spacing <= MAX_GRID_SPACING
    ):
        raise InputRejectedError(
            f"grid_spacing must be an integer between {MIN_GRID_SPACING} and "
            f"{MAX_GRID_SPACING} (e.g. the default {DEFAULT_GRID_SPACING})."
        )

    privacy_context: dict = {}

    def effect(target: TargetSnapshot, correlation_id: str) -> types.ImageContent:
        plan = _build_screenshot_plan(
            target,
            region_name=region_name,
            crop_left=crop_left,
            crop_top=crop_top,
            crop_right=crop_right,
            crop_bottom=crop_bottom,
        )
        privacy_context.update(
            region_limited=plan["region_limited"],
            mask_count=plan["mask_count"],
            pixel_area=plan["pixel_area"],
        )
        with _screenshot_memory_budget.reserve(plan["reservation_bytes"]):
            png_bytes = _grab_screenshot_as_png(
                target, plan, grid=grid, grid_spacing=grid_spacing
            )
            privacy_context["png_bytes"] = len(png_bytes)
            encoded = base64.b64encode(png_bytes).decode("ascii")
        # Crop/click translation: image pixel (0, 0) is raw
        # window-relative `relative_bounds[:2]` (crop margins in env mode,
        # the named-region origin in file mode). Expose the offset and the
        # cropped size so the caller can translate without guessing the
        # `crop_*` defaults — numbers only, nothing privacy-sensitive.
        rel_left, rel_top, rel_right, rel_bottom = plan["relative_bounds"]
        return types.ImageContent(
            type="image",
            data=encoded,
            mimeType="image/png",
            _meta={
                "correlation_id": correlation_id,
                "window_offset": [rel_left, rel_top],
                "image_size": [rel_right - rel_left, rel_bottom - rel_top],
            },
        )

    return _execute_guarded_action(
        ACTION_OBSERVE,
        title_contains,
        hwnd,
        effect,
        operation="screenshot_window",
        privacy_context=privacy_context,
        require_foreground=False,
    )


@mcp.tool()
def get_window_state(hwnd: int) -> dict:
    """Reads an allowed target's live state (read-only, requires no focus).

    Unlike `_prepare_action_target`, this tool does NOT REQUIRE the window
    to be foreground/visible — its whole purpose is to give a diagnostic
    answer to "why can't this hwnd be acted on right now?": is it
    minimized, in the background, moved outside policy, or truly gone. It
    does not offer free-form search via `title_contains` — it is for
    diagnosing a known hwnd (e.g. one taken from list_windows).
    """
    _require_action(ACTION_OBSERVE)
    _consume_read_budget()
    state = _window_diagnostic_state(hwnd)
    state["foreground"] = _user32().GetForegroundWindow() == state["hwnd"]
    state["on_top"] = _is_visibly_on_top(state["hwnd"])
    state["correlation_id"] = _new_correlation_id()
    return state


@mcp.tool()
def restore_window(hwnd: int) -> str:
    """Restores a minimized (iconic) allowed window without changing focus.

    `observe`-class, hwnd-only repair for what passive mode cannot recover:
    iconic windows are invisible to title/hw­nd resolution (`_enum_windows`
    skips them) and never foreground, so no other tool can reach them. This
    tool clears iconic state via `ShowWindow(SW_RESTORE)` and re-verifies
    identity (same pid) plus the cleared state — then stops. It never calls
    the focus path (`SetForegroundWindow`/`_focus_and_verify`): a
    restored-but-background window is still rejected by the input tools'
    own passive foreground gates, so nothing is weakened. Resolution runs
    through the policy-enforcing `_window_diagnostic_state`, so an
    out-of-policy or gone hwnd is denied before any Win32 effect. Already
    visible windows are a success no-op. Like every state-changing tool,
    the outcome is audited (`operation="restore_window"`).
    """
    start = time.perf_counter()
    correlation_id = _new_correlation_id()
    try:
        _require_action(ACTION_OBSERVE)
        state = _window_diagnostic_state(hwnd)
        if not state["iconic"]:
            result = (
                f"Window is not minimized; nothing to do (hwnd={hwnd}), "
                f"correlation_id={correlation_id}."
            )
        else:
            _action_rate_limiter.consume(
                state["process_path"], 1, MAX_ACTIONS_PER_MINUTE
            )
            _user32().ShowWindow(hwnd, SW_RESTORE)
            after = _window_diagnostic_state(hwnd)
            if after["pid"] != state["pid"]:
                raise TargetStaleError(
                    "Target identity changed while restoring (HWND reused by "
                    "another process); no further action was taken."
                )
            if after["iconic"]:
                raise PlatformError(
                    "Restore did not clear the minimized state; no further "
                    "action was taken."
                )
            result = (
                f"Restored minimized window to its previous size and position "
                f"(hwnd={hwnd}); foreground was not changed, "
                f"correlation_id={correlation_id}."
            )
    except _DENIAL_EXCEPTIONS as exc:
        _safe_record_audit_event(
            correlation_id=correlation_id,
            policy_id=_policy_id(),
            action_type=ACTION_OBSERVE,
            outcome=OUTCOME_DENIED,
            duration_ms=int((time.perf_counter() - start) * 1000),
            denial_reason=str(exc),
            operation="restore_window",
        )
        raise
    except Exception as exc:
        _safe_record_audit_event(
            correlation_id=correlation_id,
            policy_id=_policy_id(),
            action_type=ACTION_OBSERVE,
            outcome=OUTCOME_ERROR,
            duration_ms=int((time.perf_counter() - start) * 1000),
            denial_reason=str(exc),
            operation="restore_window",
        )
        raise
    _safe_record_audit_event(
        correlation_id=correlation_id,
        policy_id=_policy_id(),
        action_type=ACTION_OBSERVE,
        outcome=OUTCOME_ALLOWED,
        duration_ms=int((time.perf_counter() - start) * 1000),
        operation="restore_window",
    )
    return result


@mcp.tool()
def request_confirmation(
    action_class: str,
    title_contains: str | None = None,
    hwnd: int | None = None,
    ttl_seconds: int = 60,
) -> dict:
    """Requests a single-use, short-lived confirmation token for a
    protected action (`text`, `close`, `drag`, plus any extra action class
    the active policy file lists under `protected_actions`).

    The target is FULLY verified via `_prepare_action_target` before the
    token is issued (policy + focus + action-moment identity) — so a
    token CANNOT be obtained for an already-unauthorized or non-visible
    target. The token can only be used for THIS target's (hwnd/pid/
    process-start-time) THIS action class; pass the returned `token_id`
    to the matching effect tool as `confirmation_token`.
    """

    def effect(target: TargetSnapshot, correlation_id: str) -> dict:
        # Call-shape compatibility: without per-file extras the call stays
        # exactly `(identity, action_class, ttl_seconds)` as before; the
        # fourth argument is only present when the file actually extends
        # the protected set.
        extra = _protected_actions()
        if extra:
            token = issue_confirmation(
                _target_identity(target), action_class, ttl_seconds, extra
            )
        else:
            token = issue_confirmation(
                _target_identity(target), action_class, ttl_seconds
            )
        token["correlation_id"] = correlation_id
        return token

    return _execute_guarded_action(action_class, title_contains, hwnd, effect)


@mcp.tool()
def preview_action(
    action_class: str,
    title_contains: str | None = None,
    hwnd: int | None = None,
) -> dict:
    """Summarizes, without any side effect, what state an action is
    currently in on the target: is policy permission present, is
    confirmation required, does a valid token already exist.

    Does NOT USE `_prepare_action_target` — requires no focus/foreground,
    purely informational. It still resolves via `_window_diagnostic_state`
    to verify the target is within the allowed policy scope.
    """
    _require_action(ACTION_OBSERVE)
    _consume_read_budget()
    if hwnd is None:
        target = _resolve_window(title_contains, None)
    else:
        state = _window_diagnostic_state(hwnd)
        target = TargetSnapshot(
            hwnd=state["hwnd"],
            title=state["title"],
            pid=state["pid"],
            rect=tuple(state["rect"]),
            process_path=state["process_path"],
            window_class=state["window_class"],
        )
    result = _preview_action(
        action_class, _target_identity(target), _protected_actions()
    )
    result["correlation_id"] = _new_correlation_id()
    return result


@mcp.tool()
def wait_for_window(
    title_contains: str,
    timeout_ms: int = 5_000,
    poll_interval_ms: int = 200,
) -> dict:
    """Waits, with a bounded duration, until an allowed window appears
    with this title.

    Mandatory, upper-bounded timeout: no infinite wait, never hangs the
    MCP client. If multiple allowed windows match (a permanent ambiguity,
    unresolvable by waiting) it raises immediately; it only retries on
    "not visible yet".
    """
    _require_action(ACTION_OBSERVE)
    _consume_read_budget()
    if not title_contains or not title_contains.strip():
        raise InputRejectedError("title_contains must not be empty.")
    if not isinstance(timeout_ms, int) or not 0 < timeout_ms <= MAX_WAIT_MS:
        raise InputRejectedError(f"timeout_ms must be between 1 and {MAX_WAIT_MS}.")
    if (
        not isinstance(poll_interval_ms, int)
        or not MIN_POLL_INTERVAL_MS <= poll_interval_ms <= MAX_POLL_INTERVAL_MS
    ):
        raise InputRejectedError(
            f"poll_interval_ms must be between {MIN_POLL_INTERVAL_MS} and "
            f"{MAX_POLL_INTERVAL_MS}."
        )
    deadline = time.monotonic() + timeout_ms / 1000
    while True:
        try:
            found = _resolve_window(title_contains, None).as_public_dict()
            found["correlation_id"] = _new_correlation_id()
            return found
        except ValueError as exc:
            if str(exc) != _NOT_FOUND_MESSAGE:
                raise
        if time.monotonic() >= deadline:
            raise ActionTimeoutError(
                f"No allowed target appeared within {timeout_ms} ms: {title_contains!r}."
            )
        time.sleep(poll_interval_ms / 1000)


@mcp.tool()
def wait_for_title_change(
    hwnd: int,
    timeout_ms: int = 5_000,
    poll_interval_ms: int = 200,
) -> dict:
    """Waits, with a bounded duration, until the target's title changes
    (read-only).

    If the target stops being visible/allowed while waiting (closed,
    minimized, moved outside policy), raises immediately instead of
    waiting forever.
    """
    _require_action(ACTION_OBSERVE)
    _consume_read_budget()
    if not isinstance(timeout_ms, int) or not 0 < timeout_ms <= MAX_WAIT_MS:
        raise InputRejectedError(f"timeout_ms must be between 1 and {MAX_WAIT_MS}.")
    if (
        not isinstance(poll_interval_ms, int)
        or not MIN_POLL_INTERVAL_MS <= poll_interval_ms <= MAX_POLL_INTERVAL_MS
    ):
        raise InputRejectedError(
            f"poll_interval_ms must be between {MIN_POLL_INTERVAL_MS} and "
            f"{MAX_POLL_INTERVAL_MS}."
        )
    initial = _current_window_snapshot(hwnd)
    if initial is None:
        # A caller-supplied hwnd that never resolves is an input problem
        # (distinct from `TargetStaleError` below, which is the target
        # disappearing mid-wait after having resolved once).
        raise InputRejectedError(f"hwnd={hwnd} is not visible/allowed.")
    deadline = time.monotonic() + timeout_ms / 1000
    while True:
        current = _current_window_snapshot(hwnd)
        if current is None:
            raise TargetStaleError(
                f"hwnd={hwnd} stopped being visible/allowed while waiting."
            )
        if current.title != initial.title:
            result = current.as_public_dict()
            result["correlation_id"] = _new_correlation_id()
            return result
        if time.monotonic() >= deadline:
            raise ActionTimeoutError(
                f"Title did not change within {timeout_ms} ms (current: {current.title!r})."
            )
        time.sleep(poll_interval_ms / 1000)


def _move_cursor_into_window(target: TargetSnapshot, x: int, y: int) -> None:
    """Resolve a window-relative point, verify, and move the shared cursor."""
    abs_x, abs_y = _absolute_point(target.hwnd, x, y)
    _verify_action_target(target)
    if not _user32().SetCursorPos(abs_x, abs_y):
        raise PlatformError("Could not move the mouse to the target position.")
    _user32().mouse_event(MOUSEEVENTF_MOVE, 0, 0, 0, 0)


def _press_release(
    target: TargetSnapshot, down_flag: int, up_flag: int, *, hold_s: float = 0.05
) -> None:
    """Emit one mouse-button press+release with a guaranteed release.

    A `finally` releases the button even if the target disappears or Win32
    raises after button-down — every caller (`click_window`,
    `double_click_window`, `right_click_window`) shares this one release
    path so none of them can accidentally omit it.
    """
    _verify_action_target(target)
    pressed = False
    try:
        _user32().mouse_event(down_flag, 0, 0, 0, 0)
        pressed = True
        time.sleep(hold_s)
    finally:
        if pressed:
            _user32().mouse_event(up_flag, 0, 0, 0, 0)


@mcp.tool()
def click_window(
    title_contains: str | None = None,
    hwnd: int | None = None,
    x: int = 0,
    y: int = 0,
    confirmation_token: str = "",
) -> str:
    """Clicks a window-relative position within the verified window's bounds.

    When the active policy file lists `click` under `protected_actions`,
    a valid `confirmation_token` (from `request_confirmation(
    action_class="click", ...)`) is required; otherwise the token is
    ignored.
    """

    def effect(target: TargetSnapshot, correlation_id: str) -> str:
        _require_confirmation_token(ACTION_CLICK, confirmation_token, target)
        _move_cursor_into_window(target, x, y)
        time.sleep(0.15)
        _press_release(target, MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP)
        return (
            f"Clicked: window-relative ({x}, {y}), hwnd={target.hwnd}, "
            f"correlation_id={correlation_id}."
        )

    return _execute_guarded_action(ACTION_CLICK, title_contains, hwnd, effect)


@mcp.tool()
def double_click_window(
    title_contains: str | None = None,
    hwnd: int | None = None,
    x: int = 0,
    y: int = 0,
    confirmation_token: str = "",
) -> str:
    """Sends two quick consecutive left clicks to a position within the verified window.

    Same `click`-class confirmation rule as `click_window`: required only
    when the policy file protects `click`.
    """

    def effect(target: TargetSnapshot, correlation_id: str) -> str:
        _require_confirmation_token(ACTION_CLICK, confirmation_token, target)
        _move_cursor_into_window(target, x, y)
        time.sleep(0.15)
        _press_release(target, MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP)
        time.sleep(DOUBLE_CLICK_GAP_S)
        _press_release(target, MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP)
        return (
            f"Double-clicked: window-relative ({x}, {y}), hwnd={target.hwnd}, "
            f"correlation_id={correlation_id}."
        )

    return _execute_guarded_action(ACTION_CLICK, title_contains, hwnd, effect)


@mcp.tool()
def right_click_window(
    title_contains: str | None = None,
    hwnd: int | None = None,
    x: int = 0,
    y: int = 0,
    confirmation_token: str = "",
) -> str:
    """Sends a right click to a position within the verified window (e.g. context menu).

    Same `click`-class confirmation rule as `click_window`: required only
    when the policy file protects `click`.
    """

    def effect(target: TargetSnapshot, correlation_id: str) -> str:
        _require_confirmation_token(ACTION_CLICK, confirmation_token, target)
        _move_cursor_into_window(target, x, y)
        time.sleep(0.15)
        _press_release(target, MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP)
        return (
            f"Right-clicked: window-relative ({x}, {y}), hwnd={target.hwnd}, "
            f"correlation_id={correlation_id}."
        )

    return _execute_guarded_action(ACTION_CLICK, title_contains, hwnd, effect)


@mcp.tool()
def hover_window(
    title_contains: str | None = None,
    hwnd: int | None = None,
    x: int = 0,
    y: int = 0,
    dwell_ms: int = 400,
    confirmation_token: str = "",
) -> str:
    """Moves the mouse within the verified window without clicking.

    When the active policy file lists `hover` under `protected_actions`,
    a valid `confirmation_token` (from `request_confirmation(
    action_class="hover", ...)`) is required; otherwise the token is
    ignored.
    """
    if not isinstance(dwell_ms, int) or not 0 <= dwell_ms <= MAX_DWELL_MS:
        raise InputRejectedError(f"dwell_ms must be between 0 and {MAX_DWELL_MS}.")

    def effect(target: TargetSnapshot, correlation_id: str) -> str:
        _require_confirmation_token(ACTION_HOVER, confirmation_token, target)
        abs_x, abs_y = _absolute_point(target.hwnd, x, y)
        _verify_action_target(target)
        if not _user32().SetCursorPos(abs_x, abs_y):
            raise PlatformError("Could not move the mouse to the target position.")
        _user32().mouse_event(MOUSEEVENTF_MOVE, 0, 0, 0, 0)
        time.sleep(dwell_ms / 1000)
        return (
            f"Hover complete: window-relative ({x}, {y}), hwnd={target.hwnd}, "
            f"correlation_id={correlation_id}."
        )

    return _execute_guarded_action(ACTION_HOVER, title_contains, hwnd, effect)


@mcp.tool()
def scroll_window(
    title_contains: str | None = None,
    hwnd: int | None = None,
    x: int = 0,
    y: int = 0,
    notches: int = 1,
    confirmation_token: str = "",
) -> str:
    """Sends a mouse-wheel event at a position within the verified window.

    Wheel convention, spelled out: one `notches` unit is one physical
    wheel detent as defined by Win32 — `MOUSEEVENTF_WHEEL` with
    `WHEEL_DELTA`=120, i.e. this tool sends `notches * 120` as the wheel
    delta. The 120 is an OS constant (the amount Windows treats as a full
    detent), not a speed knob on this tool: `notches=3` is three detents,
    not "3x faster". Positive scrolls up, negative scrolls down; zero is
    rejected, and the magnitude is bounded to ±`MAX_SCROLL_NOTCHES` so one
    call cannot fling a page/list unexpectedly far.

    Vertical wheel only: there is deliberately NO horizontal axis (Win32
    `MOUSEEVENTF_HWHEEL` is not wired). Horizontal scrolling changes a
    different scroll dimension and would need its own permission/decision;
    it is not smuggled in through the sign of `notches`.

    Uses the `click` permission; when the policy file protects `click`, a
    valid `confirmation_token` is required (same rule as `click_window`).
    """
    if (
        not isinstance(notches, int)
        or notches == 0
        or not -MAX_SCROLL_NOTCHES <= notches <= MAX_SCROLL_NOTCHES
    ):
        raise InputRejectedError(
            "notches cannot be zero and must be between "
            f"-{MAX_SCROLL_NOTCHES} and {MAX_SCROLL_NOTCHES}."
        )

    def effect(target: TargetSnapshot, correlation_id: str) -> str:
        _require_confirmation_token(ACTION_CLICK, confirmation_token, target)
        _move_cursor_into_window(target, x, y)
        _verify_action_target(target)
        _user32().mouse_event(MOUSEEVENTF_WHEEL, 0, 0, notches * WHEEL_DELTA, 0)
        return (
            f"Scrolled: window-relative ({x}, {y}), {notches} notch(es), "
            f"hwnd={target.hwnd}, correlation_id={correlation_id}."
        )

    return _execute_guarded_action(ACTION_CLICK, title_contains, hwnd, effect)


@mcp.tool()
def drag_window(
    title_contains: str | None = None,
    hwnd: int | None = None,
    start_x: int = 0,
    start_y: int = 0,
    end_x: int = 0,
    end_y: int = 0,
    confirmation_token: str = "",
    path: list[list[int]] | None = None,
) -> str:
    """Sends a drag from start to end within the verified window.

    `drag` is a SEPARATE action class from `click` because it can
    permanently change target content (reordering, drag-and-drop). Start
    AND end point pass window-bounds checking INDEPENDENTLY of each other;
    the mouse button is always released via `finally` on the error path.

    Protected class like `close`/`text`: requires a valid, unconsumed
    `confirmation_token` obtained beforehand via `request_confirmation(
    action_class="drag", ...)`.

    `path` is an optional list of intermediate window-relative `[x, y]`
    waypoints traversed in order between start and end with the button
    held throughout (for routes around an obstacle that a straight line
    cannot model). Omitted (or empty) means the legacy straight drag.
    Every waypoint passes the same independent bounds check as start/end;
    all points resolve BEFORE anything is emitted, so a bad waypoint
    fails with zero side effects.
    """
    # Shape/length checks run BEFORE target resolution and token consume
    # (same order as `send_key_sequence`): a rejected overlong/malformed
    # `path` burns neither the caller's single-use token nor any mouse
    # effect. Window-bounds resolution stays inside `effect` (it needs the
    # live target rect).
    waypoints = _checked_drag_path(path)
    if len(waypoints) > MAX_DRAG_WAYPOINTS:
        raise InputRejectedError(
            f"path must contain at most {MAX_DRAG_WAYPOINTS} waypoints."
        )

    def effect(target: TargetSnapshot, correlation_id: str) -> str:
        if not isinstance(confirmation_token, str) or not confirmation_token.strip():
            raise InputRejectedError(
                "confirmation_token is required; first get a token via "
                "request_confirmation(action_class='drag', ...)."
            )
        consume_confirmation(confirmation_token, _target_identity(target), ACTION_DRAG)
        start_abs = _absolute_point(target.hwnd, start_x, start_y)
        end_abs = _absolute_point(target.hwnd, end_x, end_y)
        waypoint_abs = [_absolute_point(target.hwnd, x, y) for x, y in waypoints]
        _verify_action_target(target)
        if not _user32().SetCursorPos(*start_abs):
            raise PlatformError("Could not move the mouse to the start position.")
        _user32().mouse_event(MOUSEEVENTF_MOVE, 0, 0, 0, 0)
        time.sleep(0.15)
        mouse_down = False
        try:
            _verify_action_target(target)
            _user32().mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
            mouse_down = True
            time.sleep(0.05)
            legs = [("a path point", point) for point in waypoint_abs]
            legs.append(("the end position", end_abs))
            for label, point in legs:
                _verify_action_target(target)
                if not _user32().SetCursorPos(*point):
                    raise PlatformError(f"Could not move the mouse to {label}.")
                _user32().mouse_event(MOUSEEVENTF_MOVE, 0, 0, 0, 0)
                time.sleep(0.15)
            _verify_action_target(target)
        finally:
            # Never leave the physical mouse button held if the target disappears
            # or an unexpected Win32 failure occurs mid-drag.
            if mouse_down:
                _user32().mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
        route = f"({start_x}, {start_y}) -> ({end_x}, {end_y})" + (
            f" via {len(waypoints)} waypoint(s)" if waypoints else ""
        )
        return (
            f"Dragged: window-relative {route}, hwnd={target.hwnd}, "
            f"correlation_id={correlation_id}."
        )

    return _execute_guarded_action(ACTION_DRAG, title_contains, hwnd, effect)


@mcp.tool()
def send_text(
    title_contains: str | None = None,
    hwnd: int | None = None,
    text: str = "",
    confirmation_token: str = "",
) -> str:
    """Sends plain text input events to the verified target.

    `text` is a protected action class: requires a STILL-valid, unconsumed
    `confirmation_token` obtained beforehand via `request_confirmation(
    action_class="text", ...)`. The token is bound to the target's
    action-moment identity (hwnd/pid/process-start-time) and cannot be
    used again once consumed. A successful return proves the Win32 input
    events were sent; it does not prove that a specific editor in the
    target application accepted the text and displayed it on screen. The
    caller should perform a separate UI observation if needed.
    """
    if (
        not isinstance(text, str)
        or not text
        or "\x00" in text
        or len(text) > MAX_TEXT_LENGTH
    ):
        raise InputRejectedError(
            f"Text must not be empty/contain NUL and may be at most "
            f"{MAX_TEXT_LENGTH} characters."
        )

    def effect(target: TargetSnapshot, correlation_id: str) -> str:
        _text_rate_limiter.consume(
            target.process_path, len(text), MAX_TEXT_CHARS_PER_MINUTE
        )
        if not isinstance(confirmation_token, str) or not confirmation_token.strip():
            raise InputRejectedError(
                "confirmation_token is required; first get a token via "
                "request_confirmation(action_class='text', ...)."
            )
        consume_confirmation(confirmation_token, _target_identity(target), ACTION_TEXT)
        if _text_mode() == "unicode":
            _send_unicode_text(target, text)
            return (
                f"Text input events sent; visibility in the target control "
                f"was not verified ({len(text)} characters), hwnd={target.hwnd}, "
                f"correlation_id={correlation_id}."
            )
        for char in text:
            _verify_action_target(target)
            if not _user32().PostMessageW(target.hwnd, WM_CHAR, ord(char), 0):
                raise PlatformError("Could not send the WM_CHAR text event.")
            time.sleep(0.01)
        return (
            f"Text input events sent; visibility in the target control "
            f"was not verified ({len(text)} characters), hwnd={target.hwnd}, "
            f"correlation_id={correlation_id}."
        )

    return _execute_guarded_action(ACTION_TEXT, title_contains, hwnd, effect)


@mcp.tool()
def send_key(
    title_contains: str | None = None,
    hwnd: int | None = None,
    key: str = "",
    modifiers: list[str] | None = None,
    hold_ms: int = 80,
    confirmation_token: str = "",
) -> str:
    """Sends an OS-level key/shortcut event to the verified target.

    When the active policy file lists `key` under `protected_actions`,
    a valid `confirmation_token` (from `request_confirmation(
    action_class="key", ...)`) is required; otherwise the token is
    ignored.
    """
    if not isinstance(hold_ms, int) or not 0 <= hold_ms <= MAX_HOLD_MS:
        raise InputRejectedError(f"hold_ms must be between 0 and {MAX_HOLD_MS}.")
    if modifiers is not None and (
        not isinstance(modifiers, list) or len(modifiers) > 2
    ):
        raise InputRejectedError("modifiers must be a list of at most two keys.")

    def effect(target: TargetSnapshot, correlation_id: str) -> str:
        _require_confirmation_token(ACTION_KEY, confirmation_token, target)
        key_code = _vk_code(key)
        modifier_codes = [_vk_code(modifier) for modifier in (modifiers or [])]
        pressed: list[int] = []
        try:
            _verify_action_target(target)
            for code in modifier_codes:
                _send_vk(code)
                pressed.append(code)
            _send_vk(key_code)
            pressed.append(key_code)
            time.sleep(hold_ms / 1000)
        finally:
            for code in reversed(pressed):
                _send_vk(code, key_up=True)
        return (
            f"Key input events sent; target behavior was not verified "
            f"({' + '.join([*(modifiers or []), key])}), hwnd={target.hwnd}, "
            f"correlation_id={correlation_id}."
        )

    return _execute_guarded_action(ACTION_KEY, title_contains, hwnd, effect)


def _checked_key_step(value: object, where: str) -> tuple[str, list[str]]:
    """Validate one sequence step's shape (names resolve later via `_vk_code`).

    Caller bug (non-object, missing/empty key, bad modifiers, unknown
    fields) is `InputRejectedError`, the same class as `send_key`'s own
    modifiers check — not a policy decision.
    """
    if not isinstance(value, dict):
        raise InputRejectedError(
            f"{where} must be an object like {{'key': 'a', 'modifiers': ['ctrl']}}."
        )
    unknown = sorted(set(value) - {"key", "modifiers"})
    if unknown:
        raise InputRejectedError(f"{where} unknown field(s): {', '.join(unknown)}.")
    key = value.get("key")
    if not isinstance(key, str) or not key.strip():
        raise InputRejectedError(f"{where}.key must be a non-empty key name.")
    modifiers = value.get("modifiers", [])
    if not isinstance(modifiers, list) or len(modifiers) > 2:
        raise InputRejectedError(
            f"{where}.modifiers must be a list of at most two keys."
        )
    for modifier in modifiers:
        if not isinstance(modifier, str) or not modifier.strip():
            raise InputRejectedError(
                f"{where}.modifiers must contain only non-empty key names."
            )
    return key, list(modifiers)


def _checked_drag_path(value: object) -> list[list[int]]:
    """Validate an optional drag waypoint list (shape only, not bounds).

    Caller bug (non-list, malformed pair) is `InputRejectedError`, the
    same class as bad `crop_*`/timeout inputs. Window-bounds checking
    happens separately per waypoint via `_absolute_point`, exactly like
    start/end. `None` (and `[]`) means the legacy straight drag.
    """
    if value is None:
        return []
    if not isinstance(value, list):
        raise InputRejectedError("path must be a list of [x, y] waypoints or None.")
    for pos, point in enumerate(value):
        if (
            not isinstance(point, list)
            or len(point) != 2
            or not all(isinstance(v, int) and not isinstance(v, bool) for v in point)
        ):
            raise InputRejectedError(f"path[{pos}] must be an [x, y] integer pair.")
    return [list(point) for point in value]


@mcp.tool()
def send_key_sequence(
    keys: list[dict],
    title_contains: str | None = None,
    hwnd: int | None = None,
    hold_ms: int = 80,
    confirmation_token: str = "",
) -> str:
    """Sends several OS-level key/shortcut steps in one guarded call.

    Each step is `{"key": <name>, "modifiers": [...]}` — the same shape as
    `send_key`'s `key`+`modifiers`; `hold_ms` applies to every step. At
    most `MAX_KEY_SEQUENCE_LENGTH` steps (`InputRejectedError` beyond
    it). Same `key`-class permission and `confirmation_token` rules as
    `send_key` (one token covers the whole sequence). Every step is
    press-hold-release on its own, the target is re-verified before each
    step, and a mid-sequence failure releases EVERYTHING pressed so far
    via nested `finally` — no key is ever left held.
    """
    if not isinstance(hold_ms, int) or not 0 <= hold_ms <= MAX_HOLD_MS:
        raise InputRejectedError(f"hold_ms must be between 0 and {MAX_HOLD_MS}.")
    if not isinstance(keys, list) or not keys:
        raise InputRejectedError("keys must be a non-empty list of key steps.")
    if len(keys) > MAX_KEY_SEQUENCE_LENGTH:
        raise InputRejectedError(
            f"keys must contain at most {MAX_KEY_SEQUENCE_LENGTH} steps."
        )
    if hold_ms * len(keys) > MAX_SEQUENCE_TOTAL_HOLD_MS:
        raise InputRejectedError(
            f"hold_ms x steps must total at most {MAX_SEQUENCE_TOTAL_HOLD_MS} ms "
            "in one call; split longer holdings across calls."
        )
    checked = [_checked_key_step(step, f"keys[{pos}]") for pos, step in enumerate(keys)]
    # Resolve ALL names BEFORE target resolution and token consume (same
    # order as the shape checks above): `_vk_code` is a pure table lookup
    # with no Win32 calls, so an unknown key in step 5 fails here with
    # zero side effects — neither emissions nor a burned single-use token.
    plan = [
        (
            _vk_code(key_name),
            [_vk_code(modifier) for modifier in modifiers],
            " + ".join([*modifiers, key_name]),
        )
        for key_name, modifiers in checked
    ]

    def effect(target: TargetSnapshot, correlation_id: str) -> str:
        _require_confirmation_token(ACTION_KEY, confirmation_token, target)
        pressed: list[int] = []
        try:
            for key_code, modifier_codes, _label in plan:
                _verify_action_target(target)
                step_pressed: list[int] = []
                try:
                    for code in (*modifier_codes, key_code):
                        _send_vk(code)
                        step_pressed.append(code)
                        pressed.append(code)
                    time.sleep(hold_ms / 1000)
                finally:
                    for code in reversed(step_pressed):
                        _send_vk(code, key_up=True)
                        pressed.remove(code)
        finally:
            # Only non-empty if a release above raised: retry it rather
            # than leaving a physical key held in the shared input stream.
            for code in reversed(pressed):
                _send_vk(code, key_up=True)
        return (
            f"Key sequence sent ({len(plan)} step(s)); target behavior was "
            f"not verified ({'; '.join(label for _, _, label in plan)}), "
            f"hwnd={target.hwnd}, correlation_id={correlation_id}."
        )

    return _execute_guarded_action(ACTION_KEY, title_contains, hwnd, effect)


@mcp.tool()
def close_window(
    title_contains: str | None = None,
    hwnd: int | None = None,
    confirmation_token: str = "",
) -> str:
    """Sends a normal WM_CLOSE request to the verified allowed window.

    `close` is a protected action class: requires a valid, unconsumed
    `confirmation_token` obtained beforehand via `request_confirmation(
    action_class="close", ...)` (see `send_text`'s docstring — same flow).
    """

    def effect(target: TargetSnapshot, correlation_id: str) -> str:
        if not isinstance(confirmation_token, str) or not confirmation_token.strip():
            raise InputRejectedError(
                "confirmation_token is required; first get a token via "
                "request_confirmation(action_class='close', ...)."
            )
        consume_confirmation(confirmation_token, _target_identity(target), ACTION_CLOSE)
        _verify_action_target(target)
        if not _user32().PostMessageW(target.hwnd, WM_CLOSE, 0, 0):
            raise PlatformError("Could not send the WM_CLOSE request.")
        return (
            f"Close request sent, hwnd={target.hwnd}, correlation_id={correlation_id}."
        )

    return _execute_guarded_action(ACTION_CLOSE, title_contains, hwnd, effect)


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
