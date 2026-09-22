"""A real, action-free JSON-RPC stdio handshake against the source entry point."""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path

import mcp.types as mcp_types


_ROOT = Path(__file__).resolve().parents[1]
_TIMEOUT_SECONDS = 10


def _safe_server_environment() -> dict[str, str]:
    """Do not inherit a developer's live desktop policy into this probe."""
    return {
        name: value
        for name, value in os.environ.items()
        if not name.startswith("DESKTOP_AUTOMATION_")
    }


class StdioProtocolTests(unittest.TestCase):
    def test_source_entrypoint_serves_raw_jsonrpc_without_desktop_policy(self):
        process = subprocess.Popen(
            [sys.executable, "-u", "server.py"],
            cwd=_ROOT,
            env=_safe_server_environment(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        responses: queue.Queue[str] = queue.Queue()
        assert process.stdout is not None

        def collect_responses() -> None:
            for line in process.stdout:
                responses.put(line)

        reader = threading.Thread(target=collect_responses)
        reader.start()

        def request(
            request_id: int, method: str, params: dict[str, object]
        ) -> dict[str, object]:
            assert process.stdin is not None
            process.stdin.write(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "method": method,
                        "params": params,
                    }
                )
                + "\n"
            )
            process.stdin.flush()
            deadline = time.monotonic() + _TIMEOUT_SECONDS
            while (remaining := deadline - time.monotonic()) > 0:
                try:
                    line = responses.get(timeout=remaining)
                except queue.Empty as exc:
                    raise AssertionError(
                        "stdio server did not answer JSON-RPC request"
                    ) from exc
                response = json.loads(line)
                if response.get("id") != request_id:
                    continue
                self.assertNotIn("error", response)
                return response["result"]
            self.fail("stdio server did not answer JSON-RPC request")

        try:
            initialized = request(
                1,
                "initialize",
                {
                    "protocolVersion": mcp_types.LATEST_PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "source-stdio-regression", "version": "1"},
                },
            )
            self.assertEqual(initialized["serverInfo"]["name"], "desktop-automation")

            assert process.stdin is not None
            process.stdin.write(
                json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"})
                + "\n"
            )
            process.stdin.flush()

            tools = request(2, "tools/list", {})
            self.assertIn("health_check", {tool["name"] for tool in tools["tools"]})

            health = request(3, "tools/call", {"name": "health_check", "arguments": {}})
            self.assertFalse(health.get("isError", False))
        finally:
            if process.stdin is not None:
                process.stdin.close()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.terminate()
                process.wait(timeout=2)
            reader.join(timeout=2)
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()


if __name__ == "__main__":
    unittest.main()
