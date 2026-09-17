"""Confirmation token validator (Phase 3 design draft).

Usage:
    py -3.12 tools/validate_confirmation_token.py <token.json>

What it does: enforces the rules in schema/confirmation_token.schema.json
(without a JSON Schema library; standard library only). Tokens and audit
events are machine-produced/consumed JSON records, so only JSON is
accepted here (no YAML support as in policy files, deliberately —
rationale is in schema/README.md).

Exit codes (same contract as tools/validate_policy.py):
    0 — token VALID (writes "OK").
    1 — token INVALID (says which rule was violated).
    2 — probe/file error: file not found, unreadable, or malformed JSON
        (broken probe is NOT the same as an invalid token).
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _iso8601 import Iso8601Error, is_past, parse_offset_aware

# Protected action classes: text, close, and drag. Must match the
# `action_class` enum in the schema exactly; when the schema widens,
# this set is updated together with it.
PROTECTED_ACTIONS = frozenset({"text", "close", "drag"})

TOKEN_FIELDS = frozenset(
    {
        "token_id",
        "target_identity",
        "action_class",
        "issued_at",
        "expires_at",
        "single_use",
        "consumed_at",
    }
)

IDENTITY_FIELDS = frozenset({"hwnd", "pid", "process_started_at"})

UUID4_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-4[0-9a-fA-F]{3}"
    r"-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$"
)


class TokenFileError(Exception):
    """File could not be read or parsed (NOT an invalid token)."""


def load_token(path: str | Path) -> dict:
    """Read a token file and convert it to a dict (JSON only)."""
    candidate = Path(path)
    if not candidate.exists():
        raise TokenFileError(f"file not found: {candidate}")
    if not candidate.is_file():
        raise TokenFileError(f"token path is not a file: {candidate}")
    try:
        text = candidate.read_text(encoding="utf-8")
    except OSError as exc:
        raise TokenFileError(f"could not read file ({candidate}): {exc}") from exc
    if text.strip() == "":
        raise TokenFileError(f"file is empty: {candidate}")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise TokenFileError(
            f"{candidate}: malformed JSON ({exc.msg}, line {exc.lineno})"
        ) from exc
    if not isinstance(parsed, dict):
        raise TokenFileError(
            f"{candidate}: JSON root must be an object, found: {type(parsed).__name__}"
        )
    return parsed


def _is_int(value: object) -> bool:
    # bool is a subclass of int; explicitly excluded so True/1 are not mixed up.
    return isinstance(value, int) and not isinstance(value, bool)


def _identity_errors(value: object) -> list[str]:
    field = "'target_identity'"
    if not isinstance(value, dict):
        return [f"{field} must be an object; found: {type(value).__name__}"]
    errors: list[str] = []
    for extra in sorted(set(value) - IDENTITY_FIELDS):
        errors.append(
            f"{field}.{extra} unknown field; allowed fields: "
            + ", ".join(sorted(IDENTITY_FIELDS))
            + " (additionalProperties: false)"
        )
    for name in ("hwnd", "pid", "process_started_at"):
        if name not in value:
            errors.append(f"{field}.{name} required; target identity missing")
        elif not _is_int(value[name]):
            errors.append(
                f"{field}.{name} must be an integer; found: {type(value[name]).__name__}"
            )
    return errors


def _moment_errors(field: str, value: object):
    """Check offset-aware ISO-8601; return (errors, moment|None)."""
    if not isinstance(value, str):
        return [f"{field} must be a string; found: {type(value).__name__}"], None
    text = value.strip()
    if text == "":
        return [f"{field} must not be empty"], None
    try:
        return [], parse_offset_aware(text)
    except Iso8601Error as exc:
        return [f"{field} {exc}"], None


def validate_token(doc: object) -> list[str]:
    """Check a confirmation token; return a violation list (empty = valid)."""
    errors: list[str] = []
    if not isinstance(doc, dict):
        return [f"root node must be an object; found: {type(doc).__name__}"]
    for extra in sorted(set(doc) - TOKEN_FIELDS):
        errors.append(
            f"unknown field at root level {extra!r}; allowed fields: "
            + ", ".join(sorted(TOKEN_FIELDS))
            + " (additionalProperties: false)"
        )

    token_id = doc.get("token_id")
    if "token_id" not in doc:
        errors.append("'token_id' required; write a UUID4 id")
    elif not isinstance(token_id, str):
        errors.append(f"'token_id' must be a string; found: {type(token_id).__name__}")
    elif UUID4_RE.fullmatch(token_id.strip()) is None:
        errors.append(f"'token_id' must be UUID4-shaped; found: {token_id!r}")

    if "target_identity" not in doc:
        errors.append("'target_identity' required; write the target identity")
    else:
        errors.extend(_identity_errors(doc["target_identity"]))

    action = doc.get("action_class")
    if "action_class" not in doc:
        errors.append("'action_class' required; write the protected action class")
    elif action not in PROTECTED_ACTIONS:
        errors.append(
            f"'action_class' unknown action class {action!r}; "
            "protected actions: " + ", ".join(sorted(PROTECTED_ACTIONS))
        )

    if "issued_at" not in doc:
        errors.append("'issued_at' required; write the issuance moment")
        issued = None
    else:
        issued_problems, issued = _moment_errors("'issued_at'", doc["issued_at"])
        errors.extend(issued_problems)
    if "expires_at" not in doc:
        errors.append("'expires_at' required; write the expiry")
        expires = None
    else:
        expires_problems, expires = _moment_errors("'expires_at'", doc["expires_at"])
        errors.extend(expires_problems)

    now = datetime.now(timezone.utc)
    if issued is not None and issued > now:
        errors.append(
            f"'issued_at' cannot be in the future ({doc['issued_at']!r}); "
            "clock may be ahead or the record is wrong"
        )
    if issued is not None and expires is not None and expires <= issued:
        errors.append(
            "'expires_at' must be after the 'issued_at' value; lifetime "
            f"cannot be negative/zero (issued_at={doc['issued_at']!r}, "
            f"expires_at={doc['expires_at']!r})"
        )
    if expires is not None and is_past(expires):
        errors.append(
            f"'expires_at' is a past date ({doc['expires_at']!r}); "
            "expired tokens are rejected"
        )

    if "single_use" not in doc:
        errors.append("'single_use' required; only true is valid in this version")
    elif doc["single_use"] is not True:
        errors.append(
            "'single_use' const:true violation; in this first version every token "
            f"is single-use, found: {doc['single_use']!r}"
        )

    if "consumed_at" in doc:
        # A consumption record looks to the past; no expiry rejection
        # applies, only format + not-before-issuance are checked.
        consumed_problems, consumed = _moment_errors(
            "'consumed_at'", doc["consumed_at"]
        )
        errors.extend(consumed_problems)
        if consumed is not None and issued is not None and consumed < issued:
            errors.append(
                "'consumed_at' cannot be before the 'issued_at' value "
                f"(consumption cannot precede issuance): {doc['consumed_at']!r}"
            )
    return errors


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print(
            "usage: py -3.12 tools/validate_confirmation_token.py <token.json>",
            file=sys.stderr,
        )
        return 2
    try:
        doc = load_token(args[0])
    except TokenFileError as exc:
        print(f"FILE ERROR: {exc}", file=sys.stderr)
        return 2
    problems = validate_token(doc)
    if problems:
        print(f"INVALID: {args[0]}")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print(f"OK: {args[0]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
