"""Environment-variable-based permission logic ("which window, which action?").

This module answers: which window title patterns, which executable paths,
which action classes, and which focus/text transport mode are the operator
has explicitly allowed for this MCP instance? It does NOT touch a live HWND
or process handle — that is `target.py`'s job. Every function here fails
closed: an absent or malformed environment variable denies, never grants, a
capability.
"""

from __future__ import annotations

import fnmatch
import os
from typing import NoReturn

from .errors import PolicyDeniedError
from .policy_file import load_policy_file

# A window allowlist answers "which application?".  It must not silently
# imply permission to perform every possible action in that application.
ACTION_OBSERVE = "observe"
ACTION_HOVER = "hover"
ACTION_CLICK = "click"
ACTION_TEXT = "text"
ACTION_KEY = "key"
ACTION_CLOSE = "close"
# `drag` is a SEPARATE action class from `click` because it emits a
# continuous mouse-button-down motion between a start and end point and can
# permanently change target content (reordering, drag-and-drop) — consistent
# with the `protected_actions: [text, close, drag]` example in ROADMAP §9.
ACTION_DRAG = "drag"
KNOWN_ACTIONS = frozenset(
    {
        ACTION_OBSERVE,
        ACTION_HOVER,
        ACTION_CLICK,
        ACTION_TEXT,
        ACTION_KEY,
        ACTION_CLOSE,
        ACTION_DRAG,
    }
)

FOCUS_MODE_PASSIVE = "passive"
FOCUS_MODE_ACTIVATE = "activate"

# Version-controlled policy file (`schema/policy.schema.json` format).
# When set, title/process/action decisions are read from this file; if the
# corresponding legacy environment variable is set AT THE SAME TIME, the two
# sources are NOT merged, it is a denial (ROADMAP §9). Focus/text transport
# modes have no schema counterpart; they are still read from the environment.
POLICY_FILE_ENV_VAR = "DESKTOP_AUTOMATION_POLICY_FILE"


def _policy_file_document() -> dict | None:
    """Reads the policy file and returns the validated document (or None).

    Deliberately UNCACHED: re-reads the file on every call. Rationale:
    (1) tests change the environment between calls, `lru_cache` would return
    a stale value and break tests; (2) security/testability matters more
    than micro-performance; (3) the file is KB-scale, the cost of re-reading
    per action is negligible. As a side effect, a change to the file takes
    effect on the very next action (no restart needed).
    """
    file_path = os.environ.get(POLICY_FILE_ENV_VAR, "").strip()
    if not file_path:
        return None
    return load_policy_file(file_path)


def _reject_conflicting_source(legacy_var: str) -> NoReturn:
    """Rejects a file + legacy-variable conflict by naming it."""
    raise PolicyDeniedError(
        f"{POLICY_FILE_ENV_VAR} and {legacy_var} are both set at the same time; "
        "the two policy sources are not merged — remove one."
    )


def _allowed_title_patterns() -> list[str]:
    """Default deny: only titles the operator specified can be seen."""
    doc = _policy_file_document()
    legacy = os.environ.get("DESKTOP_AUTOMATION_ALLOWED_TITLES", "")
    if doc is not None and legacy.strip():
        _reject_conflicting_source("DESKTOP_AUTOMATION_ALLOWED_TITLES")
    if doc is not None:
        return list(doc["application"]["title_patterns"])
    patterns = [
        p.strip()
        for p in os.environ.get("DESKTOP_AUTOMATION_ALLOWED_TITLES", "").split(",")
        if p.strip()
    ]
    if not patterns:
        raise PolicyDeniedError(
            "Target window policy is not configured. Add allowed title "
            "patterns to the MCP environment, e.g. "
            "DESKTOP_AUTOMATION_ALLOWED_TITLES='*Ruffle*'."
        )
    return patterns


def _is_allowed_title(title: str) -> bool:
    return any(
        fnmatch.fnmatchcase(title.casefold(), pattern.casefold())
        for pattern in _allowed_title_patterns()
    )


def _normalise_process_path(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


def _allowed_process_paths() -> set[str]:
    """Binds target identity to a full executable path alongside the title."""
    doc = _policy_file_document()
    legacy = os.environ.get("DESKTOP_AUTOMATION_ALLOWED_PROCESS_PATHS", "")
    if doc is not None and legacy.strip():
        _reject_conflicting_source("DESKTOP_AUTOMATION_ALLOWED_PROCESS_PATHS")
    if doc is not None:
        # The schema carries a SINGLE executable_path (one file = one app);
        # it is run through the SAME normalization as the env path so it can
        # be compared byte-for-byte downstream.
        return {_normalise_process_path(doc["application"]["executable_path"])}
    paths = {
        _normalise_process_path(path.strip())
        for path in os.environ.get(
            "DESKTOP_AUTOMATION_ALLOWED_PROCESS_PATHS", ""
        ).split(";")
        if path.strip()
    }
    if not paths:
        raise PolicyDeniedError(
            "Application identity policy is not configured. Add allowed "
            "full executable paths to the MCP environment, e.g. "
            "DESKTOP_AUTOMATION_ALLOWED_PROCESS_PATHS="
            "'C:\\Program Files\\ruffle\\bin\\ruffle.exe'."
        )
    return paths


def _allowed_actions() -> set[str]:
    """Returns the explicitly permitted capabilities for this MCP instance.

    An absent setting deliberately grants no capability.  This prevents an
    allowlisted window from becoming writable merely because a new tool is
    added in a later release.
    """
    doc = _policy_file_document()
    legacy = os.environ.get("DESKTOP_AUTOMATION_ALLOWED_ACTIONS", "")
    if doc is not None and legacy.strip():
        _reject_conflicting_source("DESKTOP_AUTOMATION_ALLOWED_ACTIONS")
    if doc is not None:
        # The schema enum is lowercase; casefold is applied for behavioral
        # parity with the env path (a no-op on a valid document).
        return {action.casefold() for action in doc["application"]["allowed_actions"]}
    actions = {
        action.strip().casefold()
        for action in os.environ.get("DESKTOP_AUTOMATION_ALLOWED_ACTIONS", "").split(
            ","
        )
        if action.strip()
    }
    unknown = actions - KNOWN_ACTIONS
    if unknown:
        raise PolicyDeniedError(
            "Unknown action permission: " + ", ".join(sorted(unknown)) + "."
        )
    return actions


def _protected_actions() -> set[str]:
    """Returns the per-application extra-protected actions from the policy file.

    Empty set when no policy file is active (env mode has no per-app
    protected list) or when the file omits `protected_actions`. Values are
    casefolded for parity with `_allowed_actions()`. The document was
    already schema- and subset-validated by `load_policy_file`, so no
    re-validation happens here. This set EXTENDS the fixed
    `confirmation.PROTECTED_ACTION_CLASSES` (union) — it never narrows it.
    """
    doc = _policy_file_document()
    if doc is None:
        return set()
    protected = doc["application"].get("protected_actions", [])
    return {action.casefold() for action in protected if isinstance(action, str)}


def _screenshot_constraints() -> dict:
    """Return validated screenshot privacy constraints for the active policy.

    Screenshot capture is region-denied by default. File policies must define
    at least one named `safe_regions` entry. The legacy environment policy has
    no region vocabulary, so full/cropped-window capture is available only
    through the deliberately loud compatibility opt-in
    `DESKTOP_AUTOMATION_ALLOW_UNRESTRICTED_SCREENSHOTS=true`.

    `None` therefore means "explicitly unrestricted legacy mode"; an empty
    list means "no screenshot region is allowed". Other `observe` tools keep
    working in both cases.
    """
    doc = _policy_file_document()
    if doc is None:
        raw = os.environ.get(
            "DESKTOP_AUTOMATION_ALLOW_UNRESTRICTED_SCREENSHOTS", ""
        ).strip()
        if raw.casefold() in {"", "0", "false", "no", "off"}:
            regions: list[dict] | None = []
        elif raw.casefold() in {"1", "true", "yes", "on"}:
            regions = None
        else:
            raise PolicyDeniedError(
                "DESKTOP_AUTOMATION_ALLOW_UNRESTRICTED_SCREENSHOTS may only "
                "be true/false (or 1/0, yes/no, on/off)."
            )
        return {
            "safe_regions": regions,
            "screenshot_masks": [],
            "max_screenshot_bytes": None,
        }
    app = doc["application"]
    return {
        "safe_regions": app.get("safe_regions", []),
        "screenshot_masks": list(app.get("screenshot_masks", [])),
        "max_screenshot_bytes": app.get("max_screenshot_bytes"),
    }


def _focus_mode() -> str:
    """Returns whether the server may take foreground focus.

    Passive mode is the default because foreground activation disrupts the
    operator's keyboard workflow. It is not a weaker check: the target must
    already be foreground and visibly unobscured or the request is rejected.
    """
    mode = (
        os.environ.get("DESKTOP_AUTOMATION_FOCUS_MODE", FOCUS_MODE_PASSIVE)
        .strip()
        .casefold()
    )
    if mode not in {FOCUS_MODE_PASSIVE, FOCUS_MODE_ACTIVATE}:
        raise ValueError(
            "DESKTOP_AUTOMATION_FOCUS_MODE may only be 'passive' or 'activate'."
        )
    return mode


def _require_action(action: str) -> None:
    if action not in _allowed_actions():
        raise PolicyDeniedError(
            f"Action {action!r} is not allowed for this MCP instance. "
            "Explicitly permit it in the DESKTOP_AUTOMATION_ALLOWED_ACTIONS "
            "environment variable."
        )


def _text_mode() -> str:
    """Selects the text transport; WM_CHAR is retained only for compatibility."""
    mode = os.environ.get("DESKTOP_AUTOMATION_TEXT_MODE", "unicode").strip().casefold()
    if mode not in {"unicode", "wm_char"}:
        raise ValueError(
            "DESKTOP_AUTOMATION_TEXT_MODE may only be 'unicode' or 'wm_char'."
        )
    return mode
