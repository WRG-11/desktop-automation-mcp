"""Per-key sliding-window rate limiting (ROADMAP §9's closing item:
"Add per-application action, image-size, and input-character rate
limits.").

Answers ONE question: has this key (in practice, an application's
normalised process path) already spent its budget for a given resource
in the current time window? It does not decide what the limits ARE
(`server.py` reads them from environment variables, with defaults) or
which resource a tool call maps to — callers call `consume` at whichever
point they want a budget enforced.

Design decisions (no silent assumptions):

- Sliding WINDOW LOG, not a token bucket: a list of (timestamp, cost)
  pairs per key, pruned lazily on each call. Simpler to reason about and
  to test deterministically (inject `now`) than a refilling bucket, and
  "per-minute" budgets map directly onto it without an extra refill-rate
  concept.
- `consume` either fully admits an event or fully rejects it — there is no
  partial admission. Rejecting raises `PolicyDeniedError` (a policy-level
  denial with stable ``policy_denied`` code; still a `PermissionError`), not
  a `RuntimeError`/`ValueError`: a caller that hit their budget is not wrong
  about the target, only over quota.
- Per-key isolation: one noisy application cannot exhaust another
  application's budget, because each key gets its own event list.
- `now` is an optional injectable parameter (defaults to
  `time.monotonic()`) purely so tests can advance time deterministically
  without real sleeps; production code never passes it.
"""

from __future__ import annotations

import threading
import time

from .errors import PolicyDeniedError


class SlidingWindowLimiter:
    """Thread-safe per-key sliding-window budget."""

    def __init__(self, window_s: float = 60.0) -> None:
        if window_s <= 0:
            raise ValueError("window_s must be positive")
        self._window_s = window_s
        self._lock = threading.Lock()
        self._events: dict[str, list[tuple[float, int]]] = {}

    def consume(
        self, key: str, cost: int, limit: int, *, now: float | None = None
    ) -> None:
        """Admit `cost` units under `key`, or raise if over `limit`.

        `limit` is the total budget allowed within the window; `cost` is
        how much this one call would use. Both must be non-negative
        integers (a caller passing a bad type/sign is a programmer error,
        `ValueError`, not a rate-limit decision).
        """
        if not isinstance(cost, int) or isinstance(cost, bool) or cost < 0:
            raise ValueError(f"cost must be a non-negative integer: {cost!r}")
        if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
            raise ValueError(f"limit must be a positive integer: {limit!r}")
        moment = time.monotonic() if now is None else now
        cutoff = moment - self._window_s
        with self._lock:
            events = self._events.setdefault(key, [])
            events[:] = [event for event in events if event[0] >= cutoff]
            used = sum(existing_cost for _, existing_cost in events)
            if used + cost > limit:
                # Independent security review F-4a (2026-09-14): the message
                # carried `key` (the application's full exe path) and this
                # `PolicyDeniedError` ENTERED `audit.py` as `denial_reason`
                # via `_prepare_action_target` — against the spirit of the
                # installed-software-inventory redaction principle.
                # The message no longer contains `key`; numbers only.
                raise PolicyDeniedError(
                    f"rate limit exceeded: {used}+{cost} > {limit} "
                    f"(window={self._window_s:.0f} s)"
                )
            events.append((moment, cost))

    def snapshot(self, *, now: float | None = None) -> dict:
        """Current window usage WITHOUT key names.

        Returns `{"window_s", "tracked_keys", "total_used"}` over live
        (unexpired) events only. Per-key identities are deliberately NOT
        exposed: keys are application exe paths (see the F-4a note above),
        so only aggregates leave this module. Read-only: prunes nothing,
        admits nothing; `now` is injectable for deterministic tests exactly
        like in `consume`.
        """
        moment = time.monotonic() if now is None else now
        cutoff = moment - self._window_s
        with self._lock:
            live_keys = 0
            live_used = 0
            for events in self._events.values():
                key_used = sum(
                    existing_cost for stamp, existing_cost in events if stamp >= cutoff
                )
                if key_used:
                    live_keys += 1
                    live_used += key_used
        return {
            "window_s": self._window_s,
            "tracked_keys": live_keys,
            "total_used": live_used,
        }

    def reset(self) -> None:
        """Wipe all tracked usage (tests only; production never calls this)."""
        with self._lock:
            self._events.clear()
