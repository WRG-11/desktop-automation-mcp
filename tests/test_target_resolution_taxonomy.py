"""Target-resolution error taxonomy (architecture audit round 1).

Scope: every other caller-input validation in the codebase raises
`InputRejectedError` (stable `input_rejected` code), but
`target._resolve_window`'s five caller-input rejections raise BARE
`ValueError` — denied without a machine-readable code. The fix keeps
messages byte-identical (notably the no-match text, which
`wait_for_window` matches on via `_NOT_FOUND_MESSAGE`) and relies on
`InputRejectedError ⊂ ValueError` so every existing `except ValueError`
path behaves exactly as before.

File layout note: separate file so the red tests land without touching
existing suites.
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from desktop_automation_mcp import errors
from desktop_automation_mcp.target import TargetSnapshot, _resolve_window


def _snap(hwnd=42, title="Ruffle player"):
    return TargetSnapshot(
        hwnd=hwnd,
        title=title,
        pid=1,
        rect=(0, 0, 100, 100),
        process_started_at=1,
        process_path=r"C:\Program Files\ruffle\bin\ruffle.exe",
        window_class="RuffleWindowClass",
    )


class ResolveWindowTaxonomyTests(unittest.TestCase):
    def test_both_target_forms_rejected_with_stable_code(self):
        with self.assertRaises(errors.InputRejectedError) as ctx:
            _resolve_window("Ruffle", 42)
        self.assertEqual(ctx.exception.code, errors.INPUT_REJECTED)
        self.assertEqual(
            str(ctx.exception), "Give exactly one target: title_contains or hwnd."
        )

    def test_neither_target_form_rejected_with_stable_code(self):
        with self.assertRaises(errors.InputRejectedError) as ctx:
            _resolve_window(None, None)
        self.assertEqual(ctx.exception.code, errors.INPUT_REJECTED)
        self.assertEqual(
            str(ctx.exception), "Give exactly one target: title_contains or hwnd."
        )

    def test_empty_title_rejected_with_stable_code(self):
        with patch("desktop_automation_mcp.target._enum_windows", return_value=[]):
            with self.assertRaises(errors.InputRejectedError) as ctx:
                _resolve_window("   ", None)
        self.assertEqual(ctx.exception.code, errors.INPUT_REJECTED)
        self.assertEqual(str(ctx.exception), "title_contains must not be empty.")

    def test_no_match_rejected_with_byte_identical_message(self):
        # Guards the `_NOT_FOUND_MESSAGE` coupling: `wait_for_window`
        # retries ONLY on this exact text.
        with patch("desktop_automation_mcp.target._enum_windows", return_value=[]):
            with self.assertRaises(errors.InputRejectedError) as ctx:
                _resolve_window("Nope", None)
        self.assertEqual(ctx.exception.code, errors.INPUT_REJECTED)
        self.assertEqual(
            str(ctx.exception), "No target found among the allowed visible windows."
        )

    def test_ambiguity_rejected_with_stable_code(self):
        windows = [_snap(42, "Ruffle one"), _snap(43, "Ruffle two")]
        with patch("desktop_automation_mcp.target._enum_windows", return_value=windows):
            with self.assertRaises(errors.InputRejectedError) as ctx:
                _resolve_window("Ruffle", None)
        self.assertEqual(ctx.exception.code, errors.INPUT_REJECTED)
        self.assertEqual(
            str(ctx.exception),
            "Multiple allowed windows matched; use the hwnd from list_windows() output.",
        )


if __name__ == "__main__":
    unittest.main()
