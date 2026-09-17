"""Backward-compatibility entry point.

Existing MCP registrations may invoke this file. The application code
lives in the package under src; do not remove this wrapper during the
0.1 series.
"""
from __future__ import annotations

import sys
from pathlib import Path

_SOURCE_ROOT = Path(__file__).resolve().parent / "src"
if str(_SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SOURCE_ROOT))

from desktop_automation_mcp.server import main


if __name__ == "__main__":
    main()
