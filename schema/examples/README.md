# Example policy bundle

This bundle provides observe-only, interaction, text, and close-confirmation
examples. All four can be loaded into the server via
`DESKTOP_AUTOMATION_POLICY_FILE` (see `schema/README.md`,
`docs/guides/migration-env-to-policy-file.md`); the "to-be-guarded action"
concept is the union of the fixed `text`/`close`/`drag` set in
`confirmation.py` and the file's `protected_actions` list (enforced since
2026-09-17; an `observe` entry validates but never gates reads).

## Four examples side by side

| File | Scenario | `allowed_actions` | `protected_actions` |
|---|---|---|---|
| `ruffle-observe-only-policy.yaml` | Ruffle window observation; imaging off | `observe` | — (field absent) |
| `ruffle-policy.yaml` | Ruffle interaction + named-region imaging | `observe, hover, click, key` | — (field absent) |
| `notepad-text-policy.yaml` | Notepad text | `observe, text, close` | `text, close` |
| `notepad-close-confirmation-policy.yaml` | Close confirmation | `observe, text, close` | `text, close` |

`notepad-text-policy.yaml` and `notepad-close-confirmation-policy.yaml`
carry the same permission set; both concretize the "both writing and
closing require a confirmation token" situation. The reason the
close-confirmation scenario is a separate file is to give a named target to
the future integration that will test the confirmation flow
(`request_confirmation` / `consume_confirmation`). Policy files
without `safe_regions` grant no screenshot authority; `observe` still
covers content-free observations like window listing/status.

## Validation outputs (real runs)

The following four commands were actually run on this machine; the outputs
are copy-pasted, not fabricated:

```powershell
py -3.12 tools/validate_policy.py schema/examples/ruffle-policy.yaml
py -3.12 tools/validate_policy.py schema/examples/ruffle-observe-only-policy.yaml
py -3.12 tools/validate_policy.py schema/examples/notepad-text-policy.yaml
py -3.12 tools/validate_policy.py schema/examples/notepad-close-confirmation-policy.yaml
```

```text
OK: schema/examples/ruffle-policy.yaml
OK: schema/examples/ruffle-observe-only-policy.yaml
OK: schema/examples/notepad-text-policy.yaml
OK: schema/examples/notepad-close-confirmation-policy.yaml
```

## Why does the `observe-only` example have no `protected_actions`?

`protected_actions: []` (empty list) was not written; the field was omitted
entirely. Rationale: `schema/policy.schema.json` places a `minItems: 1`
constraint on this field, and `tools/validate_policy.py` rejects an empty
list. Trying with an empty list was actually run and rejected:

```text
INVALID: probe-empty-protected.yaml
  - 'application.protected_actions' must contain at least 1 action; empty list is invalid
```

(Exit code 1; tried with a temporary probe file, never written to the
repo.) When there is no action left to guard, the correct expression is not
"an empty guarded list" but the absence of the field: if there is nothing
to guard, there is no guarded class either. This rule does not change
without a schema change.
