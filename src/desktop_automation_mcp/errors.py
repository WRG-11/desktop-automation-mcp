"""Stable machine-readable error codes for the tool's failure taxonomy.

ROADMAP asks for named failure categories ("policy_denied,
target_stale, target_occluded, target_not_foreground, input_rejected,
timeout, uia_ambiguous, platform_error, memory_budget_exceeded"), each with
"a stable machine code and a human-readable Turkish/English message". This
module answers ONE question: given that an action failed, which stable
category does it belong to? It does not decide policy, resolve targets, or emit input —
those modules raise these exceptions at the point they already detect the
failure.

Every exception here subclasses the SAME builtin exception type existing
callers already catch (`PermissionError`, `RuntimeError`, `ValueError`,
`TimeoutError`, `OSError`) so no existing `except PermissionError` /
`except RuntimeError` / etc. anywhere in the codebase (or in a caller's own
code) breaks. The `.code` class attribute is the new, additive thing: a
caller that wants the stable category can read `.code`; a caller that only
wants "was this a security denial or a Win32 failure" keeps working via
`isinstance` exactly as before.

`memory_budget_exceeded` remains a `PolicyDeniedError` subclass so existing
permission-denial handlers keep working while capacity pressure is separately
machine-readable. `uia_ambiguous` has no exception class yet — there is no UI Automation
integration until ROADMAP Faz 2; adding a class for a capability that does
not exist would be speculative, not a real taxonomy entry.
"""

from __future__ import annotations

POLICY_DENIED = "policy_denied"
TARGET_STALE = "target_stale"
TARGET_OCCLUDED = "target_occluded"
TARGET_NOT_FOREGROUND = "target_not_foreground"
INPUT_REJECTED = "input_rejected"
TIMEOUT = "timeout"
UIA_AMBIGUOUS = "uia_ambiguous"
PLATFORM_ERROR = "platform_error"
MEMORY_BUDGET_EXCEEDED = "memory_budget_exceeded"


class PolicyDeniedError(PermissionError):
    """The configured allowlist or capability policy denied the request.

    Subclassing :class:`PermissionError` preserves the public fail-closed
    contract while exposing a stable code to MCP clients.
    """

    code = POLICY_DENIED


class MemoryBudgetExceededError(PolicyDeniedError):
    """A capture was policy-valid but could not reserve the bounded,
    process-wide working-memory allowance.

    It remains a :class:`PolicyDeniedError` for backward compatibility while
    exposing a distinct machine code so audit consumers do not misclassify
    capacity pressure as an allowlist violation.
    """

    code = MEMORY_BUDGET_EXCEEDED


class TargetStaleError(RuntimeError):
    """The resolved target's identity (PID/process generation) no longer
    matches what a Win32 call at this HWND currently reports — the window
    was closed and a new one recycled the handle, or the process behind it
    restarted."""

    code = TARGET_STALE


class TargetOccludedError(RuntimeError):
    """The target is the correct window and process, but something else is
    currently drawn on top of it at the point an effect would land."""

    code = TARGET_OCCLUDED


class TargetNotForegroundError(RuntimeError):
    """The target is not the operating system's foreground window right
    now. In `passive` focus mode this is the expected rejection when the
    operator has not brought the target forward themself."""

    code = TARGET_NOT_FOREGROUND


class InputRejectedError(ValueError):
    """A caller-supplied input value (text, key name, coordinate, notch
    count, timeout, ...) failed a bound or shape check before anything was
    sent to Win32."""

    code = INPUT_REJECTED


class ActionTimeoutError(TimeoutError):
    """A bounded wait (`wait_for_window`, `wait_for_title_change`) reached
    its deadline without observing the condition it was polling for."""

    code = TIMEOUT


class PlatformError(OSError):
    """A Win32 API call itself reported failure (returned FALSE/NULL) for a
    reason unrelated to policy or target identity — a platform-level fault,
    not a security decision."""

    code = PLATFORM_ERROR
