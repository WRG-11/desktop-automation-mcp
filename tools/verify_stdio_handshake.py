"""Verify that the installed console entry point completes MCP stdio init.

Run this from outside the checkout after installing the built wheel. The
server starts without any desktop-policy environment variables, so the probe
exercises only MCP initialization and grants no window authority.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
import sys

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


async def verify() -> None:
    """Start the installed server and require its protocol identity."""
    python_directory = Path(sys.executable).parent
    candidates = (
        python_directory / "desktop-automation-mcp.exe",
        python_directory / "Scripts" / "desktop-automation-mcp.exe",
    )
    entry_point = next((path for path in candidates if path.is_file()), None)
    if entry_point is None:
        locations = ", ".join(str(path) for path in candidates)
        raise RuntimeError(
            f"Installed console entry point not found; checked: {locations}"
        )
    parameters = StdioServerParameters(command=str(entry_point))
    async with stdio_client(parameters) as streams:
        async with ClientSession(*streams) as session:
            result = await asyncio.wait_for(session.initialize(), timeout=20)
            tools = await asyncio.wait_for(session.list_tools(), timeout=20)
    if result.serverInfo.name != "desktop-automation":
        raise RuntimeError(
            "MCP server identity mismatch: "
            f"expected 'desktop-automation', got {result.serverInfo.name!r}"
        )
    if "health_check" not in {tool.name for tool in tools.tools}:
        raise RuntimeError("MCP tool discovery did not include health_check")


def main() -> None:
    asyncio.run(verify())
    print("MCP stdio handshake OK: desktop-automation")


if __name__ == "__main__":
    main()
