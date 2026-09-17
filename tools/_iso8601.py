"""Offset-aware ISO-8601 date-time helpers (Phase 3 design draft).

Shared between the `tools/validate_*.py` validators; avoids duplicating the
"offset-aware ISO-8601 + past-date rejection" logic verbatim in two files.
Uses only the standard library.

Rule (same in all schemas): the date-time string must contain a numeric
UTC offset (like `+03:00`); bare `Z` or offset-less values are invalid.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

OFFSET_SUFFIX_RE = re.compile(r"[+-]\d{2}:?\d{2}$")


class Iso8601Error(ValueError):
    """Structural date-time error (malformed format, missing offset, etc.)."""


def parse_offset_aware(raw: str) -> datetime:
    """Convert an offset-aware ISO-8601 string to a timezone-aware datetime.

    Raises Iso8601Error on failure; the message carries no field-name
    prefix, the caller prefixes it as `f"{field} {error}"`.
    """
    text = raw.strip()
    if text.endswith(("Z", "z")):
        raise Iso8601Error(
            "bare 'Z' is not allowed; a numeric UTC offset is required "
            "(e.g. 2027-01-01T00:00:00+03:00)"
        )
    if OFFSET_SUFFIX_RE.search(text) is None:
        raise Iso8601Error(
            "must contain a UTC offset (e.g. +03:00); offset-less "
            f"date-times are not accepted: {text!r}"
        )
    try:
        moment = datetime.fromisoformat(text)
    except ValueError as exc:
        raise Iso8601Error(
            f"could not be parsed as an ISO-8601 date-time: {text!r}"
        ) from exc
    if moment.tzinfo is None:
        raise Iso8601Error(
            "timezone-naive value; a UTC offset is required (e.g. +03:00)"
        )
    return moment


def is_past(moment: datetime) -> bool:
    """Is the moment in the past relative to now (for expiry checks)?"""
    return moment <= datetime.now(timezone.utc)
