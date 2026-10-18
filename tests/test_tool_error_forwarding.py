"""Which tool failures reach the MCP client, and with what text.

The MCP SDK forwards only `ToolError` text to the client and reports any other
exception as a bare "Error executing tool <name>". The server re-raises its own
error categories (`errors.CATEGORY_ERRORS`) as `ToolError`, so their
client-oriented messages arrive; every other exception stays generic.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError

from desktop_automation_mcp import __version__, confirmation, server, target
from desktop_automation_mcp.errors import (
    CATEGORY_ERRORS,
    ActionTimeoutError,
    InputRejectedError,
    MemoryBudgetExceededError,
    PlatformError,
    PolicyDeniedError,
    TargetNotForegroundError,
    TargetOccludedError,
    TargetStaleError,
)

_SRC = Path(__file__).resolve().parents[1] / "src" / "desktop_automation_mcp"


def _call(name: str, arguments: dict | None = None):
    return asyncio.run(server.mcp.call_tool(name, arguments or {}))


class CategoryErrorsReachTheClientTests(unittest.TestCase):
    def test_every_category_is_forwarded(self):
        expected = {
            PolicyDeniedError,
            TargetStaleError,
            TargetOccludedError,
            TargetNotForegroundError,
            InputRejectedError,
            ActionTimeoutError,
            PlatformError,
        }
        self.assertEqual(set(CATEGORY_ERRORS), expected)

    def test_category_message_reaches_the_client_for_each_category(self):
        for category in (*CATEGORY_ERRORS, MemoryBudgetExceededError):
            with self.subTest(category=category.__name__):
                message = f"{category.__name__} detail for the client"
                with patch.object(
                    server, "_require_action", side_effect=category(message)
                ):
                    with self.assertRaises(ToolError) as ctx:
                        _call("list_windows")
                self.assertNotIsInstance(ctx.exception, UnexpectedToolError)
                self.assertIn(message, str(ctx.exception))

    def test_unexpected_exception_text_stays_on_the_server(self):
        secret = r"C:\Users\someone\private\policy.json"
        with patch.object(server, "_require_action", side_effect=RuntimeError(secret)):
            with self.assertRaises(UnexpectedToolError) as ctx:
                _call("list_windows")
        self.assertEqual(str(ctx.exception), "Error executing tool list_windows")

    def test_a_builtin_error_outside_the_categories_is_not_forwarded(self):
        with patch.object(
            server, "_require_action", side_effect=PermissionError("raw detail")
        ):
            with self.assertRaises(UnexpectedToolError) as ctx:
                _call("list_windows")
        self.assertNotIn("raw detail", str(ctx.exception))

    def test_in_process_callers_keep_the_original_exception_type(self):
        with patch.object(
            server, "_require_action", side_effect=PolicyDeniedError("denied")
        ):
            with self.assertRaises(PolicyDeniedError):
                server.list_windows()


class ToolRegistrationTests(unittest.TestCase):
    def test_registered_schema_keeps_the_function_signature(self):
        tools = {tool.name: tool for tool in asyncio.run(server.mcp.list_tools())}
        self.assertEqual(len(tools), 26)
        for name in ("click_window", "send_text", "screenshot_window"):
            with self.subTest(tool=name):
                schema = tools[name].model_dump(by_alias=True)["inputSchema"]
                parameters = inspect.signature(getattr(server, name)).parameters
                self.assertEqual(set(schema["properties"]), set(parameters))

    def test_server_reports_the_package_version(self):
        self.assertEqual(server.mcp.version, __version__)


class ProfileLoadingForToolsTests(unittest.TestCase):
    def test_missing_profile_file_is_an_input_error_naming_the_given_path(self):
        with self.assertRaises(InputRejectedError) as ctx:
            server._load_profile_for_tool("no-such-profile.json")
        self.assertIn("no-such-profile.json", str(ctx.exception))

    def test_resolve_tool_reports_a_missing_profile_as_input_error(self):
        with self.assertRaises(InputRejectedError):
            server.resolve_coordinate_profile_point("no-such-profile.json", "x")

    def test_every_tool_loads_profiles_through_the_tool_boundary(self):
        tree = ast.parse((_SRC / "server.py").read_text(encoding="utf-8"))
        callers = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
            and any(
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Name)
                and call.func.id == "_load_profile_file"
                for call in ast.walk(node)
            )
        ]
        self.assertEqual([node.name for node in callers], ["_load_profile_for_tool"])

    def test_invalid_profile_content_stays_a_policy_denial(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "profile.json"
            path.write_text("{}", encoding="utf-8")
            with self.assertRaises(PolicyDeniedError):
                server._load_profile_for_tool(str(path))


class ConvertedRaiseSitesTests(unittest.TestCase):
    """Client-reachable checks raise a category that subclasses the SAME builtin."""

    def test_confirmation_input_checks_are_input_errors(self):
        with self.assertRaises(InputRejectedError):
            confirmation._check_ttl(0)
        with self.assertRaises(InputRejectedError):
            confirmation._check_identity("not-a-dict")

    def test_gone_hwnd_is_an_input_error(self):
        with patch.object(target, "_user32") as user32:
            user32.return_value.IsWindow.return_value = 0
            with self.assertRaises(InputRejectedError):
                target._window_diagnostic_state(42)

    def test_modules_reachable_from_tools_raise_no_bare_builtin(self):
        # (module, builtin) -> allowed count. read_profile_document keeps its
        # OSError contract for an unreadable file; server._load_profile_for_tool
        # turns that into InputRejectedError at the tool boundary.
        allowed = {
            ("coordinate_profile.py", "FileNotFoundError"): 1,
            ("coordinate_profile.py", "OSError"): 2,
        }
        builtins = {"ValueError", "PermissionError", "OSError", "FileNotFoundError"}
        for module in ("confirmation.py", "target.py", "coordinate_profile.py"):
            tree = ast.parse((_SRC / module).read_text(encoding="utf-8"))
            counts: dict[str, int] = {}
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Raise)
                    and isinstance(node.exc, ast.Call)
                    and isinstance(node.exc.func, ast.Name)
                    and node.exc.func.id in builtins
                ):
                    counts[node.exc.func.id] = counts.get(node.exc.func.id, 0) + 1
            for name in builtins:
                with self.subTest(module=module, builtin=name):
                    self.assertEqual(
                        counts.get(name, 0), allowed.get((module, name), 0)
                    )


if __name__ == "__main__":
    unittest.main()
