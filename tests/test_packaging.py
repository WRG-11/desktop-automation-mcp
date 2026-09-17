"""Release metadata contracts that a fresh installer must preserve."""

from __future__ import annotations

from pathlib import Path
import sys
import tomllib
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from desktop_automation_mcp import __version__


class PackagingDependencyTests(unittest.TestCase):
    def test_mcp_dependency_does_not_allow_the_incompatible_v2_api(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        metadata = tomllib.loads(
            (project_root / "pyproject.toml").read_text(encoding="utf-8")
        )

        dependencies = metadata["project"]["dependencies"]
        self.assertIn("mcp>=1.29.1,<2", dependencies)

    def test_declared_runtime_dependency_floors_are_explicit(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        metadata = tomllib.loads(
            (project_root / "pyproject.toml").read_text(encoding="utf-8")
        )

        self.assertIn("Pillow>=12.3", metadata["project"]["dependencies"])

    def test_package_version_matches_project_metadata(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        metadata = tomllib.loads(
            (project_root / "pyproject.toml").read_text(encoding="utf-8")
        )

        self.assertEqual(__version__, metadata["project"]["version"])
