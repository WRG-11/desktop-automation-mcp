# Migrating from environment variables to the policy file (migration guide)

**Update (2026-09-15): this migration is COMPLETE and is a working
feature today.** The server (`src/desktop_automation_mcp/policy.py`) now
reads the file in `schema/policy.schema.json` format when the
`DESKTOP_AUTOMATION_POLICY_FILE` environment variable is set, validates
it against the schema plus cross-field rules, and enforces it; when it is
NOT set, it falls back to the legacy environment-variable-based behavior
(table below). **The two sources are never merged**: if one of the legacy
variables (e.g. `DESKTOP_AUTOMATION_ALLOWED_TITLES`) is also set while the
file is active, the server rejects with a `PermissionError` — naming which
two conflict. Focus mode (`DESKTOP_AUTOMATION_FOCUS_MODE`) and text mode
(`DESKTOP_AUTOMATION_TEXT_MODE`) are two fields with STILL no counterpart
in the schema — even when the file is active, both continue to be read from
the environment variable (a deliberate, not-yet-decided gap, also noted
separately below).

## Switching to the file: single step

```powershell
$env:DESKTOP_AUTOMATION_POLICY_FILE = "C:\yol\ruffle-policy.yaml"
```

The moment this variable is set, all subsequent actions in the same MCP
session (no restart REQUIRED — `policy.py` re-reads the file uncached on
every call) use the `application.title_patterns`/`executable_path`/
`allowed_actions` triple from the file. Do NOT set the legacy
`DESKTOP_AUTOMATION_ALLOWED_TITLES`/`_PROCESS_PATHS`/`_ACTIONS`
variables in that session — if set, the conflict rejection above is
triggered.

## Environment-variable-based path (still valid when the file is not set)

In the `desktop-automation` entry inside `~/.claude/.mcp.json` (details
are in README.md):

| Variable | Meaning |
|---|---|
| `DESKTOP_AUTOMATION_ALLOWED_TITLES` | Comma-separated, case-insensitive title glob patterns (`*Ruffle*,*Notepad*`). |
| `DESKTOP_AUTOMATION_ALLOWED_PROCESS_PATHS` | Permitted full exe paths; separated by semicolons when there is more than one. |
| `DESKTOP_AUTOMATION_ALLOWED_ACTIONS` | Comma-separated actions (a subset of `observe,hover,click,text,key,close`); if missing, the server grants no tool any permission. |
| `DESKTOP_AUTOMATION_FOCUS_MODE` | Default `passive`; only the explicit `activate` value enables automatic-focus behavior. |
| `DESKTOP_AUTOMATION_TEXT_MODE` | Default UTF-16 `SendInput` path; only the explicit `wm_char` value selects the legacy compatibility mode. |

## Side-by-side comparison: Ruffle example

The working Ruffle entry in the README and its counterpart in
`schema/examples/ruffle-policy.yaml` — both work in practice today:

| Environment variable | Schema field |
|---|---|
| `DESKTOP_AUTOMATION_ALLOWED_TITLES="*Ruffle*"` | `application.title_patterns: ["*Ruffle*"]` |
| `DESKTOP_AUTOMATION_ALLOWED_PROCESS_PATHS="C:\Program Files\ruffle\bin\ruffle.exe"` | `application.executable_path: "C:\\Program Files\\ruffle\\bin\\ruffle.exe"` |
| `DESKTOP_AUTOMATION_ALLOWED_ACTIONS="observe,hover,click,key"` | `application.allowed_actions: [observe, hover, click, key]` |
| No counterpart (`text`/`close` are also requested separately in env, but the "protected class" concept exists ONLY in the schema) | `application.protected_actions` (optional; must be a subset of `allowed_actions` — **ENFORCED since 2026-09-17 as a union over the built-in `text`/`close`/`drag` set**, see below) |
| `DESKTOP_AUTOMATION_FOCUS_MODE` | No counterpart — there is no focus-mode field in the schema; it is still read from env even when the file is active |
| `DESKTOP_AUTOMATION_TEXT_MODE` | No counterpart — there is no text-mode field in the schema; it is still read from env even when the file is active |
| `DESKTOP_AUTOMATION_ALLOW_UNRESTRICTED_SCREENSHOTS=true` (legacy compatibility only; default off) | `application.safe_regions` plus optional `screenshot_masks` and `max_screenshot_bytes`; in file mode there is no image without a named region |
| No counterpart | `expires_at` (optional, validated in the schema) |

## `protected_actions` enforcement (closed 2026-09-17)

`policy_file.py` validates the `protected_actions` field in a file against
the schema plus cross-field rules (it must be a subset of
`allowed_actions`, etc.), and `server.py` now ENFORCES it: the resolved
app's list is passed into the confirmation check as `extra_protected`,
unioned with the fixed `PROTECTED_ACTION_CLASSES` set in
`confirmation.py` (`text`, `close`, `drag`). In other words, the file can
ADD protection (e.g. listing `click` makes token-less `click_window` fail
with an `input_rejected` denial), but writing `protected_actions` without
`text`/`close`/`drag` does NOT lift the approval requirement for them.
One carve-out: an `observe` entry validates but never gates reads.

## Validating the policy file

```powershell
py -3.12 tools/validate_policy.py schema/examples/ruffle-policy.yaml
```

Actual output:

```text
OK: schema/examples/ruffle-policy.yaml
```

Exit code `0` = valid; `1` = invalid (the violated rule is printed item by
item); `2` = file unreadable/corrupt (a broken probe is not the same as an
invalid policy). The validator proves the SHAPE correctness of the file; if
you additionally want to prove that the server actually ENFORCES it, set
`DESKTOP_AUTOMATION_POLICY_FILE` and make a real `list_windows()` call,
then observe that only windows matching the file's
`title_patterns`/`executable_path` are returned (the `policy` section of
`health_check()` returns only `configured`/`ok` — it does NOT separately
show whether the source is the file or env).

## Rollback

If an error occurs during migration, returning to the environment variable
is always possible: the currently working entry (the `env` block inside
`~/.claude/.mcp.json`) MUST BE KEPT throughout the migration attempt, never
deleted. When the policy file is active, the two sources are not merged; on conflict, rejection is
returned. So if a field is mistyped during the switch to the file, the
result is not "running with the old setting" but "rejection" — the rollback
path is to remove the file path and fall back to the environment variable.
