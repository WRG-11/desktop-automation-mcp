"""Policy file validator (thin CLI wrapper).

Usage:
    py -3.12 tools/validate_policy.py <policy.yaml|policy.json>

ALL validation logic lives in the shippable
`desktop_automation_mcp.policy_file` module (it reaches the user on
install); this file only keeps the CLI exit-code contract and exposes
the `load_policy` / `validate_policy` names compatible with
`tests/test_policy_schema.py`. No PyYAML is needed for YAML parsing;
the package keeps its zero-dependency principle (standard library only).

Exit codes (deliberately distinct):
    0 — policy VALID (writes "OK").
    1 — policy INVALID (says which rule was violated).
    2 — probe/file error: file not found, unreadable, or malformed
        YAML/JSON (broken probe is NOT the same as an invalid policy).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from desktop_automation_mcp import policy_file


class PolicyFileError(Exception):
    """File could not be read or parsed (NOT an invalid policy)."""


def load_policy(path: str | Path) -> dict:
    """Read a policy file and convert it to a dict (no validation).

    JSON is tried first, then the narrow YAML subset. If neither works,
    PolicyFileError is raised (a file/parse error, not a policy denial).
    """
    try:
        return policy_file.read_policy_document(path)
    except OSError as exc:
        raise PolicyFileError(f"could not read file ({path}): {exc}") from exc
    except policy_file.PolicyParseError as exc:
        raise PolicyFileError(f"{path}: {exc}") from exc


def validate_policy(doc: object) -> list[str]:
    """Check a policy dict; return a violation list (empty = valid)."""
    return policy_file.validate_policy_document(doc)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print(
            "usage: py -3.12 tools/validate_policy.py <policy.yaml|json>",
            file=sys.stderr,
        )
        return 2
    try:
        doc = load_policy(args[0])
    except PolicyFileError as exc:
        print(f"FILE ERROR: {exc}", file=sys.stderr)
        return 2
    problems = validate_policy(doc)
    if problems:
        print(f"INVALID: {args[0]}")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print(f"OK: {args[0]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
