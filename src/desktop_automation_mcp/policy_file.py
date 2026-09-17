"""Version-controlled policy file support ("which window, which action?" v2).

This module is part of the SHIPPED package: it reads, schema-validates and
cross-validates a single `schema/policy.schema.json` policy file. `tools/`
stays developer-only CLI surface; the logic that the running server depends
on lives here, so an installed package never needs `tools/` on disk.

Failure contract (fail closed, same spirit as `policy.py`):

- `load_policy_file` raises `PermissionError` (never `ValueError`) on ANY
  validation failure: malformed, schema-violating or expired content denies
  exactly like a missing env var does today.
- It raises `OSError` if the file cannot be read at all (missing path,
  directory, IO error). A broken probe is NOT "invalid content": callers
  must be able to tell "no file" apart from "bad file".

Design decisions (no silent assumptions):

- ONE file = ONE application. `application.executable_path` is a single
  string while the legacy env var accepts several `;`-separated paths.
  File mode therefore supports exactly one executable path (wrapped in a
  one-element set by `policy.py`). Multi-path setups cannot be moved to
  file mode one-to-one; that needs a future multi-document format, not a
  quiet reinterpretation of this schema.
- `protected_actions` is VALIDATED here (schema shape + subset of
  `allowed_actions`). The runtime confirmation mechanism (`confirmation.py`'s
  fixed `text`/`close`/`drag` set EXTENDED by this per-file list, union
  semantics enforced by `server.py`) consumes it. Screenshot regions, masks
  and byte limits ARE strictly validated here and enforced by `server.py`.
  `focus_mode` / `text_mode` have no schema fields and remain
  environment-only. Note: an `observe` entry validates but never gates
  reads — observation is side-effect-free by design and `list_windows` has
  no target to bind a token to; only input classes take effect.
- The schema action enum and `policy.KNOWN_ACTIONS` share seven actions:
  `observe, hover, click, text, key, close, drag`. Every file-granted action
  is runtime-known, so downstream `_require_action` keeps working.
- Zero runtime dependencies: the narrow block-YAML subset parser and the
  offset-aware ISO-8601 checks live here in stdlib-only form. PyYAML is
  NOT required. (The ISO-8601 helpers previously lived in
  `tools/_iso8601.py`; that file is frozen for the token/audit validators
  and this module is now the canonical home for policy validation. The
  remaining duplication across those two homes is known and reported, not
  silently fixed, because those files are out of scope for this change.)
- Policy files are capped at 1 MiB and JSON duplicate keys are rejected.
  Both checks happen before policy values can influence authorization.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from .errors import PolicyDeniedError

# Mirrors the `allowed_actions` / `protected_actions` enum in
# `schema/policy.schema.json`. Deliberately NOT imported from `policy.py`:
# the schema is the stable contract (seven actions), kept in lockstep with
# the runtime set.
SCHEMA_ACTIONS = frozenset(
    {"observe", "hover", "click", "text", "key", "close", "drag"}
)

APPLICATION_FIELDS = frozenset(
    {
        "executable_path",
        "title_patterns",
        "allowed_actions",
        "protected_actions",
        "safe_regions",
        "screenshot_masks",
        "max_screenshot_bytes",
        "expires_at",
    }
)

MIN_SCREENSHOT_BYTES = 1_024
MAX_SCREENSHOT_BYTES = 16 * 1_024 * 1_024
MAX_POLICY_FILE_BYTES = 1 * 1_024 * 1_024
SAFE_REGION_FIELDS = frozenset({"name", "rect"})
SCREENSHOT_MASK_FIELDS = frozenset({"rect"})


def _rect_errors(value: object, where: str) -> list[str]:
    if not isinstance(value, list) or len(value) != 4:
        return [f"{where} must be [left, top, right, bottom] with four integers"]
    if not all(isinstance(part, int) and not isinstance(part, bool) for part in value):
        return [f"{where} must contain only integer coordinates"]
    if any(part < 0 for part in value):
        return [f"{where} cannot contain a negative coordinate"]
    left, top, right, bottom = value
    if left >= right or top >= bottom:
        return [f"{where} must be ordered: left < right and top < bottom"]
    return []


_KEY_RE = re.compile(r"[A-Za-z0-9_-]+")
_OFFSET_SUFFIX_RE = re.compile(r"[+-]\d{2}:?\d{2}$")


class Iso8601Error(ValueError):
    """Structural date-time error (malformed format, missing offset, etc.)."""


class PolicyParseError(ValueError):
    """The file was read but could not be parsed as a YAML/JSON document."""


def parse_offset_aware(raw: str) -> datetime:
    """Converts an offset-aware ISO-8601 string to a timezone-aware datetime.

    Raises `Iso8601Error` on a bare `Z` / offset-less / naive value; the
    message carries no field-name prefix, the caller prepends one.
    """
    text = raw.strip()
    if text.endswith(("Z", "z")):
        raise Iso8601Error(
            "cannot use a bare 'Z'; a numeric UTC offset is required "
            "(e.g. 2027-01-01T00:00:00+03:00)"
        )
    if _OFFSET_SUFFIX_RE.search(text) is None:
        raise Iso8601Error(
            "must include a UTC offset (e.g. +03:00); an offset-less "
            f"date-time is not accepted: {text!r}"
        )
    try:
        moment = datetime.fromisoformat(text)
    except ValueError as exc:
        raise Iso8601Error(
            f"could not parse as an ISO-8601 date-time: {text!r}"
        ) from exc
    if moment.tzinfo is None:
        raise Iso8601Error(
            "is timezone-less (naive); a UTC offset is required (e.g. +03:00)"
        )
    return moment


def is_past(moment: datetime) -> bool:
    """Is the moment in the past relative to now (for expiry checks)?"""
    return moment <= datetime.now(timezone.utc)


def _is_escaped(text: str, position: int) -> bool:
    """Whether the character at `position` has an odd backslash prefix."""
    backslashes = 0
    cursor = position - 1
    while cursor >= 0 and text[cursor] == "\\":
        backslashes += 1
        cursor -= 1
    return backslashes % 2 == 1


def _strip_comment(line: str) -> str:
    """Drops a trailing '#' comment that is outside of quotes."""
    in_single = False
    in_double = False
    for i, ch in enumerate(line):
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single and not _is_escaped(line, i):
            in_double = not in_double
        elif ch == "#" and not in_single and not in_double:
            if i == 0 or line[i - 1] in (" ", "\t"):
                return line[:i]
    return line


_SIMPLE_ESCAPES = {
    "\\": "\\",
    '"': '"',
    "n": "\n",
    "t": "\t",
    "r": "\r",
    "0": "\0",
}


def _unescape_double(body: str, lineno: int, token: str) -> str:
    """Resolves escapes in a double-quoted body; non-ASCII/Unicode passes through unchanged."""
    out: list[str] = []
    i = 0
    while i < len(body):
        ch = body[i]
        if ch == "\\":
            if i + 1 >= len(body):
                raise PolicyParseError(
                    f"line {lineno}: dangling escape at end of value: {token!r}"
                )
            nxt = body[i + 1]
            out.append(_SIMPLE_ESCAPES.get(nxt, "\\" + nxt))
            i += 2
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _parse_scalar(token: str, lineno: int) -> str | int:
    token = token.strip()
    if len(token) >= 2 and token.startswith('"') and token.endswith('"'):
        return _unescape_double(token[1:-1], lineno, token)
    if len(token) >= 2 and token.startswith("'") and token.endswith("'"):
        return token[1:-1].replace("''", "'")
    if token.startswith(('"', "'")):
        raise PolicyParseError(f"line {lineno}: unterminated quote: {token!r}")
    if token == "":
        raise PolicyParseError(f"line {lineno}: empty scalar value")
    if re.fullmatch(r"-?(0|[1-9]\d*)", token):
        return int(token)
    if token.startswith(("[", "{")) or token.endswith(("]", "}")):
        raise PolicyParseError(
            f"line {lineno}: flow collection is not supported "
            f"at this position: {token!r}"
        )
    return token


def _split_flow_items(body: str, lineno: int) -> list[str]:
    """Splits a '[a, b]' body on quote-aware commas."""
    items: list[str] = []
    current: list[str] = []
    in_single = False
    in_double = False
    for position, ch in enumerate(body):
        if ch == "'" and not in_double:
            in_single = not in_single
            current.append(ch)
        elif ch == '"' and not in_single and not _is_escaped(body, position):
            in_double = not in_double
            current.append(ch)
        elif ch == "," and not in_single and not in_double:
            items.append("".join(current))
            current = []
        else:
            current.append(ch)
    if in_single or in_double:
        raise PolicyParseError(
            f"line {lineno}: unterminated quote inside a bracketed list"
        )
    items.append("".join(current))
    return items


def _parse_value(token: str, lineno: int):
    token = token.strip()
    if token.startswith("[") and token.endswith("]"):
        return [
            _parse_scalar(part, lineno)
            for part in _split_flow_items(token[1:-1], lineno)
            if part.strip() != ""
        ]
    return _parse_scalar(token, lineno)


def _logical_lines(text: str) -> list[tuple[int, int, str]]:
    """Filters out blank/comment lines and returns (line-no, indent, content)."""
    logical: list[tuple[int, int, str]] = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        if (
            "\t" in raw
            and raw.strip() != ""
            and raw[: len(raw) - len(raw.lstrip())].find("\t") != -1
        ):
            raise PolicyParseError(
                f"line {lineno}: tabs are not allowed in indentation, use spaces"
            )
        code = _strip_comment(raw).rstrip()
        if code.strip() == "":
            continue
        indent = len(code) - len(code.lstrip(" "))
        logical.append((lineno, indent, code.strip()))
    return logical


def _parse_policy_yaml(text: str) -> dict:
    """Parses the narrow block-YAML subset policy files are written in.

    Supported: `key: value` mappings, `- item` block lists, and `[a, b]`
    inline lists. Unsupported structure raises an explicit
    PolicyParseError; it is never silently swallowed.
    """
    logical = _logical_lines(text)
    if not logical:
        raise PolicyParseError("file is empty: nothing to parse")
    root: dict = {}
    # Stack: (indent, container). The root mapping sits at indent -1.
    stack: list[tuple[int, object]] = [(-1, root)]
    index = 0
    while index < len(logical):
        lineno, indent, content = logical[index]
        while len(stack) > 1 and indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1]
        if content == "-" or content.startswith("- "):
            if not isinstance(parent, list):
                raise PolicyParseError(
                    f"line {lineno}: '-' list item is not inside "
                    "a list; policy hierarchy is broken"
                )
            item_token = content[1:].strip()
            if item_token == "":
                raise PolicyParseError(f"line {lineno}: empty list item after '-'")
            if ":" in item_token:
                key, _, rest = item_token.partition(":")
                key = key.strip()
                rest = rest.strip()
                if not _KEY_RE.fullmatch(key):
                    raise PolicyParseError(
                        f"line {lineno}: invalid list-mapping key: {key!r}"
                    )
                if rest == "":
                    raise PolicyParseError(
                        f"line {lineno}: list-mapping key {key!r} has no value"
                    )
                item = {key: _parse_value(rest, lineno)}
                parent.append(item)
                # Following, more-indented `rect: ...` lines extend this map.
                stack.append((indent, item))
            else:
                parent.append(_parse_scalar(item_token, lineno))
            index += 1
            continue
        if ":" in content:
            key, _, rest = content.partition(":")
            key = key.strip()
            rest = rest.strip()
            if not _KEY_RE.fullmatch(key):
                raise PolicyParseError(f"line {lineno}: invalid mapping key: {key!r}")
            if not isinstance(parent, dict):
                raise PolicyParseError(
                    f"line {lineno}: '{key}' mapping entry cannot appear inside a list"
                )
            if key in parent:
                raise PolicyParseError(f"line {lineno}: duplicate key: {key!r}")
            if rest == "":
                # Nested block: is the next logical line a list or a mapping?
                if index + 1 < len(logical) and logical[index + 1][1] > indent:
                    nxt = logical[index + 1][2]
                    child: object = [] if (nxt == "-" or nxt.startswith("- ")) else {}
                else:
                    raise PolicyParseError(
                        f"line {lineno}: '{key}' has no value; a "
                        "nested block (indented line) was expected"
                    )
                parent[key] = child
                stack.append((indent, child))
            else:
                parent[key] = _parse_value(rest, lineno)
            index += 1
            continue
        raise PolicyParseError(
            f"line {lineno}: unparseable line (supported forms are "
            f"'key: value' or '- item'): {content!r}"
        )
    return root


def read_policy_document(path: str | Path) -> dict:
    """Reads the file and converts the YAML/JSON document into a dict (unvalidated).

    JSON is tried first; if that fails, the narrow YAML subset is tried.
    Read problems ALWAYS raise a plain `OSError` (never `PermissionError`):
    a builtin `PermissionError` from the OS (e.g. reading a directory on
    Windows) is deliberately wrapped, because a broken probe must not be
    confused with a validation denial. Parse problems raise
    `PolicyParseError`. Does NOT perform schema validation; that is owned
    by `validate_policy_document` (or `load_policy_file`, which does both).
    """
    candidate = Path(path)
    if not candidate.exists():
        raise FileNotFoundError(f"file not found: {candidate}")
    if not candidate.is_file():
        raise OSError(f"policy path is not a file: {candidate}")
    try:
        file_size = candidate.stat().st_size
    except OSError as exc:
        raise OSError(f"could not stat policy file ({candidate}): {exc}") from exc
    if file_size > MAX_POLICY_FILE_BYTES:
        raise PolicyParseError(
            f"{candidate}: policy file is {file_size} bytes; the limit "
            f"is {MAX_POLICY_FILE_BYTES} bytes"
        )
    try:
        with candidate.open("rb") as handle:
            raw_bytes = handle.read(MAX_POLICY_FILE_BYTES + 1)
    except OSError as exc:
        raise OSError(f"could not read file ({candidate}): {exc}") from exc
    if len(raw_bytes) > MAX_POLICY_FILE_BYTES:
        raise PolicyParseError(
            f"{candidate}: policy file exceeded the "
            f"{MAX_POLICY_FILE_BYTES}-byte limit while reading"
        )
    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        # Independent security review F-5 (2026-09-14): `UnicodeDecodeError`
        # is a `ValueError` subclass, and letting it pass through unwrapped
        # would break this module's own contract (probe error=OSError,
        # parse error=PolicyParseError) — `load_policy_file` would raise a
        # raw `ValueError`; `_prepare_action_target` would still classify
        # it as denied on the safe side, but the CLI would show a raw
        # traceback instead of a clean "INVALID". A non-UTF-8 (e.g.
        # Latin-1-saved) policy file triggers this.
        raise PolicyParseError(
            f"{candidate}: file could not be decoded as UTF-8: {exc}"
        ) from exc
    if text.strip() == "":
        raise PolicyParseError(f"file is empty: {candidate}")

    def reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict:
        parsed_object: dict = {}
        for key, value in pairs:
            if key in parsed_object:
                raise PolicyParseError(f"duplicate JSON key: {key!r}")
            parsed_object[key] = value
        return parsed_object

    try:
        parsed = json.loads(text, object_pairs_hook=reject_duplicate_keys)
    except PolicyParseError:
        raise
    except json.JSONDecodeError:
        parsed = None
    else:
        if not isinstance(parsed, dict):
            raise PolicyParseError(
                f"{candidate}: JSON root must be an object, "
                f"found: {type(parsed).__name__}"
            )
        return parsed
    try:
        return _parse_policy_yaml(text)
    except PolicyParseError:
        raise
    except Exception as exc:  # pragma: no cover - defensive
        raise PolicyParseError(f"{candidate}: YAML parse error: {exc}") from exc


def _action_list_errors(field: str, value: object) -> list[str]:
    errors: list[str] = []
    known = sorted(SCHEMA_ACTIONS)
    if not isinstance(value, list):
        return [f"'{field}' must be a list; found: {type(value).__name__}"]
    if not value:
        errors.append(
            f"'{field}' must contain at least 1 action; empty list is invalid"
        )
    seen: set[str] = set()
    for pos, item in enumerate(value):
        where = f"'{field}[{pos}]'"
        if not isinstance(item, str):
            errors.append(f"{where} must be a string; found: {type(item).__name__}")
            continue
        if item in seen:
            errors.append(f"{where} duplicate action: {item!r}")
        seen.add(item)
        if item not in SCHEMA_ACTIONS:
            errors.append(
                f"{where} unknown action {item!r}; known actions: " + ", ".join(known)
            )
    return errors


def _expires_at_errors(value: object) -> list[str]:
    field = "'application.expires_at'"
    if not isinstance(value, str):
        return [f"{field} must be a string; found: {type(value).__name__}"]
    text = value.strip()
    if text == "":
        return [f"{field} cannot be empty"]
    try:
        moment = parse_offset_aware(text)
    except Iso8601Error as exc:
        return [f"{field} {exc}"]
    if is_past(moment):
        return [f"{field} is a past date ({value!r}); an expired policy is rejected"]
    return []


def validate_policy_document(doc: object) -> list[str]:
    """Validates the policy dict against the schema; returns a list of violations.

    Empty list = valid. The `protected_actions ⊆ allowed_actions`
    cross-constraint and `expires_at` expiry are enforced here (things the
    schema file cannot express on its own).
    """
    errors: list[str] = []
    if not isinstance(doc, dict):
        return [
            "root node must be an object "
            f"(e.g. an 'application:' mapping); found: {type(doc).__name__}"
        ]
    for extra in sorted(set(doc) - {"application"}):
        errors.append(
            f"unknown field at root level {extra!r}; only 'application' "
            "is allowed (additionalProperties: false)"
        )
    if "application" not in doc:
        errors.append("'application' field is required; policy is empty/rootless")
        return errors
    app = doc["application"]
    if not isinstance(app, dict):
        return errors + [
            f"'application' must be a mapping (object); found: {type(app).__name__}"
        ]
    for extra in sorted(set(app) - APPLICATION_FIELDS):
        errors.append(
            f"'application.{extra}' unknown field; allowed fields: "
            + ", ".join(sorted(APPLICATION_FIELDS))
            + " (additionalProperties: false)"
        )

    exe = app.get("executable_path")
    if "executable_path" not in app:
        errors.append(
            "'application.executable_path' is required; write the full exe path"
        )
    elif not isinstance(exe, str):
        errors.append(
            "'application.executable_path' must be a string; "
            f"found: {type(exe).__name__}"
        )
    elif exe.strip() == "":
        errors.append("'application.executable_path' cannot be empty")

    titles = app.get("title_patterns")
    if "title_patterns" not in app:
        errors.append(
            "'application.title_patterns' is required; write at least one glob pattern"
        )
    elif not isinstance(titles, list):
        errors.append(
            "'application.title_patterns' must be a list; "
            f"found: {type(titles).__name__}"
        )
    elif not titles:
        errors.append("'application.title_patterns' must contain at least 1 pattern")
    else:
        for pos, pattern in enumerate(titles):
            if not isinstance(pattern, str):
                errors.append(
                    f"'application.title_patterns[{pos}]' must be a string; "
                    f"found: {type(pattern).__name__}"
                )
            elif pattern.strip() == "":
                errors.append(f"'application.title_patterns[{pos}]' cannot be empty")

    allowed: list[str] = []
    if "allowed_actions" not in app:
        errors.append(
            "'application.allowed_actions' is required; write at least one action"
        )
    else:
        errors.extend(
            f"'application.{msg[1:]}" if msg.startswith("'") else msg
            for msg in _action_list_errors("allowed_actions", app["allowed_actions"])
        )
        if isinstance(app["allowed_actions"], list):
            allowed = [a for a in app["allowed_actions"] if isinstance(a, str)]

    if "protected_actions" in app:
        errors.extend(
            f"'application.{msg[1:]}" if msg.startswith("'") else msg
            for msg in _action_list_errors(
                "protected_actions", app["protected_actions"]
            )
        )
        protected = app["protected_actions"]
        if isinstance(protected, list) and allowed:
            outside = sorted(
                {p for p in protected if isinstance(p, str)} - set(allowed)
            )
            if outside:
                errors.append(
                    "'application.protected_actions' must be a subset of "
                    "'application.allowed_actions'; protected action(s) not "
                    "in the allowed list: " + ", ".join(outside)
                )
        elif isinstance(protected, list) and not allowed:
            errors.append(
                "'application.protected_actions' is set but "
                "'application.allowed_actions' must contain a valid action; "
                "subset check could not be performed"
            )

    regions = app.get("safe_regions")
    if regions is not None:
        if not isinstance(regions, list):
            errors.append(
                "'application.safe_regions' must be a list; "
                f"found: {type(regions).__name__}"
            )
        else:
            names: set[str] = set()
            for pos, item in enumerate(regions):
                where = f"'application.safe_regions[{pos}]'"
                if not isinstance(item, dict):
                    errors.append(f"{where} must be an object")
                    continue
                unknown = sorted(set(item) - SAFE_REGION_FIELDS)
                if unknown:
                    errors.append(f"{where} unknown field(s): {', '.join(unknown)}")
                name = item.get("name")
                if not isinstance(name, str) or not name.strip():
                    errors.append(f"{where}.name must be a non-empty string")
                elif name in names:
                    errors.append(f"{where}.name duplicate: {name!r}")
                else:
                    names.add(name)
                errors.extend(_rect_errors(item.get("rect"), f"{where}.rect"))

    masks = app.get("screenshot_masks")
    if masks is not None:
        if not isinstance(masks, list):
            errors.append(
                "'application.screenshot_masks' must be a list; "
                f"found: {type(masks).__name__}"
            )
        else:
            for pos, item in enumerate(masks):
                where = f"'application.screenshot_masks[{pos}]'"
                if not isinstance(item, dict):
                    errors.append(f"{where} must be an object")
                    continue
                unknown = sorted(set(item) - SCREENSHOT_MASK_FIELDS)
                if unknown:
                    errors.append(f"{where} unknown field(s): {', '.join(unknown)}")
                errors.extend(_rect_errors(item.get("rect"), f"{where}.rect"))

    max_bytes = app.get("max_screenshot_bytes")
    if max_bytes is not None and (
        not isinstance(max_bytes, int)
        or isinstance(max_bytes, bool)
        or not MIN_SCREENSHOT_BYTES <= max_bytes <= MAX_SCREENSHOT_BYTES
    ):
        errors.append(
            "'application.max_screenshot_bytes' must be an integer "
            f"between {MIN_SCREENSHOT_BYTES}..{MAX_SCREENSHOT_BYTES}"
        )

    if "expires_at" in app:
        errors.extend(_expires_at_errors(app["expires_at"]))
    return errors


def load_policy_file(path: str) -> dict:
    """Read, schema-validate, and cross-validate one policy file.

    Raises PermissionError (not ValueError) on ANY validation failure —
    a malformed or expired policy file must fail CLOSED, the same way a
    missing env var does today. Raises OSError if the file cannot be read
    at all (broken-probe != absence: if the file cannot be read, do not
    confuse that with "invalid").
    """
    try:
        doc = read_policy_document(path)
    except PolicyParseError as exc:
        raise PolicyDeniedError(f"could not parse policy file ({path}): {exc}") from exc
    problems = validate_policy_document(doc)
    if problems:
        raise PolicyDeniedError(
            f"policy file is invalid ({path}): " + "; ".join(problems)
        )
    return doc
