"""Versioned, verifiable coordinate maps for UIA-less canvas apps.

Ruffle-style applications expose no meaningful UI Automation tree
(ADR-0001 deferred full UIA integration), so the only targeting path is
raw pixel coordinates — which are FRAGILE across app updates, window
resizes and wrong-version launches. A coordinate profile pins one app
version and one window size to a pre-recorded, verifiable map. Its
`reference_region_name` selects the only safe region whose masked PNG is
hashed; every verification point must remain inside that region. Every
later use first proves the live window still matches the profile and
REFUSES otherwise (default-deny); only then are named regions resolved
to coordinates.

This module is part of the SHIPPED package (like `policy_file.py`);
`tools/validate_coordinate_profile.py` is the thin developer CLI over
it. It performs NO Win32 calls and NO screen capture — matching is pure
comparison over caller-supplied values (server.py owns pixels). It does
not know `TargetSnapshot`; the profile's `process_path`/`window_size`
are matched by the caller against the resolved target.

Design decisions (no silent assumptions):

- `validate_profile` raises `PermissionError`, never `ValueError`, on
  ANY rule violation — the pattern `policy_file.py`/`confirmation.py`
  established: a malformed profile is a DENIAL (same result class as a
  missing env var), not a caller bug. Rationale: every downstream use
  of a profile gates an effect; routing all rejections through the
  single denial class keeps fail-closed handling uniform.
- `resolve_point` on an unknown region raises `PermissionError` (not
  `KeyError`/`ValueError`) for the same reason: resolving nothing must
  deny the action, and the message lists the available names so the
  operator can see the typo instead of guessing.
- `profile_matches_live_window` NEVER raises for expected-domain
  problems: it returns `(False, reason)` for an invalid profile, bad
  live dimensions, or a hash mismatch. Rationale: its whole job is
  answering "does it match?" — the answer "no, and here is exactly
  which gate failed" must not itself explode. Size and hash mismatches
  get SEPARATE messages (different diagnoses: "window resized" vs.
  "content changed").
- `live_reference_image_bytes=None` means "size gate only": returns
  `(True, ...)` with the reason EXPLICITLY noting the hash check was
  skipped. Rationale: the signature marks the bytes as conditional
  ("verilirse"); refusing outright would make the None path useless,
  while silently treating it as a full match would lie. The reason
  string carries the caveat, so no caller can mistake it for a full
  verification.
- Reference matching is byte-EXACT sha256 (lowercase hexdigest
  canonical form). No fuzzy/perceptual hashing: "close enough" would
  be a silent assumption about a different app version rendering
  acceptably — fail closed instead; the operator re-records.
- Region centers use floor division (`//`): pixel coordinates are
  integers and the rule is deterministic.
- This module never normalises `process_path` (validation only checks
  shape); matching sides must normalise identically at the call site,
  exactly like `policy.py` does for env vs. file paths.
- Only stdlib (`hashlib`, `json`, `re`, `datetime`): no new runtime
  dependency. Images are NEVER stored here — only their hashes.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

_PROFILE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_HASH_RE = re.compile(r"[0-9a-f]{64}")
_OFFSET_SUFFIX_RE = re.compile(r"[+-]\d{2}:?\d{2}$")

_PROFILE_FIELDS = frozenset(
    {
        "profile_id",
        "application",
        "window_size",
        "reference_region_name",
        "reference_image_hash",
        "safe_regions",
        "verification_points",
        "created_at",
    }
)
_APPLICATION_FIELDS = frozenset({"process_path", "app_version"})
_SIZE_FIELDS = frozenset({"width", "height"})
_REGION_FIELDS = frozenset({"name", "rect", "description"})
_POINT_FIELDS = frozenset({"point", "expected_color"})


class ProfileParseError(ValueError):
    """File was read but could not be parsed as a JSON document."""


class Iso8601Error(ValueError):
    """Structural date-time error (malformed format, missing offset, etc.)."""


def parse_offset_aware(raw: str) -> datetime:
    """Convert an offset-aware ISO-8601 string to a timezone-aware datetime.

    Bare `Z` / offset-less / naive values raise `Iso8601Error`.
    (Same rule as `policy_file.py`; small in-package duplication kept
    here because the shared `tools/_iso8601.py` cannot be imported from
    outside the package by tools.)
    """
    text = raw.strip()
    if text.endswith(("Z", "z")):
        raise Iso8601Error(
            "bare 'Z' is not allowed; a numeric UTC offset is required "
            "(e.g. 2026-09-14T12:00:00+03:00)"
        )
    if _OFFSET_SUFFIX_RE.search(text) is None:
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
    """Is the moment in the past relative to now?"""
    return moment <= datetime.now(timezone.utc)


def _is_int(value: object) -> bool:
    # bool is a subclass of int; explicitly excluded so True/1 are not mixed up.
    return isinstance(value, int) and not isinstance(value, bool)


def read_profile_document(path: str | Path) -> dict:
    """Read a profile file and convert it to a dict (JSON only, no validation).

    `OSError` on read problems, `ProfileParseError` on parse problems.
    Profiles are machine-produced/consumed records (hash + exact rect);
    YAML's implicit typing (unquoted `0123` etc.) risks silent corruption
    in such data, so only JSON is accepted.
    """
    candidate = Path(path)
    if not candidate.exists():
        raise FileNotFoundError(f"file not found: {candidate}")
    if not candidate.is_file():
        raise OSError(f"profile path is not a file: {candidate}")
    try:
        text = candidate.read_text(encoding="utf-8")
    except OSError as exc:
        raise OSError(f"could not read file ({candidate}): {exc}") from exc
    if text.strip() == "":
        raise ProfileParseError(f"file is empty: {candidate}")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProfileParseError(
            f"{candidate}: malformed JSON ({exc.msg}, line {exc.lineno})"
        ) from exc
    if not isinstance(parsed, dict):
        raise ProfileParseError(
            f"{candidate}: JSON root must be an object, found: {type(parsed).__name__}"
        )
    return parsed


def _window_bounds(doc: dict) -> tuple[int, int] | None:
    """(width, height) if a valid window_size exists, else None."""
    size = doc.get("window_size")
    if not isinstance(size, dict):
        return None
    width, height = size.get("width"), size.get("height")
    if _is_int(width) and _is_int(height) and width >= 1 and height >= 1:
        return (width, height)
    return None


def validate_profile_document(doc: object) -> list[str]:
    """Check a profile dict against the schema + cross-field rules.

    Return a violation list (empty = valid). What the schema file cannot
    express is enforced here: region-name uniqueness, rect/point ordering
    + window bounds, `created_at` future-date rejection.
    """
    errors: list[str] = []
    if not isinstance(doc, dict):
        return [f"root node must be an object; found: {type(doc).__name__}"]
    for extra in sorted(set(doc) - _PROFILE_FIELDS):
        errors.append(
            f"unknown field at root level {extra!r}; allowed fields: "
            + ", ".join(sorted(_PROFILE_FIELDS))
            + " (additionalProperties: false)"
        )
    for field in (
        "profile_id",
        "application",
        "window_size",
        "reference_region_name",
        "reference_image_hash",
        "safe_regions",
        "verification_points",
        "created_at",
    ):
        if field not in doc:
            errors.append(f"'{field}' required field missing")

    profile_id = doc.get("profile_id")
    if "profile_id" in doc:
        if not isinstance(profile_id, str):
            errors.append(
                f"'profile_id' must be a string; found: {type(profile_id).__name__}"
            )
        elif not profile_id or len(profile_id) > 128:
            errors.append(
                "'profile_id' must be 1-128 characters; "
                f"found length: {len(profile_id)}"
            )
        elif _PROFILE_ID_RE.fullmatch(profile_id) is None:
            errors.append(
                f"'profile_id' must be slug-shaped "
                f"(starts with letter/digit, [A-Za-z0-9._-]): {profile_id!r}"
            )

    app = doc.get("application")
    if "application" in doc:
        if not isinstance(app, dict):
            errors.append(
                f"'application' must be an object; found: {type(app).__name__}"
            )
        else:
            for extra in sorted(set(app) - _APPLICATION_FIELDS):
                errors.append(
                    f"'application.{extra}' unknown field; allowed: "
                    "process_path, app_version"
                )
            exe = app.get("process_path")
            if "process_path" not in app:
                errors.append(
                    "'application.process_path' required; write the full path"
                )
            elif not isinstance(exe, str):
                errors.append(
                    "'application.process_path' must be a string; "
                    f"found: {type(exe).__name__}"
                )
            elif exe.strip() == "":
                errors.append("'application.process_path' must not be empty")
            if "app_version" in app and (
                not isinstance(app["app_version"], str) or app["app_version"] == ""
            ):
                errors.append(
                    "'application.app_version' must be a non-empty string if given"
                )

    size = doc.get("window_size")
    if "window_size" in doc:
        if not isinstance(size, dict):
            errors.append(
                f"'window_size' must be an object; found: {type(size).__name__}"
            )
        else:
            for extra in sorted(set(size) - _SIZE_FIELDS):
                errors.append(
                    f"'window_size.{extra}' unknown field; allowed: width, height"
                )
            for dim in ("width", "height"):
                if dim not in size:
                    errors.append(f"'window_size.{dim}' required")
                elif not _is_int(size[dim]):
                    errors.append(
                        f"'window_size.{dim}' must be an integer (not boolean); "
                        f"found: {type(size[dim]).__name__}"
                    )
                elif size[dim] < 1:
                    errors.append(
                        f"'window_size.{dim}' must be positive; found: {size[dim]}"
                    )

    digest = doc.get("reference_image_hash")
    if "reference_image_hash" in doc:
        if not isinstance(digest, str):
            errors.append(
                "'reference_image_hash' must be a string; "
                f"found: {type(digest).__name__}"
            )
        elif _HASH_RE.fullmatch(digest) is None:
            errors.append(
                "'reference_image_hash' must be 64 lowercase hex "
                "(hashlib.hexdigest canonical form)"
            )

    bounds = _window_bounds(doc) if isinstance(doc, dict) else None

    regions = doc.get("safe_regions")
    if "safe_regions" in doc:
        if not isinstance(regions, list):
            errors.append(
                f"'safe_regions' must be a list; found: {type(regions).__name__}"
            )
        elif not regions:
            errors.append(
                "'safe_regions' must contain at least 1 region; a regionless profile cannot resolve"
            )
        else:
            seen: set[str] = set()
            for pos, region in enumerate(regions):
                where = f"'safe_regions[{pos}]'"
                if not isinstance(region, dict):
                    errors.append(
                        f"{where} must be an object; found: {type(region).__name__}"
                    )
                    continue
                for extra in sorted(set(region) - _REGION_FIELDS):
                    errors.append(
                        f"{where}.{extra} unknown field; allowed: name, rect, "
                        "description"
                    )
                name = region.get("name")
                if "name" not in region:
                    errors.append(f"{where}.name required")
                elif not isinstance(name, str) or name.strip() == "":
                    errors.append(f"{where}.name must be a non-empty string")
                elif name in seen:
                    errors.append(f"{where}.name duplicated region name: {name!r}")
                else:
                    seen.add(name)
                rect = region.get("rect")
                if "rect" not in region:
                    errors.append(f"{where}.rect required ([left, top, right, bottom])")
                else:
                    errors.extend(_rect_errors(f"{where}.rect", rect, bounds))
                if "description" in region and not isinstance(
                    region["description"], str
                ):
                    errors.append(
                        f"{where}.description must be a string; "
                        f"found: {type(region['description']).__name__}"
                    )

    reference_region_name = doc.get("reference_region_name")
    reference_region_rect: list[int] | None = None
    if "reference_region_name" in doc:
        if (
            not isinstance(reference_region_name, str)
            or reference_region_name.strip() == ""
        ):
            errors.append("'reference_region_name' must be a non-empty string")
        elif isinstance(regions, list):
            matching_regions = [
                region
                for region in regions
                if isinstance(region, dict)
                and region.get("name") == reference_region_name
            ]
            if len(matching_regions) != 1:
                errors.append(
                    "'reference_region_name' must occur exactly once "
                    f"inside safe_regions: {reference_region_name!r}"
                )
            elif isinstance(matching_regions[0].get("rect"), list):
                reference_region_rect = matching_regions[0]["rect"]

    points = doc.get("verification_points")
    if "verification_points" in doc:
        if not isinstance(points, list):
            errors.append(
                f"'verification_points' must be a list; found: {type(points).__name__}"
            )
        elif not points:
            errors.append(
                "'verification_points' must contain at least 1 point; a "
                "profile without a canary cannot prove its validity"
            )
        else:
            for pos, point in enumerate(points):
                where = f"'verification_points[{pos}]'"
                if not isinstance(point, dict):
                    errors.append(
                        f"{where} must be an object; found: {type(point).__name__}"
                    )
                    continue
                for extra in sorted(set(point) - _POINT_FIELDS):
                    errors.append(
                        f"{where}.{extra} unknown field; allowed: point, expected_color"
                    )
                coords = point.get("point")
                if "point" not in point:
                    errors.append(f"{where}.point required ([x, y])")
                elif (
                    not isinstance(coords, list)
                    or len(coords) != 2
                    or not all(_is_int(v) for v in coords)
                ):
                    errors.append(f"{where}.point must be an [x, y] integer pair")
                elif bounds is not None and not (
                    0 <= coords[0] < bounds[0] and 0 <= coords[1] < bounds[1]
                ):
                    errors.append(
                        f"{where}.point outside window bounds "
                        f"({bounds[0]}x{bounds[1]}): {coords}"
                    )
                elif reference_region_rect is not None and not (
                    reference_region_rect[0] <= coords[0] < reference_region_rect[2]
                    and reference_region_rect[1] <= coords[1] < reference_region_rect[3]
                ):
                    errors.append(
                        f"{where}.point outside reference region "
                        f"{reference_region_name!r}: {coords}"
                    )
                color = point.get("expected_color")
                if "expected_color" not in point:
                    errors.append(f"{where}.expected_color required ([r, g, b])")
                elif (
                    not isinstance(color, list)
                    or len(color) != 3
                    or not all(_is_int(v) for v in color)
                    or not all(0 <= v <= 255 for v in color)
                ):
                    errors.append(
                        f"{where}.expected_color must be [r, g, b] (each "
                        "channel a 0-255 integer)"
                    )

    created = doc.get("created_at")
    if "created_at" in doc:
        if not isinstance(created, str):
            errors.append(
                f"'created_at' must be a string; found: {type(created).__name__}"
            )
        else:
            try:
                moment = parse_offset_aware(created.strip())
            except Iso8601Error as exc:
                errors.append(f"'created_at' {exc}")
            else:
                if moment > datetime.now(timezone.utc):
                    errors.append(
                        f"'created_at' cannot be in the future ({created!r}); the "
                        "record moment lies in the past"
                    )
    return errors


def _rect_errors(field: str, rect: object, bounds: tuple[int, int] | None) -> list[str]:
    if (
        not isinstance(rect, list)
        or len(rect) != 4
        or not all(_is_int(v) for v in rect)
    ):
        return [f"{field} must be a [left, top, right, bottom] integer quadruple"]
    left, top, right, bottom = rect
    if not (left < right and top < bottom):
        return [f"{field} must be ordered (left<right, top<bottom): {rect}"]
    if left < 0 or top < 0:
        return [f"{field} must not carry negative coordinates: {rect}"]
    if bounds is not None and not (right <= bounds[0] and bottom <= bounds[1]):
        return [f"{field} outside window bounds ({bounds[0]}x{bounds[1]}): {rect}"]
    return []


def validate_profile(profile: dict) -> None:
    """Enforce schema + cross-field rules; PermissionError on violation.

    An invalid profile is a PERMISSION decision (NOT `ValueError`):
    consistent with the `policy_file.py`/`confirmation.py` pattern — a
    malformed input is in the SAME result class as a missing env var
    (access is denied). Returns None on success.
    """
    problems = validate_profile_document(profile)
    if problems:
        raise PermissionError("coordinate profile invalid: " + "; ".join(problems))
    return None


def load_profile_file(path: str) -> dict:
    """Read a profile file, validate it, return the dict.

    Unreadable files raise `OSError`, invalid content raises
    `PermissionError` (the broken-probe vs. invalid-content split is the
    same contract as `policy_file.py`).
    """
    try:
        doc = read_profile_document(path)
    except ProfileParseError as exc:
        raise PermissionError(
            f"could not parse coordinate profile ({path}): {exc}"
        ) from exc
    validate_profile(doc)
    return doc


def resolve_point(profile: dict, region_name: str) -> tuple[int, int]:
    """Return the CENTER point of the named region (rounded down).

    An unknown name raises `PermissionError` and the message lists the
    available names (so the operator sees the typo instead of guessing).
    """
    regions = profile.get("safe_regions") if isinstance(profile, dict) else None
    if not isinstance(regions, list):
        raise PermissionError("profile has no resolvable safe_regions; profile invalid")
    names = [region.get("name") for region in regions if isinstance(region, dict)]
    for region in regions:
        if isinstance(region, dict) and region.get("name") == region_name:
            rect = region.get("rect")
            if (
                not isinstance(rect, list)
                or len(rect) != 4
                or not all(_is_int(v) for v in rect)
            ):
                raise PermissionError(
                    f"'{region_name}' region has a corrupt rect; profile invalid"
                )
            left, top, right, bottom = rect
            return ((left + right) // 2, (top + bottom) // 2)
    known = ", ".join(sorted(str(name) for name in names if name)) or "(none)"
    raise PermissionError(
        f"unknown region {region_name!r}; regions in profile: {known}"
    )


def profile_matches_live_window(
    profile: dict,
    live_width: int,
    live_height: int,
    live_reference_image_bytes: bytes | None,
) -> tuple[bool, str]:
    """Does the live window pass the profile gates? (matched, reason).

    SIDE-EFFECT-FREE pure comparison: NO Win32/screen calls. The size
    gate is checked first, then (if bytes were given) the reference-hash
    gate; if the profile itself is invalid the result is `(False, rule)`.
    With no bytes given returns `(True, ...)` BUT the reason EXPLICITLY
    says the content check was SKIPPED (so it is not mistaken for full
    verification).
    """
    problems = validate_profile_document(profile)
    if problems:
        return False, f"profile invalid: {problems[0]}"
    size = profile["window_size"]
    if (
        not _is_int(live_width)
        or not _is_int(live_height)
        or live_width < 1
        or live_height < 1
    ):
        return False, f"invalid live size: {live_width!r}x{live_height!r}"
    if (live_width, live_height) != (size["width"], size["height"]):
        return (
            False,
            "size mismatch: profile "
            f"{size['width']}x{size['height']}, live {live_width}x{live_height}",
        )
    if live_reference_image_bytes is None:
        return (
            True,
            f"size matches ({live_width}x{live_height}); reference hash "
            "not provided, content check skipped",
        )
    if not isinstance(live_reference_image_bytes, bytes):
        return (
            False,
            "live reference image must be bytes; "
            f"found: {type(live_reference_image_bytes).__name__}",
        )
    live_digest = hashlib.sha256(live_reference_image_bytes).hexdigest()
    expected = profile["reference_image_hash"]
    if live_digest != expected:
        return (
            False,
            f"reference image hash mismatch: profile {expected}, live {live_digest}",
        )
    return True, f"matched: {live_width}x{live_height}, reference hash verified"


def pixel_matches(point: dict, rgb: tuple[int, int, int] | list[int]) -> bool:
    """Does a verification-point definition + live RGB match? (pure comparison).

    Exact match is required (NO tolerance); structural corruption returns
    False (deny direction, not a false alarm). Pixel SAMPLING belongs to
    server.py (PIL + screen access lives there); this only compares.
    """
    if not isinstance(point, dict):
        return False
    expected = point.get("expected_color")
    if (
        not isinstance(expected, list)
        or len(expected) != 3
        or not all(_is_int(v) and 0 <= v <= 255 for v in expected)
    ):
        return False
    if (
        not isinstance(rgb, (tuple, list))
        or len(rgb) != 3
        or not all(_is_int(v) and 0 <= v <= 255 for v in rgb)
    ):
        return False
    return list(rgb) == expected
