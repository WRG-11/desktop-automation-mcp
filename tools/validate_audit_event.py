"""Running-server redacted audit event validator.

Usage:
    py -3.12 tools/validate_audit_event.py <event.json>

What it does: enforces the rules in schema/audit_event.schema.json
(without a JSON Schema library; standard library only). The same
JSON-only decision as the token validator applies here as well
(rationale is in schema/README.md).

Exit codes (same contract as tools/validate_policy.py):
    0 — event VALID (writes "OK").
    1 — event INVALID (says which rule was violated).
    2 — probe/file error: file not found, unreadable, or malformed JSON
        (broken probe is NOT the same as an invalid event).
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _iso8601 import Iso8601Error, parse_offset_aware

# Must be identical to KNOWN_ACTIONS on the server
# (src/desktop_automation_mcp/server.py and policy.py). When a new action
# is added, all three places are updated together.
KNOWN_ACTIONS = frozenset({"observe", "hover", "click", "text", "key", "close", "drag"})

OUTCOMES = frozenset({"allowed", "denied", "error"})

AUDIT_FIELDS = frozenset(
    {
        "timestamp",
        "correlation_id",
        "policy_id",
        "action_type",
        "outcome",
        "denial_reason",
        "duration_ms",
        "target_identity",
        "operation",
        "privacy_context",
    }
)

IDENTITY_FIELDS = frozenset({"hwnd", "pid", "process_started_at"})

CORRELATION_ID_RE = re.compile(r"^[0-9a-f]{12}$")
OPERATION_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
PRIVACY_CONTEXT_FIELDS = frozenset(
    {"region_limited", "mask_count", "pixel_area", "png_bytes"}
)


class AuditFileError(Exception):
    """File could not be read or parsed (NOT an invalid event)."""


def load_event(path: str | Path) -> dict:
    """Read an audit file and convert it to a dict (JSON only)."""
    candidate = Path(path)
    if not candidate.exists():
        raise AuditFileError(f"file not found: {candidate}")
    if not candidate.is_file():
        raise AuditFileError(f"audit path is not a file: {candidate}")
    try:
        text = candidate.read_text(encoding="utf-8")
    except OSError as exc:
        raise AuditFileError(f"could not read file ({candidate}): {exc}") from exc
    if text.strip() == "":
        raise AuditFileError(f"file is empty: {candidate}")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AuditFileError(
            f"{candidate}: malformed JSON ({exc.msg}, line {exc.lineno})"
        ) from exc
    if not isinstance(parsed, dict):
        raise AuditFileError(
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
    extras = set(value) - IDENTITY_FIELDS
    if "title" in extras:
        # Fail-closed leak prevention: titles must not enter audit; this
        # conflict is spelled out more explicitly than the generic
        # 'unknown field' message.
        errors.append(
            f"{field}.title FORBIDDEN; audit records must not carry window titles "
            "(no image, text, or title is stored)"
        )
        extras = extras - {"title"}
    for extra in sorted(extras):
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


def _privacy_context_errors(value: object) -> list[str]:
    field = "'privacy_context'"
    if not isinstance(value, dict):
        return [f"{field} must be an object; found: {type(value).__name__}"]
    errors: list[str] = []
    for extra in sorted(set(value) - PRIVACY_CONTEXT_FIELDS):
        errors.append(
            f"{field}.{extra} unknown/sensitive field; allowed fields: "
            + ", ".join(sorted(PRIVACY_CONTEXT_FIELDS))
        )
    if "region_limited" in value and not isinstance(value["region_limited"], bool):
        errors.append(f"{field}.region_limited must be boolean")
    for name in ("mask_count", "pixel_area", "png_bytes"):
        if name in value and (not _is_int(value[name]) or value[name] < 0):
            errors.append(f"{field}.{name} must be a non-negative integer")
    return errors


def validate_event(doc: object) -> list[str]:
    """Check an audit event; return a violation list (empty = valid)."""
    errors: list[str] = []
    if not isinstance(doc, dict):
        return [f"root node must be an object; found: {type(doc).__name__}"]
    for extra in sorted(set(doc) - AUDIT_FIELDS):
        errors.append(
            f"unknown field at root level {extra!r}; allowed fields: "
            + ", ".join(sorted(AUDIT_FIELDS))
            + " (additionalProperties: false — sensitive fields like a future "
            "screenshot_bytes are rejected this way)"
        )

    if "timestamp" not in doc:
        errors.append("'timestamp' required; write the event moment")
    elif not isinstance(doc["timestamp"], str):
        errors.append(
            f"'timestamp' must be a string; found: {type(doc['timestamp']).__name__}"
        )
    else:
        # NOTE: an audit record is a retrospective observation; a past date
        # is normal — there is NO expiry rejection here (format only).
        try:
            parse_offset_aware(doc["timestamp"].strip())
        except Iso8601Error as exc:
            errors.append(f"'timestamp' {exc}")

    correlation = doc.get("correlation_id")
    if "correlation_id" not in doc:
        errors.append("'correlation_id' required; write a 12-lowercase-hex id")
    elif not isinstance(correlation, str):
        errors.append(
            f"'correlation_id' must be a string; found: {type(correlation).__name__}"
        )
    elif CORRELATION_ID_RE.fullmatch(correlation.strip()) is None:
        errors.append(
            f"'correlation_id' must be 12-lowercase-hex; found: {correlation!r}"
        )

    policy_id = doc.get("policy_id")
    if "policy_id" not in doc:
        errors.append("'policy_id' required; write the opaque policy id")
    elif not isinstance(policy_id, str):
        errors.append(
            f"'policy_id' must be a string; found: {type(policy_id).__name__}"
        )
    elif policy_id.strip() == "":
        errors.append("'policy_id' must not be empty")

    action = doc.get("action_type")
    if "action_type" not in doc:
        errors.append("'action_type' required; write the action name")
    elif action not in KNOWN_ACTIONS:
        errors.append(
            f"'action_type' unknown action {action!r}; known actions: "
            + ", ".join(sorted(KNOWN_ACTIONS))
        )

    operation = doc.get("operation")
    if "operation" in doc and (
        not isinstance(operation, str) or OPERATION_RE.fullmatch(operation) is None
    ):
        errors.append(f"'operation' must be safe snake_case; found: {operation!r}")

    outcome = doc.get("outcome")
    if "outcome" not in doc:
        errors.append("'outcome' required; write allowed/denied/error")
    elif outcome not in OUTCOMES:
        errors.append(
            f"'outcome' invalid value {outcome!r}; allowed: "
            + ", ".join(sorted(OUTCOMES))
        )

    reason = doc.get("denial_reason")
    if outcome in ("denied", "error"):
        # The schema's then-branch (required) + non-emptiness check.
        if "denial_reason" not in doc:
            errors.append(
                f"'denial_reason' required; with outcome {outcome!r} "
                "a reason must be written"
            )
        elif not isinstance(reason, str):
            errors.append(
                f"'denial_reason' must be a string; found: {type(reason).__name__}"
            )
        elif reason.strip() == "":
            errors.append(
                f"'denial_reason' must not be empty; with outcome {outcome!r} "
                "a meaningful reason must be written"
            )
    elif outcome == "allowed":
        # The schema's else-branch: an allowed record must not carry a denial reason.
        if "denial_reason" in doc:
            errors.append(
                "'denial_reason' must not be given with outcome 'allowed'; "
                "an 'allowed but denial-reasoned' record is contradictory"
            )
    elif "denial_reason" in doc and not isinstance(reason, str):
        errors.append(
            f"'denial_reason' must be a string; found: {type(reason).__name__}"
        )

    duration = doc.get("duration_ms")
    if "duration_ms" not in doc:
        errors.append("'duration_ms' required; write the duration in milliseconds")
    elif not _is_int(duration):
        errors.append(
            "'duration_ms' must be an integer (not boolean); "
            f"found: {type(duration).__name__}"
        )
    elif duration < 0:
        errors.append(
            f"'duration_ms' must not be negative (minimum: 0); found: {duration}"
        )

    if "target_identity" in doc:
        errors.extend(_identity_errors(doc["target_identity"]))
    if "privacy_context" in doc:
        errors.extend(_privacy_context_errors(doc["privacy_context"]))
    return errors


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print(
            "usage: py -3.12 tools/validate_audit_event.py <event.json>",
            file=sys.stderr,
        )
        return 2
    try:
        doc = load_event(args[0])
    except AuditFileError as exc:
        print(f"FILE ERROR: {exc}", file=sys.stderr)
        return 2
    problems = validate_event(doc)
    if problems:
        print(f"INVALID: {args[0]}")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print(f"OK: {args[0]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
