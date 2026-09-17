# Quickstart

Desktop Automation MCP starts with no window or action authority. Install a
local checkout first:

```powershell
py -3.12 -m pip install -e .
```

Then configure one
known application, allow only the actions you need, and keep focus mode
passive.

```json
{
  "mcpServers": {
    "desktop-automation": {
      "command": "desktop-automation-mcp",
      "env": {
        "DESKTOP_AUTOMATION_ALLOWED_TITLES": "*Example App*",
        "DESKTOP_AUTOMATION_ALLOWED_PROCESS_PATHS": "C:\\Program Files\\Example App\\example.exe",
        "DESKTOP_AUTOMATION_ALLOWED_ACTIONS": "observe"
      }
    }
  }
}
```

Restart the MCP client after changing its configuration. Begin with
`list_windows()` and select a specific `hwnd`; do not broaden a title pattern
just to make an ambiguous match succeed. Add `click`, `key`, or other actions
only when the workflow requires them. `text`, `drag`, and `close` additionally
require a short-lived confirmation token bound to the resolved target.

For repeatable configuration, use a policy file validated against
[`schema/policy.schema.json`](../../schema/policy.schema.json). See the
[policy migration guide](migration-env-to-policy-file.md) for the format and
examples.

## Read-only OpenCode connection check

To dogfood a local checkout without granting desktop authority, add a
project-local MCP registration with no policy environment variables:

```powershell
opencode mcp add --env PY_PYTHON=3.12 desktop-automation-public py server.py
opencode mcp list
```

Run the commands from the repository root. A `connected` status proves that
OpenCode can start the server and complete MCP initialization; it does not
prove an action was permitted or performed. Keep the generated
`opencode.json` local. For a first agent-side check, ask it to call only
`health_check`; do not authorize screenshots, focus, or input merely to test
the connection.
