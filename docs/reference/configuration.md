# Configuration reference

The server starts with no window or action authority. This page lists every
setting that grants or limits it. For a first least-privilege setup, start with
the [quickstart](../guides/quickstart.md).

## Target policy (environment variables)

Install the runtime package and register it as `desktop-automation` in your
MCP client configuration:

```powershell
py -3.12 -m pip install .
```

```json
{
  "mcpServers": {
    "desktop-automation": {
      "command": "py",
      "args": ["-3.12", "C:\\path\\to\\desktop-automation-mcp\\server.py"],
      "env": {
        "DESKTOP_AUTOMATION_ALLOWED_TITLES": "*Ruffle*",
        "DESKTOP_AUTOMATION_ALLOWED_PROCESS_PATHS": "C:\\Program Files\\ruffle\\bin\\ruffle.exe",
        "DESKTOP_AUTOMATION_ALLOWED_ACTIONS": "observe,hover,click,key"
      }
    }
  }
}
```

Because it is a user-level config, it can be used **in all projects**,
not just one. A newly added/changed MCP server generally takes effect
after the MCP client restarts the server.

The server has no access to any window by default. DESKTOP_AUTOMATION_ALLOWED_TITLES
are comma-separated, case-insensitive title glob patterns; example:
*Ruffle*,*Notepad*. DESKTOP_AUTOMATION_ALLOWED_PROCESS_PATHS are the corresponding
allowed full executable paths; use a semicolon for multiple paths.
A window must have
both an allowed title and an allowed process. New settings take effect in the next MCP session.

`DESKTOP_AUTOMATION_ALLOWED_ACTIONS` is also mandatory: if it is missing, the server
authorizes no tool. Comma-separated values may be `observe`, `hover`, `click`,
`text`, `key`, `close`, and `drag`. For least privilege, add only
what you need. Because `close` can close the window, `text` can
change content, and `drag` can permanently change the target's content
(reordering, drag-and-drop), they are not included in the default example for
observation or game flows. `double_click_window`/`right_click_window`/
`scroll_window` use the `click` permission (they have no separate action names — they are
part of the mouse-click family); `drag_window` checks only the `drag` permission.
A tool added later does not automatically gain authorization.

`DESKTOP_AUTOMATION_FOCUS_MODE` defaults to `passive`: the tool does not bring the target
window to the front; it only acts on a visible window the operator has already brought forward.
This way the keyboard focus of your background work is not stolen. The old
auto-focus behavior is enabled only with an explicit `activate` value. Because `click`
and `hover` still move the operating system's shared cursor, do not authorize them
during active use.

## Policy file

Title/process/action permissions can also be read from a version-controlled
policy file instead of environment variables ([`schema/policy.schema.json`](../../schema/policy.schema.json) format,
examples under [`schema/examples/`](../../schema/examples/)):

```json
"DESKTOP_AUTOMATION_POLICY_FILE": "C:\\path\\to\\desktop-automation-mcp\\schema\\examples\\ruffle-policy.yaml"
```

Rules: if ONLY the file is set, the three decisions above come from this file;
if a legacy variable for the file (`ALLOWED_TITLES` / `ALLOWED_PROCESS_PATHS` /
`ALLOWED_ACTIONS`) is set AT THE SAME TIME, the two sources are not merged — access is
denied (`PermissionError` — there is no precedence deciding which one "wins").
If the file is corrupt, off-schema, or expired, access is denied; if the file cannot be
read at all (missing, directory), a separate read error is raised. `FOCUS_MODE` and
`TEXT_MODE` are not in the schema; in file mode they continue to be read from
environment variables. One file = one application: `executable_path` in the schema is a SINGULAR
string, so multi-path env setups joined with semicolons cannot be moved into a file
one-to-one. The policy file is limited to 1 MiB; duplicate keys in JSON
and structures outside the supported narrow YAML subset are rejected.

In file mode, a screenshot can only be taken with a region named inside `safe_regions`;
if no region is defined, the image is denied. In legacy environment-variable
mode, images are also off by default. Only if backward compatibility
is required can the full-window crop path be explicitly enabled with
`DESKTOP_AUTOMATION_ALLOW_UNRESTRICTED_SCREENSHOTS=true`. This flag does not relax the policy
file mode. Validate a draft file before applying it:

```powershell
py -3.12 tools/validate_policy.py schema/examples/ruffle-policy.yaml
```

## Coordinate profiles

A versioned, verifiable coordinate map for canvas applications without UIA, such as Ruffle
([`schema/coordinate_profile.schema.json`](../../schema/coordinate_profile.schema.json) format,
example `schema/examples/coordinate_profile-ruffle-800x600.json`):
application + window size + `reference_region_name` + the PNG SHA-256 hash of this masked allowed
region + safe regions + verification points.
All verification points must be inside the reference region. The image
itself is not stored, only its hash.
Validate the draft:

```powershell
py -3.12 tools/validate_coordinate_profile.py schema/examples/coordinate_profile-ruffle-800x600.json
```

Two MCP tools use these profiles:

- `resolve_coordinate_profile_point(profile_path, region_name)` — read-only,
  makes no Win32/screen calls and requires no permission; returns the
  center coordinates of a named region in the profile. An unknown `region_name`
  is rejected with an explicit error listing the existing region
  names.
- `check_coordinate_profile(profile_path, title_contains=None, hwnd=None)` —
  uses the `observe` permission; the live process path and the profile path, and the
  active policy rect and the profile `reference_region_name` rect, must match
  exactly. It goes through the **same named-region, mask, pixel/PNG-byte/
  rate/memory and pre/post-capture geometry-verification chain** as `screenshot_window`.
  The live window size and the masked-region hash are compared against the profile;
  on `matches: false`, the `reason` field reports size versus hash
  mismatches separately. The screenshot itself is neither returned nor
  stored.

## Security defaults and limits

| Default | Meaning | Relaxation |
|---|---|---|
| Default deny | No access unless title, process path, and action permission are all present; unknown action names are denied. | None; a missing setting is deliberately unauthorized. |
| Title + process binding | The window must match both an allowed title pattern and an allowed full exe path. | None. |
| `passive` focus | For every INPUT-EMITTING tool (`click_window`/`send_text`/`send_key`/etc.), if the target was not brought forward by the operator, the tool denies; `passive` is not weaker checking for those tools. | Only `DESKTOP_AUTOMATION_FOCUS_MODE=activate`. |
| Observation is foreground-exempt | `screenshot_window` and `check_coordinate_profile` do NOT require foreground — reading pixels does not steal keyboard focus or change window order, unlike every input-emitting tool. They still require the target to be unobscured (not covered by another window); a fully covered target is rejected the same as an input-emitting one. This means an allowed window can be observed even while the operator is actively working in a different window. | None; this is a deliberate design choice, not a bug. Do not add `screenshot_window`/`check_coordinate_profile` permissions to a policy unless background observability of that window is acceptable. |
| Action-based permission | `observe`, `hover`, `click`, `text`, `key`, `close`, `drag` are requested separately; a new tool does not automatically gain authorization. | Write only what you need into `DESKTOP_AUTOMATION_ALLOWED_ACTIONS`; `text`/`close`/`drag` are a protected class. |
| Unicode text | `send_text` defaults to the UTF-16 `SendInput` path; a pressed unit is left behind on the error path. | Only for explicit compatibility, `DESKTOP_AUTOMATION_TEXT_MODE=wm_char`; unreliable in some shell controls. |
| Action-time verification | Before every effect, HWND/PID/process start time, and visibility are re-bound; foreground is also re-bound for every input-emitting tool (not for `screenshot_window`/`check_coordinate_profile`, see above). | None. |
| Region-limited capture | PNG is never written to disk. Named region is mandatory in file policy; masks are blacked out; 16 MP, PNG byte, rate, and process-wide concurrent memory budgets are enforced. | Only for legacy env mode, explicit `DESKTOP_AUTOMATION_ALLOW_UNRESTRICTED_SCREENSHOTS=true`; does not relax file policy. |
| Limits | Text at most 4096 characters, NUL/empty text forbidden; `hold_ms` 0–5000, `dwell_ms` 0–10000, at most two modifiers. | None. |

`close_window` is a normal `WM_CLOSE` request, not process termination; it is still
in the irreversible class and requires a separate permission. Protected actions like
`text`/`close`/`drag` require a `confirmation_token` (see the
[`request_confirmation`](tools.md#request_confirmationaction_class-title_contains--hwnd-ttl_seconds60) and the [confirmation contract](confirmation.md)) — the `confirmation.py` module issues/consumes
single-use, in-memory, short-lived (TTL at most 300 s) approval tokens.
Each new `issue()` call cleans up expired records under the same lock in amortized fashion;
`purge_expired()` is also kept as an explicit maintenance path. Every action attempt (allow/deny/error, including `screenshot_window`) is additionally
recorded in `audit.py` as a redacted event (time, 12 lowercase-hex correlation id,
policy id, action type, result, duration, and if present tool name + numeric/boolean
privacy summary — title/text/image/region name NEVER) in memory.
The tool response and the audit event share the same correlation ID;
`screenshot_window` carries it in the MCP content `_meta` field. There is no persistent audit
log. There is a per-application rate limit (keyed by normalized
`process_path`) with a 60-second sliding window: actions/min (`DESKTOP_AUTOMATION_MAX_ACTIONS_PER_MINUTE`,
default 120), text characters/min
(`DESKTOP_AUTOMATION_MAX_TEXT_CHARS_PER_MINUTE`, default 20000), and
screenshot pixels/min
(`DESKTOP_AUTOMATION_MAX_SCREENSHOT_PIXELS_PER_MINUTE`, default
`MAX_SCREENSHOT_PIXELS*4`); overruns raise `PermissionError`. Concurrent image-processing
memory is limited by
`DESKTOP_AUTOMATION_SCREENSHOT_MEMORY_BUDGET_BYTES`, read once at server startup (default
128 MiB; allowed range 1 MiB–2 GiB). If no reservation can be made, a backward-compatible
`PermissionError` subclass with stable `code="memory_budget_exceeded"` is returned.

## Focus mode

In the default `passive` mode, tools require the operator to have already brought the target forward;
otherwise they deny without sending any image or input. If
`DESKTOP_AUTOMATION_FOCUS_MODE=activate` is explicitly set, the tool tries to bring the target forward for about two
seconds; the first successful attempt waits only 50ms to settle.
