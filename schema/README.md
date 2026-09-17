# Schema reference

This directory contains the policy, confirmation-token, audit-event, and
coordinate-profile contracts. These schemas are bound to real code:
`policy_file.py` is read by
`server.py` via the `DESKTOP_AUTOMATION_POLICY_FILE` environment
variable; the file-based policy and the legacy environment-variable-based
policy (`DESKTOP_AUTOMATION_ALLOWED_TITLES` etc.) CANNOT be active AT THE
SAME TIME — if both are set, it is rejected with `PermissionError`
(conflicts are not merged). `confirmation.py` is likewise bound to the
`request_confirmation`/`close_window`/`send_text`/`drag_window` MCP
tools, and since 2026-09-17 the file's `protected_actions` list additionally
extends that protection (union) to `click`/`hover`/`key` when listed.
Focus/text transport modes (`DESKTOP_AUTOMATION_
FOCUS_MODE`/`_TEXT_MODE`) are still read from environment variables only —
there is no counterpart in the schema; the migration guide notes this
separately (`docs/guides/migration-env-to-policy-file.md`).

## Contents

- `policy.schema.json` — the official JSON Schema (draft 2020-12)
  definition of the application policy. There is
  `additionalProperties: false` at the root and `application` level:
  unknown fields are not silently swallowed, they produce an explicit
  error (consistent with the project's "default deny" principle).
- `examples/ruffle-policy.yaml` — the README's Ruffle example written
  against the new schema (`*Ruffle*`, `ruffle.exe`,
  `observe,hover,click,key`).
- `examples/notepad-text-policy.yaml` — Notepad text-flow example; `text`
  and `close` are allowed but in the guarded class (`protected_actions`).
- `confirmation_token.schema.json` — single-use, short-lived confirmation
  token for guarded actions (`text`, `close`, `drag`). Bound to the target
  identity (`hwnd`/`pid`/`process_started_at` — same as the `TargetSnapshot`
  triple), the action class, and the expiry.
- `audit_event.schema.json` — code-integrated redacted audit event per
  action (time, 12-lowercase-hex correlation id, policy id, action type,
  outcome, denial reason, duration; optional tool name and numeric/boolean
  privacy summary). Image/text/title/region name are NEVER carried; the
  `additionalProperties: false` on the root and privacy objects prevents a
  sensitive field from being silently added.
- `examples/confirmation_token-close-action.json` — example of a valid
  `close` token.
- `examples/audit_event-denied-text.json` — example audit event for a
  `text` denial (`outcome: denied` + meaningful `denial_reason`).
- `coordinate_profile.schema.json` — versioned, verifiable coordinate map
  for canvas applications without UIA. Profile =
  application + window size + `reference_region_name` + SHA-256 hash of the
  masked allowed region's PNG + safe regions + verification points + record
  moment. The image itself is never stored, only its hash. The profile does
  not store the DPI value as a separate field: `window_size` and the masked
  reference-region hash make scale changes fail safe. Produce a separate
  profile for each supported real DPI scale and state the scale explicitly
  in the file name/`profile_id`; never reuse one scale's profile adapted to
  another.
- `examples/coordinate_profile-ruffle-800x600.json` — Ruffle 800x600
  example (hash generated from a synthetic byte sequence, no real
  screenshot). Shape-identical to what the `record_coordinate_profile`
  MCP tool returns (same eight fields, validated with the real
  `coordinate_profile.validate_profile` before the tool returns), so the
  example doubles as the recorded-output contract.

## Relationship: token → audit

An audit event is produced WHEN an issued confirmation token IS CONSUMED —
but there is DELIBERATELY NO direct reference/foreign-key field between
this schema set: the audit event must stand on its own via `correlation_id`
and must not have to chain to the token identity. Rationale: audit records
are also kept for actions denied before any token could be produced
(`denied` outcome); no record's existence may depend on a token.

## Validation

No extra dependencies; the scripts use only the standard library (no
package needed, including PyYAML) and accept both YAML and JSON policy
files:

```powershell
py -3.12 tools/validate_policy.py schema/examples/ruffle-policy.yaml
py -3.12 tools/validate_policy.py schema/examples/notepad-text-policy.yaml
py -3.12 tools/validate_confirmation_token.py schema/examples/confirmation_token-close-action.json
py -3.12 tools/validate_audit_event.py schema/examples/audit_event-denied-text.json
py -3.12 tools/validate_coordinate_profile.py schema/examples/coordinate_profile-ruffle-800x600.json
```

Exit codes are deliberately distinct (a broken probe is not the same as an
invalid policy):

| Code | Meaning |
|---:|---|
| 0 | Policy VALID (prints `OK`). |
| 1 | Policy INVALID; each violated rule is printed item by item. |
| 2 | Probe/file error: file not found, unreadable, or malformed YAML/JSON. |

Checked rules (summary): `executable_path` is required and cannot be empty;
`title_patterns` contains at least 1 pattern; `allowed_actions` takes only
the `observe, hover, click, text, key, close, drag` values (exactly matching
`KNOWN_ACTIONS` on the server); `protected_actions` is a subset of this set
(conflict is a rejection reason); if present, `expires_at` must contain a
numeric UTC offset (like `+03:00`; bare `Z` or offset-less is not accepted)
and cannot be a past date. The offset-ISO-8601 + expiry logic is shared
across the three validators; it is deduplicated into the shared
`tools/_iso8601.py` module to avoid code duplication.

The token/audit validators accept JSON only (the YAML support of the policy
validator is DELIBERATELY ABSENT here). Rationale: policy files are written
by human hand (YAML ergonomics), while token and audit events are
machine-produced/consumed records (single-spelling JSON).

Decisions (no silent assumptions):

- While `outcome: allowed`, `denial_reason` MUST NOT be given — the
  schema's else-branch forbids it, the validator rejects it. Rationale: an
  "allowed but with a denial reason" record contradicts itself and misleads
  automated audit scans; the conflict is a schema error rather than being
  silently swallowed.
- While `outcome: denied`/`error`, `denial_reason` must not only EXIST but
  also must not be EMPTY (the validator checks this separately).
- No past-date rejection applies to audit `timestamp` and token
  `consumed_at`: both are retrospective observations/records. `consumed_at`
  is subject only to the format + not-before-`issued_at` rule (consumption
  cannot happen before issuance).
- The example token's `expires_at` value is deliberately set in the far
  future (2035): in production tokens live for minutes, but the static
  example must stay valid for the repo test for many years. The expiry rule
  is real; the example does not break it, it only keeps its horizon far
  away.
- `token_id` follows the real UUID4 pattern (including variant bits).
  Audit `correlation_id` is exactly 12 lowercase hex characters, the
  server's real runtime format; UUID4 or free text is not accepted.
- `profile_id` is a human-readable slug (not a UUID). Rationale: profiles
  are a small number of operator-managed files; readability in error
  messages, file names, and logs is preferred over opaque uniqueness.
  Uniqueness is provided by operator discipline + file-name convention, not
  randomness.
- Verification points carry only `expected_color` ([r,g,b]) and all must
  lie inside the safe region selected by `reference_region_name`.
  `reference_image_hash` belongs to the PNG output of the same named region
  with policy masks applied. Matching is EXACT, there is no tolerance; a
  different version/theme is not silently approved, the profile is rejected
  and the operator re-records.
- `reference_image_hash` accepts only lowercase 64-hex
  (`hashlib.hexdigest` canonical form); the uppercase variant is rejected
  to prevent normalization errors.
- The profile validator accepts JSON only (no policy-style YAML support
  HERE). Rationale: in machine data like hashes + exact rects, YAML's
  implicit typing risks silent corruption; JSON's explicitness is safer.
- `validate_profile` (and `resolve_point` in an unknown region) raises
  `PermissionError` (NOT `ValueError`). Rationale: a corrupt profile is in
  the SAME outcome class as a missing env variable (access denied);
  consistent with the `policy_file.py`/`confirmation.py` pattern.
- `safe_regions` and `verification_points` have at least 1 element each: a
  region-less profile cannot resolve, a canary-less profile cannot prove
  its validity. They are also BOUNDED above (`MAX_PROFILE_REGIONS`=64,
  `MAX_PROFILE_POINTS`=256, enforced in `server.py` upfront validation):
  a 100k-region/100k-point document would otherwise turn recording into
  an unbounded allocation loop. The bounds live in code, not in this
  JSON Schema (cross-field/size policy like the other validator rules).
- `reference_region_name` must be found exactly once in `safe_regions`. At
  live check, the profile process path matches the target process path
  one-to-one and the profile region rect matches the active policy region
  rect one-to-one; the check uses the same mask, pixel/byte/rate/memory,
  and geometry-TOCTOU chain.
- `created_at` is retrospective (record moment), but a FUTURE date is
  rejected (corrupt provenance signal).
- The hash in the example profile is NOT from a REAL screenshot: it is the
  SHA-256 of the synthetic `b"ruffle-reference-800x600-synthetic-v1"` byte
  sequence (so the test deterministically proves matching with the same
  bytes). Rect/color values are also examples, not real UI measurements.

## Test

```powershell
py -3.12 -m unittest discover -s tests -p "test_policy_schema.py" -v
py -3.12 -m unittest discover -s tests -p "test_confirmation_token_schema.py" -v
py -3.12 -m unittest discover -s tests -p "test_audit_event_schema.py" -v
py -3.12 -m unittest discover -s tests -v
```
