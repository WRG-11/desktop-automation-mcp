"""Policy-file integration tests (Phase 3 -> production).

Scope: `policy.py`'s readers in `DESKTOP_AUTOMATION_POLICY_FILE` mode,
file+env conflict denial, fail-closed error classes and no-caching.
Does not touch `tests/test_server.py` or `tests/test_policy_schema.py`;
isolates the environment in every test (there is NO cache, order-independent).
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from desktop_automation_mcp import errors, policy, policy_file

REPO_ROOT = Path(__file__).resolve().parents[1]

POLICY_VARS = [
    "DESKTOP_AUTOMATION_POLICY_FILE",
    "DESKTOP_AUTOMATION_ALLOWED_TITLES",
    "DESKTOP_AUTOMATION_ALLOWED_PROCESS_PATHS",
    "DESKTOP_AUTOMATION_ALLOWED_ACTIONS",
    "DESKTOP_AUTOMATION_FOCUS_MODE",
    "DESKTOP_AUTOMATION_TEXT_MODE",
    "DESKTOP_AUTOMATION_ALLOW_UNRESTRICTED_SCREENSHOTS",
]

RUFFLE_EXE = r"C:\Program Files\ruffle\bin\ruffle.exe"
NOTEPAD_EXE = r"C:\Windows\System32\notepad.exe"


def _env_patch(**overrides):
    """Isolates the policy variables: removes all of them then sets only the given ones."""
    env = dict(os.environ)
    for var in POLICY_VARS:
        env.pop(var, None)
    env.update(overrides)
    return mock.patch.dict(os.environ, env, clear=True)


def _policy_yaml(exe, titles, actions, protected=None, expires=None):
    # In double-quoted YAML the Windows path is written with `\\` (as in
    # the repo's examples); otherwise escapes like `\r` resolve to their
    # real YAML meaning (carriage return).
    quoted_exe = exe.replace("\\", "\\\\")
    lines = [
        "application:",
        f'  executable_path: "{quoted_exe}"',
        "  title_patterns:",
    ]
    lines.extend(f"    - {t}" for t in titles)
    lines.append("  allowed_actions:")
    lines.extend(f"    - {a}" for a in actions)
    if protected is not None:
        lines.append("  protected_actions:")
        lines.extend(f"    - {a}" for a in protected)
    if expires is not None:
        lines.append(f"  expires_at: {expires}")
    return "\n".join(lines) + "\n"


class PolicyFileIntegrationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def _write(self, name, text):
        path = Path(self._tmp.name) / name
        path.write_text(text, encoding="utf-8")
        return str(path)

    def test_file_mode_returns_expected_values(self):
        path = self._write(
            "ruffle.yaml",
            _policy_yaml(RUFFLE_EXE, ["*Ruffle*"], ["observe", "hover"]),
        )
        with _env_patch(DESKTOP_AUTOMATION_POLICY_FILE=path):
            self.assertEqual(policy._allowed_title_patterns(), ["*Ruffle*"])
            self.assertEqual(
                policy._allowed_process_paths(),
                {policy._normalise_process_path(RUFFLE_EXE)},
            )
            self.assertEqual(policy._allowed_actions(), {"observe", "hover"})

    def test_expired_file_raises_permission_error(self):
        path = self._write(
            "expired.yaml",
            _policy_yaml(
                RUFFLE_EXE,
                ["*Ruffle*"],
                ["observe"],
                expires="2020-01-01T00:00:00+03:00",
            ),
        )
        with _env_patch(DESKTOP_AUTOMATION_POLICY_FILE=path):
            with self.assertRaises(PermissionError) as ctx:
                policy._allowed_title_patterns()
        self.assertIn("expired", str(ctx.exception))

    def test_schema_invalid_file_raises_permission_error_not_value_error(self):
        # An unknown action in the file is a denial, PermissionError not ValueError.
        path = self._write(
            "teleport.yaml",
            _policy_yaml(RUFFLE_EXE, ["*Ruffle*"], ["observe", "teleport"]),
        )
        with _env_patch(DESKTOP_AUTOMATION_POLICY_FILE=path):
            with self.assertRaises(PermissionError) as ctx:
                policy._allowed_actions()
        self.assertNotIsInstance(ctx.exception, ValueError)
        self.assertIn("unknown action", str(ctx.exception))

    def test_runtime_reads_screenshot_constraints_from_canonical_yaml(self):
        path = str(REPO_ROOT / "schema" / "examples" / "ruffle-policy.yaml")
        with _env_patch(DESKTOP_AUTOMATION_POLICY_FILE=path):
            constraints = policy._screenshot_constraints()
        self.assertEqual(
            constraints["safe_regions"],
            [{"name": "stage-800x600", "rect": [12, 40, 788, 588]}],
        )
        self.assertEqual(
            constraints["screenshot_masks"], [{"rect": [740, 40, 788, 88]}]
        )
        self.assertEqual(constraints["max_screenshot_bytes"], 4_194_304)

    def test_env_screenshot_is_denied_by_default_and_needs_explicit_opt_in(self):
        with _env_patch():
            self.assertEqual(policy._screenshot_constraints()["safe_regions"], [])
        with _env_patch(DESKTOP_AUTOMATION_ALLOW_UNRESTRICTED_SCREENSHOTS="true"):
            self.assertIsNone(policy._screenshot_constraints()["safe_regions"])

        path = self._write(
            "observe-without-regions.yaml",
            _policy_yaml(RUFFLE_EXE, ["*Ruffle*"], ["observe"]),
        )
        with _env_patch(DESKTOP_AUTOMATION_POLICY_FILE=path):
            self.assertEqual(policy._screenshot_constraints()["safe_regions"], [])

    def test_invalid_env_screenshot_opt_in_fails_closed(self):
        with _env_patch(DESKTOP_AUTOMATION_ALLOW_UNRESTRICTED_SCREENSHOTS="perhaps"):
            with self.assertRaises(errors.PolicyDeniedError):
                policy._screenshot_constraints()

    def test_invalid_policy_file_carries_stable_policy_denied_code(self):
        path = self._write(
            "invalid-action.yaml",
            _policy_yaml(RUFFLE_EXE, ["*Ruffle*"], ["teleport"]),
        )
        with _env_patch(DESKTOP_AUTOMATION_POLICY_FILE=path):
            with self.assertRaises(errors.PolicyDeniedError) as ctx:
                policy._allowed_actions()
        self.assertEqual(ctx.exception.code, errors.POLICY_DENIED)

    def test_protected_subset_checked_on_load(self):
        path = self._write(
            "subset.yaml",
            _policy_yaml(
                NOTEPAD_EXE,
                ["*Notepad*"],
                ["observe"],
                protected=["close"],
            ),
        )
        with self.assertRaises(PermissionError) as ctx:
            policy_file.load_policy_file(path)
        self.assertIn("subset", str(ctx.exception))

    def test_file_plus_env_conflict_is_rejected_with_both_names(self):
        path = self._write(
            "ok.yaml", _policy_yaml(RUFFLE_EXE, ["*Ruffle*"], ["observe"])
        )
        cases = [
            (
                "titles",
                "DESKTOP_AUTOMATION_ALLOWED_TITLES",
                "*Ruffle*",
                policy._allowed_title_patterns,
            ),
            (
                "paths",
                "DESKTOP_AUTOMATION_ALLOWED_PROCESS_PATHS",
                RUFFLE_EXE,
                policy._allowed_process_paths,
            ),
            (
                "actions",
                "DESKTOP_AUTOMATION_ALLOWED_ACTIONS",
                "observe",
                policy._allowed_actions,
            ),
        ]
        for label, legacy_var, legacy_value, reader in cases:
            with self.subTest(label=label):
                env = {"DESKTOP_AUTOMATION_POLICY_FILE": path, legacy_var: legacy_value}
                with _env_patch(**env):
                    with self.assertRaises(PermissionError) as ctx:
                        reader()
                message = str(ctx.exception)
                self.assertIn("DESKTOP_AUTOMATION_POLICY_FILE", message)
                self.assertIn(legacy_var, message)

    def test_conflict_wins_over_neither_side(self):
        # File is valid, legacy variable too (no "winner"): still a denial.
        path = self._write(
            "ok.yaml", _policy_yaml(RUFFLE_EXE, ["*Ruffle*"], ["observe"])
        )
        with _env_patch(
            DESKTOP_AUTOMATION_POLICY_FILE=path,
            DESKTOP_AUTOMATION_ALLOWED_TITLES="*Baska*",
        ):
            with self.assertRaises(PermissionError):
                policy._allowed_title_patterns()

    def test_env_only_behavior_is_unchanged(self):
        # Existing-behavior regression: multi-path env (a form NOT
        # EXPRESSIBLE in the file) works exactly as before.
        with _env_patch(
            DESKTOP_AUTOMATION_ALLOWED_TITLES="*Ruffle*,*Notepad*",
            DESKTOP_AUTOMATION_ALLOWED_PROCESS_PATHS=(f"{RUFFLE_EXE};{NOTEPAD_EXE}"),
            DESKTOP_AUTOMATION_ALLOWED_ACTIONS="observe,hover",
        ):
            self.assertEqual(
                policy._allowed_title_patterns(), ["*Ruffle*", "*Notepad*"]
            )
            self.assertEqual(
                policy._allowed_process_paths(),
                {
                    policy._normalise_process_path(RUFFLE_EXE),
                    policy._normalise_process_path(NOTEPAD_EXE),
                },
            )
            self.assertEqual(policy._allowed_actions(), {"observe", "hover"})

    def test_both_missing_keeps_old_messages(self):
        with _env_patch():
            with self.assertRaises(PermissionError) as ctx:
                policy._allowed_title_patterns()
            self.assertIn("DESKTOP_AUTOMATION_ALLOWED_TITLES", str(ctx.exception))
            with self.assertRaises(PermissionError) as ctx:
                policy._allowed_process_paths()
            self.assertIn(
                "DESKTOP_AUTOMATION_ALLOWED_PROCESS_PATHS", str(ctx.exception)
            )

    def test_missing_file_raises_oserror_not_permission_error(self):
        missing = str(Path(self._tmp.name) / "yok.yaml")
        with _env_patch(DESKTOP_AUTOMATION_POLICY_FILE=missing):
            with self.assertRaises(OSError) as ctx:
                policy._allowed_title_patterns()
        self.assertNotIsInstance(ctx.exception, PermissionError)

    def test_directory_path_raises_oserror_not_permission_error(self):
        with _env_patch(DESKTOP_AUTOMATION_POLICY_FILE=self._tmp.name):
            with self.assertRaises(OSError) as ctx:
                policy._allowed_actions()
        self.assertNotIsInstance(ctx.exception, PermissionError)

    def test_duplicate_json_key_is_rejected_instead_of_last_wins(self):
        path = self._write(
            "duplicate.json",
            '{"application":{"executable_path":"a","executable_path":"b"}}',
        )
        with self.assertRaisesRegex(policy_file.PolicyParseError, "duplicate"):
            policy_file.read_policy_document(path)

    def test_escaped_double_quote_does_not_end_yaml_comment_context(self):
        path = self._write(
            "escaped-comment.yaml",
            "application:\n"
            '  executable_path: "C:\\\\x.exe"\n'
            '  title_patterns: ["a\\"#b"] # a real comment\n'
            "  allowed_actions: [observe]\n",
        )
        doc = policy_file.read_policy_document(path)
        self.assertEqual(doc["application"]["title_patterns"], ['a"#b'])

    def test_policy_file_larger_than_one_mib_is_rejected_before_parse(self):
        path = self._write(
            "oversized.yaml", "x" * (policy_file.MAX_POLICY_FILE_BYTES + 1)
        )
        with self.assertRaisesRegex(policy_file.PolicyParseError, "limit"):
            policy_file.read_policy_document(path)

    def test_file_is_reread_without_cache(self):
        path = self._write(
            "live.yaml", _policy_yaml(RUFFLE_EXE, ["*Ruffle*"], ["observe"])
        )
        with _env_patch(DESKTOP_AUTOMATION_POLICY_FILE=path):
            self.assertEqual(policy._allowed_title_patterns(), ["*Ruffle*"])
            Path(path).write_text(
                _policy_yaml(NOTEPAD_EXE, ["*Notepad*"], ["observe"]),
                encoding="utf-8",
            )
            self.assertEqual(policy._allowed_title_patterns(), ["*Notepad*"])

    def test_focus_mode_still_reads_env_in_file_mode(self):
        path = self._write(
            "ok.yaml", _policy_yaml(RUFFLE_EXE, ["*Ruffle*"], ["observe"])
        )
        with _env_patch(
            DESKTOP_AUTOMATION_POLICY_FILE=path,
            DESKTOP_AUTOMATION_FOCUS_MODE="activate",
        ):
            self.assertEqual(policy._focus_mode(), "activate")
        with _env_patch(DESKTOP_AUTOMATION_POLICY_FILE=path):
            self.assertEqual(policy._focus_mode(), "passive")


if __name__ == "__main__":
    unittest.main()
