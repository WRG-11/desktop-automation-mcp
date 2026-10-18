# desktop-automation-mcp

[![Quality](https://github.com/WRG-11/desktop-automation-mcp/actions/workflows/quality.yml/badge.svg)](https://github.com/WRG-11/desktop-automation-mcp/actions/workflows/quality.yml)
[![CodeQL](https://github.com/WRG-11/desktop-automation-mcp/actions/workflows/codeql.yml/badge.svg)](https://github.com/WRG-11/desktop-automation-mcp/actions/workflows/codeql.yml)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)
![Platform: Windows](https://img.shields.io/badge/platform-Windows-lightgrey.svg)

An MCP server that lets a client look at and operate the Windows application windows you
name in a policy file, and refuses everything else. Windows only, Python 3.12 or newer.

A policy names one application and the action classes it may receive. This one allows
typing into Notepad and closing it, and marks both as needing a confirmation token:

```yaml
application:
  executable_path: "C:\\Windows\\System32\\notepad.exe"
  title_patterns:
    - "*Notepad*"
  allowed_actions:
    - observe
    - text
    - close
  protected_actions:
    - text
    - close
```

An allowed action is still refused when the live target cannot be verified. The audit
event for one such refusal (fields from `schema/examples/audit_event-denied-text.json`):

```json
{
  "policy_id": "notepad-text-policy/v1",
  "action_type": "text",
  "outcome": "denied",
  "denial_reason": "target window is not in the foreground (passive focus gate); text was not sent because the operator did not bring the target to the front"
}
```

[Quick start](#quick-start) · [Tools](#tools) · [Known limitations](#known-limitations) · [Documentation](#documentation)

## Why this design

Many desktop-automation MCP servers maximize capability: they expose the whole
desktop and often feed the accessibility tree to the model as text. That makes
two things easy that should be hard: acting on a window nobody authorized,
and on-screen content being read as instructions.

This server takes the opposite position:

- **Nothing is allowed until you name it.** A window must match an allowed
  title pattern **and** an exact executable path, and each action class
  (`observe`, `hover`, `click`, `key`, `text`, `drag`, `close`) is a separate
  permission. A new tool never inherits an existing permission.
- **Every effect is re-checked at the moment it happens.** Window handle,
  process ID, process start time, visibility, foreground and occlusion are
  re-bound right before input is sent. A stale or ambiguous target is denied,
  never guessed.
- **Screen content is never handed over as text.** The client receives pixels
  from policy-named regions and window-relative coordinates, so there is no
  channel for on-screen text to be mistaken for instructions.
- **Content-changing and irreversible actions need a confirmation token.**
  `text`, `drag` and `close` require a single-use, short-lived token bound to the exact target and
  action class.
- **Failures are loud.** If focus, identity or geometry cannot be verified,
  the tool raises an error instead of acting on a best guess.

It also supports canvas applications without UI Automation (for example
Flash content in Ruffle) through versioned, hash-verified coordinate profiles.

## Requirements

- Windows (the server calls Win32 APIs directly)
- Python 3.12 or newer (CI covers 3.12, 3.13 and 3.14)
- Runtime dependencies: [`mcp`](https://pypi.org/project/mcp/) 2.x and
  [Pillow](https://pypi.org/project/pillow/)

## Quick start

1. **Install** from a checkout (the package is not published to PyPI):

   ```powershell
   git clone https://github.com/WRG-11/desktop-automation-mcp.git
   cd desktop-automation-mcp
   py -3.12 -m pip install .
   ```

2. **Write a policy** for one application with only the actions you need.
   This one allows looking at Notepad and nothing more:

   ```yaml
   application:
     executable_path: "C:\\Windows\\System32\\notepad.exe"
     title_patterns:
       - "*Notepad*"
     allowed_actions:
       - observe
   ```

   Validate it before use:

   ```powershell
   py -3.12 tools/validate_policy.py notepad-observe.yaml
   ```

3. **Register the server** with your MCP client:

   ```json
   {
     "mcpServers": {
       "desktop-automation": {
         "command": "desktop-automation-mcp",
         "env": {
           "DESKTOP_AUTOMATION_POLICY_FILE": "C:\\path\\to\\notepad-observe.yaml"
         }
       }
     }
   }
   ```

   Restart the client, then call `health_check()` and `list_windows()`. If the
   list is empty, `policy_visibility_summary()` tells you whether the title or
   the executable path is the part that does not match.

The [quickstart guide](docs/guides/quickstart.md) walks through the same steps
with environment-variable configuration, and the
[security guide](docs/guides/security-guide.md) explains how to keep a policy
narrow.

## Tools

| Family | Tools | Permission |
|---|---|---|
| Diagnostics | `health_check`, `get_rate_limit_state`, `get_audit_events`, `get_virtual_screen_bounds`, `policy_visibility_summary` | none, or `observe` |
| Discovery | `list_windows`, `get_window_state`, `restore_window`, `wait_for_window`, `wait_for_title_change` | `observe` |
| Image and profiles | `screenshot_window`, `resolve_coordinate_profile_point`, `check_coordinate_profile`, `record_coordinate_profile` | `observe`, policy-named regions |
| Confirmation | `request_confirmation`, `preview_action` | the confirmed action / `observe` |
| Pointer | `click_window`, `double_click_window`, `right_click_window`, `scroll_window`, `hover_window` | `click` / `hover` |
| Keyboard | `send_key`, `send_key_sequence` | `key` |
| Protected | `send_text`, `drag_window`, `close_window` | `text` / `drag` / `close` **plus** a confirmation token |

Parameters, return shapes and per-tool guarantees are in the
[tool reference](docs/reference/tools.md).

## Configuration

A policy can come from a version-controlled file
(`DESKTOP_AUTOMATION_POLICY_FILE`, validated against
[`schema/policy.schema.json`](schema/policy.schema.json)) or from environment
variables (`DESKTOP_AUTOMATION_ALLOWED_TITLES`,
`DESKTOP_AUTOMATION_ALLOWED_PROCESS_PATHS`,
`DESKTOP_AUTOMATION_ALLOWED_ACTIONS`). Setting both is rejected rather than
merged. Screenshots in file mode are limited to regions the policy names.

Focus mode defaults to `passive`: the server never brings a window to the
front, it only acts on the window you already focused. Rate limits, the
screenshot memory budget and input limits are all configurable. Everything is
listed in the [configuration reference](docs/reference/configuration.md).

## Usage patterns

### Short, targeted input, not bulk text

`send_text` is best suited to a command or a single form field: the Unicode
path sends UTF-16 units one at a time and revalidates the target for each, so
long input takes longer. For longer text, accept that per-unit cost or paste
it with the application's normal controls; `send_text` is a protected action
and still requires a separate confirmation ([tool reference](docs/reference/tools.md),
[confirmation workflow](docs/reference/confirmation.md#client-workflow)).

### Automate another window while you work elsewhere

The default `passive` focus mode will not bring a background target forward,
so input actions require you to focus it first. Set
`DESKTOP_AUTOMATION_FOCUS_MODE=activate` only when it is acceptable for the
tool to bring that window forward and take focus
([focus-mode reference](docs/reference/configuration.md#focus-mode)).

### Screenshots need a named policy region

A text-only action grant does not authorize screenshots: capture uses the
`observe` permission, and policy-file mode also requires a `region_name`
listed in `safe_regions`. Legacy environment mode keeps screenshots disabled
by default ([configuration reference](docs/reference/configuration.md#policy-file),
[tool reference](docs/reference/tools.md)).

### Minimized or covered windows do not receive input

Input tools act only on visible windows. They reject minimized targets, as well
as covered or otherwise obscured windows; this is an intentional safety
boundary, not a failure to automate. To restore a minimized window, call
[`restore_window(hwnd)`](docs/reference/tools.md#restore_windowhwnd) with
`observe` permission. Restoring it does not change foreground or focus. In the
default `passive` mode, bring the restored window to the foreground before
trying an input action ([focus-mode reference](docs/reference/configuration.md#focus-mode)).

## Known limitations

- Windows only, on the local machine, for visible (not minimized) top-level
  windows.
- Pointer tools move the shared system cursor; do not grant `click` or `hover`
  while you are using the machine yourself.
- No OCR or text search: the server returns pixels and coordinates, not text.
- Mouse-and-key chords (holding a key while dragging) are not supported.
- UAC prompts, the sign-in screen and the lock screen are outside the target
  model; the server relies on Windows desktop isolation and never bypasses it.
- The confirmation token proves that a token was requested for this target,
  not that a human approved it. The client must keep approval as a separate
  turn; see the [confirmation contract](docs/reference/confirmation.md).

## Documentation

- [Quickstart](docs/guides/quickstart.md) and
  [security guide](docs/guides/security-guide.md)
- [Configuration reference](docs/reference/configuration.md) and
  [tool reference](docs/reference/tools.md)
- [Security model](docs/explanation/security-model.md) and
  [architecture map](docs/design/architecture-map.md)
- [Stdio transport](docs/reference/transport.md),
  [compatibility](docs/reference/compatibility.md) and
  [release verification](docs/releases/verification.md)

## Development

```powershell
py -3.12 -m pip install -e ".[dev]"
py -3.12 -m ruff check src tests tools
py -3.12 -m ruff format --check src tests tools
py -3.12 -m unittest discover -s tests -v
```

The [quality workflow](.github/workflows/quality.yml) runs the same checks on
clean Windows runners for Python 3.12, 3.13 and 3.14, then builds the wheel and
completes an MCP stdio handshake against the installed package. It never drives
real windows; UI smoke tests are run separately under operator supervision.

Chaos scenarios (`tests/test_chaos.py`) run the real `target.py`,
`visibility.py` and `server.py` functions against shared fake Win32 state
(`tests/fake_platform.py`); `tests/test_server.py` keeps its own mock pattern.

## Contributing and security

Read [CONTRIBUTING.md](CONTRIBUTING.md) before opening a pull request. Report
vulnerabilities privately as described in [SECURITY.md](SECURITY.md), not in a
public issue. For questions, see [SUPPORT.md](SUPPORT.md).

## License

[Apache License 2.0](LICENSE). See [NOTICE](NOTICE).
