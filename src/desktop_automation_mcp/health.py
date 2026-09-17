"""Read-only server health: policy, dependencies, DPI awareness.

`check_health()` answers "is this instance configured and capable?"
without touching screen content: it NEVER lists windows, screenshots,
reads titles/text, or emits input. It calls only the policy readers
(their messages are pre-redacted by `policy.py`), the installed-package
metadata, and one read-only Win32 DPI query. A health check that leaked
a title or an image would violate the local-privacy principle, so the
guarantee is structural (no imports from `target.py` / `visibility.py` /
`server.py` at all) and locked by test, not just promised here.

Failure taxonomy (broken-probe vs. absent are different things):

- A policy section that was never configured (`ok: False`,
  `configured: False`) is a normal "not set up yet" state, NOT a
  broken probe.
- A policy section whose source EXISTS but fails to load (`ok: False`,
  `configured: True`) is a real failure.
- An unimportable dependency or an unqueryable DPI state is `ok: False`
  with the reason attached — never silently read as "no problem".
- `check_health()` itself never raises: a crashing health probe is
  worse than a red one, so each section is individually guarded.
"""

from __future__ import annotations

import os
import sys
from importlib import metadata as _dist_metadata

from . import platform_win32
from . import policy
from .platform_win32 import (
    PROCESS_DPI_UNAWARE,
    PROCESS_PER_MONITOR_DPI_AWARE,
    PROCESS_SYSTEM_DPI_AWARE,
)

# Version bounds mirror pyproject.toml [project].dependencies exactly
# (`mcp>=1.29.1,<2`, `Pillow>=12.3`). pyproject cannot be read at install
# time (the package ships alone), so they are pinned here; if they change,
# BOTH places are updated together. MCP 2.x renames the `FastMCP` API, so
# a floor-only check is not safe: installation would resolve but the
# server import would break.
_DEPENDENCY_BOUNDS = {
    "mcp": {"floor": "1.29.1", "ceiling_exclusive": "2"},
    "Pillow": {"floor": "12.3", "ceiling_exclusive": None},
}

# Environment-variable names are not pinned as constants in policy.py, so
# they are spelled out here (only for the "is a source present" probe).
_ENV_TITLES = "DESKTOP_AUTOMATION_ALLOWED_TITLES"
_ENV_PATHS = "DESKTOP_AUTOMATION_ALLOWED_PROCESS_PATHS"
_ENV_ACTIONS = "DESKTOP_AUTOMATION_ALLOWED_ACTIONS"

_DPI_NAMES = {
    PROCESS_DPI_UNAWARE: "unaware",
    PROCESS_SYSTEM_DPI_AWARE: "system-aware",
    PROCESS_PER_MONITOR_DPI_AWARE: "per-monitor-aware",
}


def _version_tuple(text: str) -> tuple[int, ...]:
    """'12.3.0' -> (12, 3, 0); non-numeric tails are dropped ('1.29rc1')."""
    parts: list[int] = []
    for chunk in str(text).strip().split("."):
        digits = ""
        for char in chunk:
            if char.isdigit():
                digits += char
            else:
                break
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def _version_gte(have: str, floor: str) -> bool:
    """Missing components count as zero ('12.3' == '12.3.0')."""
    have_parts = _version_tuple(have)
    floor_parts = _version_tuple(floor)
    width = max(len(have_parts), len(floor_parts))
    have_parts += (0,) * (width - len(have_parts))
    floor_parts += (0,) * (width - len(floor_parts))
    return have_parts >= floor_parts


def _version_lt(have: str, ceiling: str) -> bool:
    """Exclusive upper bound with the same missing-patch handling as `_version_gte`."""
    have_parts = _version_tuple(have)
    ceiling_parts = _version_tuple(ceiling)
    width = max(len(have_parts), len(ceiling_parts))
    have_parts += (0,) * (width - len(have_parts))
    ceiling_parts += (0,) * (width - len(ceiling_parts))
    return have_parts < ceiling_parts


def _policy_item(name: str, reader, configured: bool) -> dict:
    """Call `reader`; on failure bound the returned `detail` so the DENIED
    path carries no sensitive data.

    Independent security review F-6 (2026-09-14): when `configured=True`
    (i.e. a policy FILE is set but unreadable/corrupt), error messages
    originating from `policy_file.py` carried the operator's full file
    path (`C:\\Users\\<name>\\...`), and since `check_health()` BY DESIGN
    requires no action permission, this reached even a not-yet-authorized
    client. Messages in the `configured=False` state (`policy.py`'s own
    "unconfigured" texts) never contain a path, so they are left as-is —
    only the `configured=True` branch is redacted. Full detail goes to
    stderr (the operator can still diagnose, the MCP client cannot).
    """
    try:
        reader()
    except (PermissionError, OSError) as exc:
        if configured:
            print(
                f"[desktop-automation-mcp] health_check: {name} error: {exc}",
                file=sys.stderr,
            )
            detail = f"{name} is configured but could not be loaded (see server log for details)"
        else:
            detail = str(exc)
        return {"ok": False, "configured": configured, "detail": detail}
    except Exception as exc:  # pragma: no cover - defensive
        print(
            f"[desktop-automation-mcp] health_check: {name} unexpected error: {exc}",
            file=sys.stderr,
        )
        return {
            "ok": False,
            "configured": configured,
            "detail": f"{name}: unexpected error (see server log for details)",
        }
    return {"ok": True, "configured": True, "detail": f"{name} loaded"}


def _check_policy() -> dict:
    file_source = bool(os.environ.get(policy.POLICY_FILE_ENV_VAR, "").strip())
    items = {
        "titles": _policy_item(
            "titles",
            policy._allowed_title_patterns,
            configured=file_source or bool(os.environ.get(_ENV_TITLES, "").strip()),
        ),
        "process_paths": _policy_item(
            "process_paths",
            policy._allowed_process_paths,
            configured=file_source or bool(os.environ.get(_ENV_PATHS, "").strip()),
        ),
        "actions": _policy_item(
            "actions",
            policy._allowed_actions,
            configured=file_source or bool(os.environ.get(_ENV_ACTIONS, "").strip()),
        ),
    }
    items["ok"] = all(item["ok"] for item in items.values())
    return items


def _check_dependency(display: str, dist: str) -> dict:
    try:
        version = _dist_metadata.version(dist)
    except Exception as exc:
        return {"ok": False, "detail": f"{display} could not be imported: {exc}"}
    bounds = _DEPENDENCY_BOUNDS[dist]
    floor = bounds["floor"]
    if not _version_gte(version, floor):
        return {
            "ok": False,
            "detail": f"{display} {version} is below the floor ({floor})",
        }
    ceiling = bounds["ceiling_exclusive"]
    if ceiling is not None and not _version_lt(version, ceiling):
        return {
            "ok": False,
            "detail": f"{display} {version} is outside the ceiling (< {ceiling})",
        }
    constraint = f">= {floor}" if ceiling is None else f">= {floor}, < {ceiling}"
    return {"ok": True, "detail": f"{display} {version} ({constraint})"}


def _check_dependencies() -> dict:
    result = {
        "pillow": _check_dependency("Pillow", "Pillow"),
        "mcp": _check_dependency("mcp", "mcp"),
    }
    result["ok"] = all(item["ok"] for item in result.values())
    return result


def _check_dpi_awareness() -> dict:
    value, note = platform_win32._process_dpi_awareness()
    if value in (PROCESS_SYSTEM_DPI_AWARE, PROCESS_PER_MONITOR_DPI_AWARE):
        # Both count as "aware": the silent-coordinate-mismatch policy is
        # only violated in the UNAWARE state. Startup asks for 2
        # (per-monitor); 1 (system) is the fallback path, still healthy.
        return {
            "ok": True,
            "value": value,
            "detail": f"DPI-aware ({_DPI_NAMES[value]}, {value})",
        }
    if value == PROCESS_DPI_UNAWARE:
        return {
            "ok": False,
            "value": value,
            "detail": "NOT DPI-aware (0): GetWindowRect and screen "
            "capture may silently use different coordinate systems",
        }
    return {
        "ok": False,
        "value": None,
        "detail": f"DPI state could not be queried: {note}",
    }


def check_health() -> dict:
    """Read-only health check; NEVER collects screen content.

    Returns: {"ok": bool, "policy": {...}, "dependencies": {...},
    "dpi_awareness": {...}}. The top-level `ok` is the AND of the three
    sections. Title patterns, exe paths, window lists, images, and text
    are NOT in this dict and cannot be (they are never collected).
    """
    policy_section = _check_policy()
    dependencies = _check_dependencies()
    dpi_awareness = _check_dpi_awareness()
    return {
        "ok": bool(policy_section["ok"] and dependencies["ok"] and dpi_awareness["ok"]),
        "policy": policy_section,
        "dependencies": dependencies,
        "dpi_awareness": dpi_awareness,
    }
