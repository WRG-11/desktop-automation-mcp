# Compatibility

The supported baseline is Windows with Python 3.12 or later and the MCP 1.x
SDK range declared in `pyproject.toml`. The server uses Win32 APIs directly
and is not supported on macOS or Linux.

Release validation covers clean wheel installation, package import, the console
entry point, and a stdio MCP handshake. Consult the release notes for the
tested dependency versions of a specific release.
