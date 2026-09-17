"""In-memory single-use confirmation tokens for protected actions.

A protected action (`text`, `close`, `drag`) must not run on policy permission
alone: it additionally needs a short-lived token bound to the exact
target identity (`hwnd`/`pid`/`process_started_at`), the action class
and an expiry moment (ROADMAP §9). This module issues, consumes,
previews and sweeps those tokens. It IS wired into `server.py`: the
`request_confirmation` MCP tool calls `issue_confirmation`, and
`close_window`/`send_text`/`drag_window` each take a mandatory
`confirmation_token` parameter that `server.py` passes to
`consume_confirmation` before performing the effect.

What this module is NOT:

- It does not decide WHO may approve. `issue_confirmation` records an
  approval; how the human operator approves (UI prompt, explicit
  request/grant round-trip) is out of scope here.
- It does not read the policy file itself. The per-file
  `protected_actions` list is resolved by the CALLER (`server.py` reads it
  via `policy._protected_actions()`) and passed in as `extra_protected`.
  The effective protected set is the UNION of the fixed
  `PROTECTED_ACTION_CLASSES` below and that per-file list: a file can ADD
  protection (e.g. `click`), it can never REMOVE the built-in
  `text`/`close`/`drag` protection. With no `extra_protected` argument the
  module behaves exactly as before (fixed set only).
- It does not persist anything. Tokens live in process memory; a
  restart wipes them and unknown token ids are denied (fail closed).
- It does not know `TargetSnapshot`. Callers pass the plain
  `{"hwnd", "pid", "process_started_at"}` triple (same meaning, no
  import); `server.py` bridges the two.

Design decisions (no silent assumptions):

- `ConfirmationStore` class (not a bare module-global dict): every test
  — and every future server instance — gets its own isolated store, so
  no state leaks between tests. Module-level `issue_confirmation` /
  `consume_confirmation` / `preview_action` / `purge_expired` delegate
  to one default store for the single-server runtime.
- Expired records are removed automatically on the next `issue()` call
  (amortized cleanup under the same lock). `purge_expired()` remains an
  explicit maintenance API and reports how many records it removed.
- `ttl_seconds` is capped at 300 s (5 min); unbounded TTL would violate
  the short-lived principle. Non-positive or non-integer TTL is
  `ValueError` (programmer error, not a permission decision).
- `consume_confirmation` checks, in fixed order: unknown id, already
  consumed, expired, identity mismatch (naming the differing fields),
  action mismatch. Each is a distinct, human-readable `PolicyDeniedError`
  with the stable ``policy_denied`` code. Token ids and expiry values are
  deliberately not repeated in denial text because that text can reach audit.
- A `threading.Lock` guards check-and-mark so two concurrent consumes
  of the same token cannot both succeed.
- `action_class` is `text`/`close`/`drag`. (`drag` was added when the
  token schema enum was extended; the runtime `KNOWN_ACTIONS` already
  carried it.) This module follows the schema (deliberately NOT
  imported from `policy.py`, see above).
"""

from __future__ import annotations

import copy
import threading
import uuid
from datetime import datetime, timedelta, timezone

from .errors import PolicyDeniedError
from .policy_file import is_past, parse_offset_aware

ACTION_TEXT = "text"
ACTION_CLOSE = "close"
ACTION_DRAG = "drag"

# Which action classes require a token. Fixed mechanism set — the per-file
# `protected_actions` data from policy documents EXTENDS (never narrows)
# this set via the `extra_protected` argument (union semantics, enforced by
# `server.py`); see module docstring.
PROTECTED_ACTION_CLASSES = frozenset({ACTION_TEXT, ACTION_CLOSE, ACTION_DRAG})

DEFAULT_TTL_SECONDS = 60
MAX_TTL_SECONDS = 300

IDENTITY_FIELDS = ("hwnd", "pid", "process_started_at")


def _iso(moment: datetime) -> str:
    """Convert a UTC moment to an offset-aware ISO-8601 string (`+00:00` offset)."""
    return moment.isoformat(timespec="seconds")


def is_protected_action(action_class: str, extra_protected=None) -> bool:
    """Is a token required for this action class (fixed set ∪ per-file extras)?

    `extra_protected` is the resolved per-application `protected_actions`
    list (any iterable of action names, or `None`). Matching is exact;
    callers normalize case beforehand (`server.py` passes already-folded
    values). Never narrows: members of `PROTECTED_ACTION_CLASSES` are
    always protected.
    """
    if action_class in PROTECTED_ACTION_CLASSES:
        return True
    if extra_protected is None:
        return False
    return action_class in set(extra_protected)


def _check_action_class(action_class: str, extra_protected=None) -> str:
    if is_protected_action(action_class, extra_protected):
        return action_class
    raise ValueError(
        f"unknown action class {action_class!r}; protected classes: "
        + ", ".join(sorted(PROTECTED_ACTION_CLASSES))
    )


def _check_identity(identity: object) -> dict:
    """Validate the target identity; return its DEFENSIVE COPY.

    The copy is critical: even if the caller later mutates their dict,
    the stored binding does not change.
    """
    if not isinstance(identity, dict):
        raise ValueError(
            "target identity must be an object "
            f"(hwnd/pid/process_started_at); found: {type(identity).__name__}"
        )
    missing = [f for f in IDENTITY_FIELDS if f not in identity]
    if missing:
        raise ValueError(f"missing field(s) in target identity: {', '.join(missing)}")
    for field in IDENTITY_FIELDS:
        value = identity[field]
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError(
                f"target identity '{field}' must be an integer (not boolean); "
                f"found: {type(value).__name__}"
            )
    extra = sorted(set(identity) - set(IDENTITY_FIELDS))
    if extra:
        raise ValueError(
            f"unknown field(s) in target identity: {', '.join(extra)}; "
            "allowed: hwnd, pid, process_started_at"
        )
    return {field: identity[field] for field in IDENTITY_FIELDS}


def _check_ttl(ttl_seconds: int) -> int:
    if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int):
        raise ValueError(
            "ttl_seconds must be an integer (not boolean); "
            f"found: {type(ttl_seconds).__name__}"
        )
    if not 0 < ttl_seconds <= MAX_TTL_SECONDS:
        raise ValueError(
            f"ttl_seconds must be between 1 and {MAX_TTL_SECONDS} "
            "(short-lived principle); "
            f"found: {ttl_seconds!r}"
        )
    return ttl_seconds


class ConfirmationStore:
    """In-memory token store; each instance is isolated (test-friendly)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tokens: dict[str, dict] = {}

    def issue(
        self,
        target_identity: dict,
        action_class: str,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        extra_protected=None,
    ) -> dict:
        """Issue and store a new single-use confirmation token.

        The returned document is compatible with
        `schema/confirmation_token.schema.json` (no `consumed_at`: not yet
        consumed). Invalid inputs raise `ValueError` (programmer error,
        not a permission decision).

        Independent security review F-2 (2026-09-14): `purge_expired()`
        was never called from any server loop, so expired records
        accumulated forever. Instead of setting up a permanent
        background task (over-engineering for this single-user,
        synchronous server), every `issue` call amortizes its OWN sweep —
        requesting a new token is already a "some time has passed" signal.
        """
        action = _check_action_class(action_class, extra_protected)
        identity = _check_identity(target_identity)
        ttl = _check_ttl(ttl_seconds)
        now = datetime.now(timezone.utc)
        token = {
            "token_id": str(uuid.uuid4()),
            "target_identity": identity,
            "action_class": action,
            "issued_at": _iso(now),
            "expires_at": _iso(now + timedelta(seconds=ttl)),
            "single_use": True,
        }
        with self._lock:
            self._tokens[token["token_id"]] = token
            expired = [
                existing_id
                for existing_id, record in self._tokens.items()
                if is_past(parse_offset_aware(record["expires_at"]))
            ]
            for existing_id in expired:
                del self._tokens[existing_id]
        return copy.deepcopy(token)

    def consume(self, token_id: str, target_identity: dict, action_class: str) -> None:
        """Consume the token; `PolicyDeniedError` on rule violations.

        On success the record is stamped with `consumed_at` (it can never
        be consumed again) and `None` is returned. The check order is
        fixed: unknown id, already consumed, expired, target-identity
        mismatch, action mismatch.
        """
        with self._lock:
            record = self._tokens.get(token_id)
            if record is None:
                raise PolicyDeniedError(
                    "unknown confirmation token; the process may have restarted."
                )
            if "consumed_at" in record:
                raise PolicyDeniedError(
                    "confirmation token already consumed (single_use violation)."
                )
            if is_past(parse_offset_aware(record["expires_at"])):
                raise PolicyDeniedError("confirmation token has expired.")
            if not isinstance(target_identity, dict):
                raise PolicyDeniedError(
                    "target identity mismatch: call did not provide an object, "
                    f"found: {type(target_identity).__name__}"
                )
            differing = [
                field
                for field in IDENTITY_FIELDS
                if target_identity.get(field) != record["target_identity"][field]
            ]
            if differing:
                raise PolicyDeniedError(
                    "target identity mismatch "
                    f"(differing fields: {', '.join(differing)}): token "
                    "cannot be used against another window/PID object"
                )
            if action_class != record["action_class"]:
                raise PolicyDeniedError(
                    "action class mismatch; confirmation token "
                    "cannot be used with the requested action, "
                    "token may only be consumed for its own class."
                )
            record["consumed_at"] = _iso(datetime.now(timezone.utc))
            return None

    def preview(
        self, action_class: str, target_identity: dict, extra_protected=None
    ) -> dict:
        """Side-effect-free summary: policy outcome + live token if any.

        Produces/consumes/deletes nothing; writes nothing to the store. If
        several live tokens exist, the one with the latest expiry is
        reported (deterministic rule). For an unknown action class returns
        `requires_confirmation=False` and does NOT raise (read-only
        information, grants nothing).
        """
        with self._lock:
            live = [
                record
                for record in self._tokens.values()
                if "consumed_at" not in record
                and not is_past(parse_offset_aware(record["expires_at"]))
                and record["action_class"] == action_class
                and record["target_identity"] == target_identity
            ]
        chosen = (
            max(live, key=lambda r: parse_offset_aware(r["expires_at"]))
            if live
            else None
        )
        echo = (
            dict(target_identity)
            if isinstance(target_identity, dict)
            else target_identity
        )
        return {
            "action_class": action_class,
            "target_identity": echo,
            "requires_confirmation": is_protected_action(action_class, extra_protected),
            "token_id": chosen["token_id"] if chosen is not None else None,
            "token_expires_at": chosen["expires_at"] if chosen is not None else None,
        }

    def purge_expired(self) -> int:
        """Delete expired records (consumed or not).

        Return the number removed. In the normal server flow `issue()`
        performs the same cleanup in amortized fashion; this method is
        for explicit maintenance/test calls.
        """
        with self._lock:
            expired = [
                token_id
                for token_id, record in self._tokens.items()
                if is_past(parse_offset_aware(record["expires_at"]))
            ]
            for token_id in expired:
                del self._tokens[token_id]
            return len(expired)


_default_store = ConfirmationStore()


def issue_confirmation(
    target_identity: dict,
    action_class: str,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    extra_protected=None,
) -> dict:
    """Issue and store a new single-use confirmation token IN MEMORY.

    The returned dict must be COMPATIBLE with
    `confirmation_token.schema.json` (so it stays verifiable with
    `tools/validate_confirmation_token.py` — REALLY test it). ValueError
    if `action_class` is neither in the fixed protected set nor in the
    per-file `extra_protected` list.
    """
    return _default_store.issue(
        target_identity, action_class, ttl_seconds, extra_protected
    )


def consume_confirmation(
    token_id: str, target_identity: dict, action_class: str
) -> None:
    """Attempt to CONSUME the token; PermissionError on failure."""
    _default_store.consume(token_id, target_identity, action_class)


def preview_action(
    action_class: str, target_identity: dict, extra_protected=None
) -> dict:
    """Summarize the policy outcome and live token without any side effects."""
    return _default_store.preview(action_class, target_identity, extra_protected)


def purge_expired() -> int:
    """Delete expired records; return the number removed."""
    return _default_store.purge_expired()
