"""Coordinate profile validator (thin CLI wrapper).

Usage:
    py -3.12 tools/validate_coordinate_profile.py <profile.json>

ALL validation logic lives in the shippable
`desktop_automation_mcp.coordinate_profile` module (it reaches the user
on install); this file only keeps the CLI exit-code contract. Profiles
are machine-produced/consumed records (hash + exact rect), so only JSON
is accepted (no YAML support as in policy files, deliberately —
rationale is in the `coordinate_profile.py` docstring).

Exit codes (deliberately distinct):
    0 — profile VALID (writes "OK").
    1 — profile INVALID (says which rule was violated).
    2 — probe/file error: file not found, unreadable, or malformed JSON
        (broken probe is NOT the same as an invalid profile).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from desktop_automation_mcp import coordinate_profile


class ProfileFileError(Exception):
    """File could not be read or parsed (NOT an invalid profile)."""


def load_profile(path: str | Path) -> dict:
    """Read a profile file and convert it to a dict (no validation, JSON only)."""
    try:
        return coordinate_profile.read_profile_document(path)
    except OSError as exc:
        raise ProfileFileError(f"could not read file ({path}): {exc}") from exc
    except coordinate_profile.ProfileParseError as exc:
        raise ProfileFileError(f"{path}: {exc}") from exc


def validate_profile(doc: object) -> list[str]:
    """Check a profile dict; return a violation list (empty = valid)."""
    return coordinate_profile.validate_profile_document(doc)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print(
            "usage: py -3.12 tools/validate_coordinate_profile.py <profile.json>",
            file=sys.stderr,
        )
        return 2
    try:
        doc = load_profile(args[0])
    except ProfileFileError as exc:
        print(f"FILE ERROR: {exc}", file=sys.stderr)
        return 2
    problems = validate_profile(doc)
    if problems:
        print(f"INVALID: {args[0]}")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print(f"OK: {args[0]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
