# Stdio transport contract

`desktop-automation-mcp` is a local, single-process MCP server. Its only
supported transport is stdio; it does not expose HTTP, SSE, WebSocket, or a
network listener.

## Compatibility boundary

- **Platform:** Windows only, with Python 3.12 or later.
- **SDK:** the `mcp` dependency range in `pyproject.toml` (`>=2.3,<3`).
  The server does not claim compatibility with MCP 1.x, with a later major
  version, or with a protocol revision outside the installed SDK's supported
  range.
- **Tool errors:** a failure in one of the categories in `errors.py` reaches
  the client as an `isError` result carrying the category's message. Any
  other exception reaches it only as `Error executing tool <name>`; the detail
  stays in the server log.
- **Verified client path:** the MCP Python SDK stdio client used by the release
  gate initializes the installed console entry point and performs tool
  discovery. Other MCP clients are welcome to use stdio, but are not a
  compatibility certification.

## Launch and stream lifecycle

Install a released wheel, then register the console entry point with the MCP
client:

```text
desktop-automation-mcp
```

For source checkout development, use `py -3.12 server.py`; do not publish a
machine-specific checkout path as a universal installation command.

The client starts one process, completes MCP initialization, discovers tools,
and sends calls over standard input/output. Standard output is reserved for
the MCP protocol. Project diagnostics, including failed audit writes, are sent
to standard error so they cannot corrupt protocol frames.

To end a session, the client closes the stdio streams and waits for the child
process to exit. There is no session resume, network reconnect, or implicit
restart contract: start a new server process for a new session.

## Policy boundary

Transport setup does not grant desktop authority. With no target policy, every
window action is denied. Configure the narrow policy before starting the
server, then use `list_windows` to select a permitted target. Protected text,
drag, and close effects still require their own short-lived confirmation
tokens after transport initialization.
