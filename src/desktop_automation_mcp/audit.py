"""In-memory redacted audit trail for every guarded action attempt.

The redacted audit contract records timestamp, a 12-lowercase-hex
correlation_id, policy_id, action type, outcome, denial reason, duration and
optional operation/privacy counters —
"No image, text, or title is stored." This module owns emission and
storage; `server.py`'s `_execute_guarded_action` calls `record_event`
around preparation and the real effect so every targeted effect or screenshot
tool call leaves one trace of its final success, denial or technical failure.

Storage is a BOUNDED in-memory ring buffer (ROADMAP §9's own "make the audit
default in-memory" decision, revisited only if a later decision
asks for durable storage) — a restart loses history, and the oldest events
are silently dropped past `MAX_EVENTS` rather than growing without bound.

What this module deliberately does NOT do:

- It does not decide WHICH actions are audited — every call site in
  `server.py` that wants a trail calls `record_event` itself; this module
  has no opinion on which tools matter.
- It does not persist to disk.
- It does not compute `policy_id` or `correlation_id` — the caller
  supplies both; this module only validates their shape
  matches `schema/audit_event.schema.json`'s cross-field rule.
- It does not know `TargetSnapshot` — callers pass the same plain
  `{"hwnd", "pid", "process_started_at"}` triple `confirmation.py` uses,
  so `target_identity` in an audit event and in a confirmation token are
  directly comparable.
- The `correlation_id` recorded here is the same opaque value returned by the
  tool result. Image results carry it in MCP `_meta`.
"""

from __future__ import annotations

import threading
from collections import deque
from datetime import datetime, timezone
import re

MAX_EVENTS = 1000

OUTCOME_ALLOWED = "allowed"
OUTCOME_DENIED = "denied"
OUTCOME_ERROR = "error"
_KNOWN_OUTCOMES = frozenset({OUTCOME_ALLOWED, OUTCOME_DENIED, OUTCOME_ERROR})
_KNOWN_ACTIONS = frozenset(
    {"observe", "hover", "click", "text", "key", "close", "drag"}
)
_CORRELATION_ID_RE = re.compile(r"[0-9a-f]{12}")
_OPERATION_RE = re.compile(r"[a-z][a-z0-9_]{0,63}")
_PRIVACY_CONTEXT_FIELDS = frozenset(
    {"region_limited", "mask_count", "pixel_area", "png_bytes"}
)


class AuditLog:
    """Bounded, thread-safe ring buffer of redacted audit events."""

    def __init__(self, max_events: int = MAX_EVENTS) -> None:
        self._lock = threading.Lock()
        self._events: deque[dict] = deque(maxlen=max_events)

    def record(
        self,
        *,
        correlation_id: str,
        policy_id: str,
        action_type: str,
        outcome: str,
        duration_ms: int,
        denial_reason: str | None = None,
        target_identity: dict | None = None,
        operation: str | None = None,
        privacy_context: dict | None = None,
    ) -> dict:
        """Append one redacted event; returns the stored (immutable) copy.

        Mirrors `audit_event.schema.json`'s cross-field rule exactly:
        `denied`/`error` REQUIRE a non-empty `denial_reason`; `allowed`
        FORBIDS one. Both directions raise `ValueError` (programmer error
        in the calling code, not a security decision) rather than silently
        storing an inconsistent event.
        """
        if outcome not in _KNOWN_OUTCOMES:
            raise ValueError(
                f"unknown outcome: {outcome!r}; allowed: "
                + ", ".join(sorted(_KNOWN_OUTCOMES))
            )
        if (
            not isinstance(correlation_id, str)
            or _CORRELATION_ID_RE.fullmatch(correlation_id) is None
        ):
            raise ValueError("correlation_id must be 12 lowercase hex")
        if not isinstance(policy_id, str) or not policy_id.strip():
            raise ValueError("policy_id must be a non-empty string")
        if action_type not in _KNOWN_ACTIONS:
            raise ValueError(
                f"unknown action_type: {action_type!r}; allowed: "
                + ", ".join(sorted(_KNOWN_ACTIONS))
            )
        if outcome != OUTCOME_ALLOWED and not (denial_reason or "").strip():
            raise ValueError(f"outcome={outcome!r} requires a non-empty denial_reason")
        if outcome == OUTCOME_ALLOWED and denial_reason:
            raise ValueError("denial_reason must not be given when outcome='allowed'")
        if (
            not isinstance(duration_ms, int)
            or isinstance(duration_ms, bool)
            or duration_ms < 0
        ):
            raise ValueError(
                f"duration_ms must be a non-negative integer; found: {duration_ms!r}"
            )
        if operation is not None and (
            not isinstance(operation, str) or _OPERATION_RE.fullmatch(operation) is None
        ):
            raise ValueError(f"operation must be safe snake_case: {operation!r}")
        if privacy_context is not None:
            if not isinstance(privacy_context, dict):
                raise ValueError("privacy_context must be an object")
            extras = sorted(set(privacy_context) - _PRIVACY_CONTEXT_FIELDS)
            if extras:
                raise ValueError(
                    "privacy_context carries unknown/sensitive field: "
                    + ", ".join(extras)
                )
            if "region_limited" in privacy_context and not isinstance(
                privacy_context["region_limited"], bool
            ):
                raise ValueError("privacy_context.region_limited must be boolean")
            for field in ("mask_count", "pixel_area", "png_bytes"):
                value = privacy_context.get(field)
                if field in privacy_context and (
                    not isinstance(value, int) or isinstance(value, bool) or value < 0
                ):
                    raise ValueError(
                        f"privacy_context.{field} must be a non-negative integer"
                    )
        event: dict = {
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "correlation_id": correlation_id,
            "policy_id": policy_id,
            "action_type": action_type,
            "outcome": outcome,
            "duration_ms": duration_ms,
        }
        if denial_reason is not None:
            event["denial_reason"] = denial_reason
        if target_identity is not None:
            event["target_identity"] = dict(target_identity)
        if operation is not None:
            event["operation"] = operation
        if privacy_context:
            event["privacy_context"] = dict(privacy_context)
        with self._lock:
            self._events.append(event)
        return dict(event)

    def read_events(self) -> list[dict]:
        """Snapshot of currently retained events, oldest first."""
        with self._lock:
            return [dict(event) for event in self._events]

    def clear(self) -> None:
        with self._lock:
            self._events.clear()


_default_log = AuditLog()


def record_event(
    *,
    correlation_id: str,
    policy_id: str,
    action_type: str,
    outcome: str,
    duration_ms: int,
    denial_reason: str | None = None,
    target_identity: dict | None = None,
    operation: str | None = None,
    privacy_context: dict | None = None,
) -> dict:
    """Record one event on the default, process-wide audit log."""
    return _default_log.record(
        correlation_id=correlation_id,
        policy_id=policy_id,
        action_type=action_type,
        outcome=outcome,
        duration_ms=duration_ms,
        denial_reason=denial_reason,
        target_identity=target_identity,
        operation=operation,
        privacy_context=privacy_context,
    )


def read_events() -> list[dict]:
    """Snapshot of the default audit log's currently retained events."""
    return _default_log.read_events()


def clear_events() -> None:
    """Wipe the default audit log (used by tests; never called by server.py)."""
    _default_log.clear()
