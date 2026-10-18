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

# The import has to follow the sys.path insertion above, so E402 is expected.
from desktop_automation_mcp.server import main  # noqa: E402


if __name__ == "__main__":
    main()
