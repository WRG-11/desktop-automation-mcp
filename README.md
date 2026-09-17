# desktop-automation-mcp

A **deny-by-default, per-action-gated** Windows window MCP server — not
another general-purpose desktop-automation tool. Every capability (observe,
click, type, close, drag, ...) is a separate permission that must be granted
explicitly; every effect is re-verified against the live target (identity,
focus, occlusion) immediately before it happens; and unlike accessibility-
tree-based automation, this server never feeds screen content to the MCP
client as text — it hands over pixels and window-relative coordinates, so
there is no channel for on-screen content to be mistaken for instructions.
It also supports UIA-less canvas applications (Flash/Ruffle-style content)
through versioned, hash-verified coordinate profiles.

It lets Claude (or another MCP client) see open windows **safely** and
interact with them — scoped to exactly the titles, executables, and actions
the operator names, nothing more.

## Why it exists

Hand-written one-off PowerShell screenshot scripts failed in one
session in two different ways:

1. **When full-screen was captured without verifying focus** — irrelevant,
   even sensitive other windows (another chat session, a GitHub tab) were
   accidentally captured.
2. **On high-DPI/scaled displays**, `GetWindowRect` and screen capture
   (`ImageGrab`/`CopyFromScreen`) used **different coordinate systems** —
   the window appeared "shifted" and the wrong region (sometimes an entirely
   different window) was captured.

This server fixes both permanently and mechanically — not "be careful",
"REFUSE TO RUN when it is wrong".

For setup, security boundaries, and contributor information, start with the
[documentation index](docs/README.md).

## Project layout

The production package lives under [src/desktop_automation_mcp](src/desktop_automation_mcp).
Regression tests are under [tests](tests), and the preserved one-off
research scripts are under [experiments](experiments). The root
[server.py](server.py) is only a compatibility wrapper so existing
MCP registrations do not break.

For module responsibilities and the guarded action lifecycle, see the
[architecture map](docs/design/architecture-map.md).

## Setup and mandatory target policy

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
in the next Claude Code session/restart.

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

### Configuration with a policy file (new)

Title/process/action permissions can also be read from a version-controlled
policy file instead of environment variables (`schema/policy.schema.json` format,
examples under `schema/examples/`):

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

### Coordinate profile

A versioned, verifiable coordinate map for canvas applications without UIA, such as Ruffle
(`schema/coordinate_profile.schema.json` format,
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

## Security defaults

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
`request_confirmation` section above) — the `confirmation.py` module issues/consumes
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

## Development quality gate

These four local commands are identical to the Windows quality gate in GitHub Actions:

```powershell
py -3.12 -m ruff check src tests tools
py -3.12 -m ruff format --check src tests tools
py -3.12 -m unittest discover -s tests -v
py -3.12 -m compileall -q src tests tools
```

The GitHub workflow [`.github/workflows/quality.yml`](.github/workflows/quality.yml)
installs the package and development dependencies on a clean Windows runner with
Python 3.12. This gate does not run real window automation; Windows UI smoke
tests stay separate and operator-supervised. Chaos scenarios
(`tests/test_chaos.py`) run the real `target.py`/`visibility.py`/`server.py`
functions against shared fake Win32 state (`tests/fake_platform.py`);
the existing `tests/test_server.py` keeps its own mock pattern.

## Design principles

- **No silent failure.** If focus cannot be verified, or another window steps in at the
  exact MOMENT a screenshot is taken, the tool **throws an error** without sending an
  image/click. There is no "it was probably the right window" assumption
  anywhere.
- **DPI awareness is mandatory at process startup** (`SetProcessDpiAwareness`),
  otherwise `GetWindowRect` and screen capture can silently use different coordinate
  systems.
- **Window-relative coordinates.** The `(x, y)` you pass to `click_window`
  is relative to the window's top-left corner — even if the window is moved/repositioned,
  the same `(x, y)` lands on the same UI element.
- **Zero external dependencies.** Only `ctypes` (Win32 API directly) +
  `Pillow` (screen capture) + the `mcp` SDK.
- **No visibility outside allowed targets.** list_windows returns only windows matching the policy
  patterns.
- **Images are never written to disk.** The PNG is produced in memory and returned in the MCP response.
- **No clicking outside the window.** Negative coordinates or coordinates outside the window rect
  are rejected without action.
- **No permanent z-order changes.** If focus cannot be verified, the tool fails
  safely; it never pins the window permanently on top.

## Tools

### `health_check()`

Checks in a read-only way whether the server is configured and operable: policy (each of the
title/process/action sources separately, distinguishing "not configured at all" from
"configured but broken"), dependency versions (`Pillow`, `mcp` — compared against the compatibility
bounds in `pyproject.toml`), and DPI awareness (a real `GetProcessDpiAwareness`
query). **Requires no action permission** — it runs even when no policy is
configured at all, because that is exactly what it exists to diagnose.
It never collects screen content (titles, images, text, window lists).

### `get_rate_limit_state()`

Reports current in-memory rate-limit and screenshot-memory usage (read-only,
`observe` permission): per-resource `limit`, live `total_used` inside the
60 s window, and `tracked_keys` count — never the keys themselves (limiter
keys are application exe paths) — plus `limit_bytes`/`in_use_bytes` for the
concurrent screenshot memory budget. Writes nothing and audits nothing.

### `get_audit_events(limit=20)`

Returns the most recent redacted audit events, newest first (read-only,
`observe` permission; `limit` 1–100). Each event carries timestamp,
correlation id, policy id, action, outcome, duration, plus `denial_reason`
(already redaction-reviewed), `operation`, and numeric/boolean privacy
counters — but never the live `target_identity` triple. Memory-only (the
ring buffer holds at most 1000 events; a restart wipes history), and the
tool itself writes no audit event.

### `get_virtual_screen_bounds()`

Reports the live virtual-desktop bounds for multi-monitor debugging
(read-only, `observe` permission): `bounds` (`[left, top, right, bottom]`),
`origin`, and `size` — the same union rect every screenshot plan is
validated against. A left-hand monitor makes the origin negative (e.g.
`[-1920, 0]`). No per-monitor detail, no parameters, no audit row.

### `resolve_coordinate_profile_point(profile_path, region_name)`

Returns the center coordinates of a named `safe_regions` region in a
[coordinate profile](#coordinate-profile) file. **Read-only, makes no
Win32/screen calls** — it only reads and validates the profile file;
it requires no action permission. An unknown `region_name` is rejected with an explicit error
listing the existing region names.

### `check_coordinate_profile(profile_path, title_contains=None, hwnd=None)`

Verifies whether a live window still matches a coordinate profile:
uses the `observe` permission; the live process path must match the profile path, and the
active policy rect must match the profile `reference_region_name` rect, exactly.
It passes through the **same named-region, mask, pixel/PNG-byte/
rate/memory and pre/post-capture focus-geometry TOCTOU chain** as `screenshot_window`.
The live window size and the masked-region hash are compared against the profile;
on `matches: false`, the `reason` field reports size versus hash
mismatches separately. The screenshot itself is neither returned nor
stored.

### `record_coordinate_profile(profile_id, reference_region_name, safe_regions, verification_points, title_contains | hwnd, app_version=None)`

Records a coordinate profile from the live window (uses the `observe`
permission; foreground is not required): captures ONLY the reference region
through the same pipeline `check_coordinate_profile` verifies with, hashes
those PNG bytes as `reference_image_hash`, and samples each `[x, y]`
verification point's color from the same image. `reference_region_name` must
name exactly one entry of `safe_regions` (each `{name, rect[, description]}`,
window-relative); points must lie inside the reference rect. Returns
`{"profile": <the eight schema fields>, "correlation_id": ...}` — the nested
profile passes `coordinate_profile.validate_profile`, so
`json.dump(result["profile"])` is directly usable by
`check_coordinate_profile`/`resolve_coordinate_profile_point`. Writes NO
file. Keep policy `screenshot_masks` disjoint from the reference region,
otherwise later masked verifications hash different pixels than recorded.

### `list_windows()`

Lists only allowed, visible, titled top-level windows:
`title`, `pid`, `hwnd`, `rect` (`[left, top, right, bottom]`, in screen
coordinates), `process_path` (normalized full executable
path), and `window_class` (Win32 window class name, e.g. `"Notepad"`).
Call this first to pick the `title_contains` value you will pass to the other tools.
Each entry also carries a `foreground` boolean (one `GetForegroundWindow`
read per call, not per window), so no follow-up `get_window_state` round
trip is needed to find the front window.

### `get_window_state(hwnd)`

Reads the live state of a known hwnd **without requiring focus**: in addition to the
fields above, `visible`, `iconic` (whether minimized to an icon),
`foreground`, `on_top`, and a `correlation_id`. Unlike `list_windows`/other
tools, it does not treat a minimized window as "gone" — it exists to give a diagnostic answer to
"why can this hwnd not be acted on right now?" (minimized/background/out-of-policy/
really-gone).

### `restore_window(hwnd)`

Restores a minimized (iconic) allowed window to its previous size and
position (`observe` permission, hwnd-only — minimized windows are invisible
to title search by design, so take the hwnd from `get_window_state`). Clears
iconic state via `ShowWindow(SW_RESTORE)` and re-verifies identity (same pid)
plus the cleared state, then stops: it never touches foreground/focus, so a
restored-but-background window is still rejected by the input tools' own
passive gates. Out-of-policy or gone hwnds are denied before any Win32
effect; an already-visible window is a success no-op. The outcome is audited.

### `wait_for_window(title_contains, timeout_ms=5000, poll_interval_ms=200)`

Waits until an allowed window appears in the title (mandatory, at most 30 s),
returning a dict in the same format as `list_windows()` when it appears. If multiple
allowed windows match (an ambiguity that waiting cannot resolve),
it errors immediately.

### `wait_for_title_change(hwnd, timeout_ms=5000, poll_interval_ms=200)`

Waits until the target's title changes (mandatory, at most 30 s). If the target stops being
visible/allowed during the wait (closed, minimized,
moved out of policy), it errors immediately instead of waiting forever.

### `request_confirmation(action_class, title_contains | hwnd, ttl_seconds=60)`

Requests a single-use approval token with a lifetime of at most 300 seconds for a protected
action (`text`, `close`, `drag`, plus any extra class the active policy file lists under
`protected_actions` — e.g. `click`; the file can only add protection, never lift the
built-in set). The target is verified EXACTLY before the token is
issued (policy + focus + action-time
identity) — no token can be OBTAINED for an unauthorized or non-visible target.
Pass the returned `token_id` to the relevant tool as `confirmation_token`; the token is valid only for
THIS target (hwnd/pid/process-start-time) and THIS
action class, and cannot be reused after it is consumed. `click_window`/`double_click_window`/
`right_click_window`/`hover_window`/`scroll_window`/`send_key` accept an optional
`confirmation_token` that is required exactly when the file protects that class
(an `observe` entry never gates reads).

### `preview_action(action_class, title_contains | hwnd)`

Summarizes, without producing any side effects, the current status of an action on its target:
whether approval is required (`requires_confirmation`), whether a valid
token already exists. Requires no focus/foreground.

### Target selection: title_contains or hwnd

Each action tool takes exactly one of the two target forms. Prefer the hwnd from the list_windows output for repeatable
operations; visibility and permission policy are rechecked on every call.

### `screenshot_window(title_contains | hwnd, region_name=None, crop_left=12, crop_top=40, crop_right=12, crop_bottom=12, grid=False, grid_spacing=50)`

Finds and **verifies** the target window, returning only that window's screenshot (with edge margins
cropped) as PNG (`ImageContent`). Unlike every input-emitting tool, this tool does **not** require the
target to be foreground — reading pixels does not steal keyboard focus or change window order. The
target must still be unobscured (not covered by another window); the operator does not need to have
brought it forward, and the tool never changes focus regardless of `DESKTOP_AUTOMATION_FOCUS_MODE`.

- If the window is covered by another window, the window rect changes before/after capture,
  **or** another window steps in at the moment the image is taken: error without returning the
  captured pixels.
- If multiple windows match `title_contains`, error (it is ambiguous which
  window is meant) — pass a more specific title fragment
  (from the `list_windows()` output).
- In a policy file, `region_name` must name a defined `safe_regions` entry;
  intersecting `screenshot_masks` are painted black.
- `crop_*` is used only in the explicit unrestricted-env compatibility path.
- Negative/boolean crops, areas above 16 MP, policy PNG byte limits, rate
  limits, or concurrent memory-budget overruns are rejected.
- `grid=True` draws a light, semi-transparent coordinate overlay (vertical/horizontal lines every
  `grid_spacing` pixels, 5–2000, with axis labels) so a `click_window`/`drag_window` target's `(x, y)`
  can be read directly off the image instead of visually estimated. Purely a rendering aid applied
  **after** policy masks — it never changes which pixels are captured, adds no new action permission
  (stays inside `observe`), and cannot reveal anything a mask already blacked out. Off by default.

### `click_window(title_contains | hwnd, x, y)`

Finds and **verifies** the window, sending mouse movement + a short wait + left-click to the
`(x, y)` window-relative
position.

`(x, y)` is in the raw coordinate system of `screenshot_window` **before cropping**
— read `window_offset: [x, y]` from the screenshot response `_meta` (alongside
`correlation_id` and `image_size`) and add it to the `(x, y)` you see in the
cropped image. With default crops the offset is `[12, 40]`; with zeroed crops
or a named region it is `[0, 0]` / the region origin respectively.

Internal ordering (some render surfaces do not register a bare `SetCursorPos` as a click;
a real "move" event + settle wait is needed first): move → wait 150ms → last-moment target verification → left-button-down
→ wait 50ms → left-button-up.

### `double_click_window(title_contains | hwnd, x, y)`

Same ordering as `click_window`, followed after a short gap (50ms) by
a second left-button-down/up cycle. Uses the `click` permission.

### `right_click_window(title_contains | hwnd, x, y)`

Same ordering as `click_window` but with the right mouse button (e.g. to open a context
menu). Uses the `click` permission; to dismiss an opened context menu,
use `send_key(key="esc")`.

### `hover_window(title_contains | hwnd, x, y, dwell_ms=400)`

Moves the mouse to `(x, y)` inside the verified window without clicking, holding it
there for `dwell_ms` (0–10000) — for hover-triggered UI elements
(tooltips, dropdowns). Uses the `hover` permission.

### `scroll_window(title_contains | hwnd, x, y, notches=1)`

Sends a mouse-wheel event at the position inside the verified window.
One `notches` unit is one physical wheel detent (`notches * 120` as the
Win32 wheel delta — 120 is the OS detent constant, not a speed knob):
positive scrolls up, negative scrolls down; zero is rejected, magnitude
limited to ±20. Vertical wheel only — no horizontal axis (deliberate, see
the tool docstring). Uses the `click` permission.

### `drag_window(title_contains | hwnd, start_x, start_y, end_x, end_y, confirmation_token, path=None)`

Sends a drag from start to end inside the verified window (move
→ left-button-down → move to end point → left-button-up). Requires a permission SEPARATE
from `click` (`drag`) because it can permanently
change the target's content (reordering, drag-and-drop). The start AND end
points each independently pass the window-bounds check;
on the error path the mouse button is always released via `finally`. **Protected
action**: requires a `confirmation_token` first obtained with
`request_confirmation(action_class="drag", ...)` (see the section above).

Optional `path` is a list of intermediate window-relative `[x, y]` waypoints
traversed in order between start and end with the button held throughout
(for routes around an obstacle a straight line cannot model; at most 16).
Every waypoint passes the same independent bounds check, all points resolve
before anything is emitted, the target is re-verified before each leg, and
the button is still released via `finally` on mid-path failure. Omitted (or
empty) means the legacy straight drag.

### `send_text(title_contains | hwnd, text, confirmation_token)`

Plain text input, by default going character by character to the actually focused control as UTF-16
`SendInput` events. This path preserves Turkish and astral
Unicode characters; it does not move the mouse. It is not for shortcut keys;
those must be sent with `send_key`. **Protected action**: requires
a `confirmation_token` first obtained with `request_confirmation(action_class="text", ...)`.

A successful return states that Win32 input events were emitted; it does not prove that a particular
editor accepted the text and displayed it on screen. This distinction matters especially for Electron,
canvas, and owned/child controls. Unless there is a proven target-specific
observation contract, the caller must verify the result with a separate, privacy-preserving
UI observation.

For compatibility with legacy controls,
`DESKTOP_AUTOMATION_TEXT_MODE=wm_char` can be set explicitly. This mode sends `WM_CHAR` to the top-level
window and may be unreliable in some shell controls such as Explorer;
it is not the default.

### `send_key(title_contains | hwnd, key, modifiers=None, hold_ms=80)`

A **real, OS-level keyboard event** via `SendInput` (keydown/keyup) —
use this for game controls (arrow keys, Space, Enter, Esc) and Ctrl/Shift/Alt
combinations. Unlike `send_text`, it is the right event shape for games/Flash
content (anything listening for `KeyboardEvent`). However,
the tool does not verify that the target application accepted the event and changed behavior;
it only reports that the events were sent into the operating-system input stream.

- `key`: `up`/`down`/`left`/`right`/`space`/`enter`/`esc`/`tab`/
  `backspace`/`delete`/`home`/`end`/`pageup`/`pagedown`, a single letter
  (`a`-`z`), a single digit (`0`-`9`), or `f1`-`f12`.
- `modifiers`: e.g. `["ctrl"]`, `["ctrl","shift"]` — pressed before `key`,
  released in **reverse order** after `key` is released.
- `hold_ms`: how long the key is held down (default 80ms).

### `send_key_sequence(keys, title_contains | hwnd, hold_ms=80, confirmation_token="")`

Several `send_key` steps in one guarded call — e.g. `ctrl+a` then `delete`
without a round-trip per key. Each step is `{"key": <name>,
"modifiers": [...]}` (same key names and at-most-two-modifiers rule as
`send_key`); `hold_ms` applies to every step. At most 10 steps per call
(longer macros belong in separate, separately audited calls). Same
`key`-class permission and `confirmation_token` rules as `send_key` (one
token covers the whole sequence). All key names resolve before anything is
emitted; each step re-verifies the target; a mid-sequence failure releases
every pressed key — none is ever left held. Like `send_key`, reports only
that events entered the OS input stream.

### `close_window(title_contains | hwnd, confirmation_token)`

**Protected action**: requires a `confirmation_token` first obtained with
`request_confirmation(action_class="close", ...)`. Sends a `WM_CLOSE` message to the window
(normal window close — forced
process termination **is not**).

Every effect-producing tool (`click_window`…`close_window`) and `get_window_state`/
`wait_for_window`/`wait_for_title_change` add an opaque `correlation_id` to their result
— it carries no identity or content. In effective tools, the same value matches the related
audit event one-to-one; `screenshot_window` carries it in the content `_meta`
field.

## Typical usage flow

```python
# 1. Find the permitted target window
wins = list_windows()
# 2. Pin the handle and take a focus-verified screenshot
target = wins[0]["hwnd"]
img = screenshot_window(hwnd=target, region_name="stage-800x600")
# 3. Click the position inside the named region via the window-relative profile point
point = resolve_coordinate_profile_point(profile_path, "sahne-merkez")
click_window(hwnd=target, x=point["x"], y=point["y"])
# 4. Send game input (real key, not WM_CHAR)
send_key(hwnd=target, key="right", hold_ms=200)
# 5. View the result again
img2 = screenshot_window(hwnd=target, region_name="stage-800x600")
```

## Focus verification

In the default `passive` mode, tools require the operator to have already brought the target forward;
otherwise they deny without sending any image or input. If
`DESKTOP_AUTOMATION_FOCUS_MODE=activate` is explicitly set, the tool tries to bring the target forward for about two
seconds; the first successful attempt waits only 50ms to settle.

## Known limitations

- Works only on the same physical machine, for visible (not minimized to an icon)
  windows.
- Runs only on Windows (direct Win32 API via `ctypes`).
- Keyboard shortcuts are possible via `send_key`'s `modifiers` parameter;
  multi-step chains (e.g. `ctrl+a` then `delete`, up to 10 steps) belong in
  `send_key_sequence`, which is tested. Hold-while-mouse combinations are
  not supported.
- If multiple windows share the same title, `title_contains` is insufficient
  — pick an exact target with the `hwnd` from the `list_windows()` output.
- No OCR/in-image text search — there is no higher-level tool asking to "find this text",
  only raw pixels/coordinates.

## Next

Current priorities are release automation, broader supported-environment
validation, clearer policy-authoring guidance, and contributions that preserve
the project's security model. See [CONTRIBUTING.md](CONTRIBUTING.md) before
opening a pull request.
