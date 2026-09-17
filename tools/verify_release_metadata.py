"""Verify that one version tag, package version, and changelog entry agree."""

from __future__ import annotations

import argparse
from pathlib import Path
import re
import tomllib


ROOT = Path(__file__).resolve().parents[1]


def package_version() -> str:
    """Read the public package version from the project metadata."""
    with (ROOT / "pyproject.toml").open("rb") as file:
        project = tomllib.load(file)["project"]
    version = project.get("version")
    if not isinstance(version, str) or not version:
        raise ValueError("pyproject.toml must define a non-empty project.version")
    return version


def verify(tag: str) -> str:
    """Return the package version when the release metadata is consistent."""
    version = package_version()
    expected_tag = f"v{version}"
    if tag != expected_tag:
        raise ValueError(f"tag {tag!r} does not match project version {expected_tag!r}")

    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    heading = re.compile(
        rf"^## \[{re.escape(version)}\] - \d{{4}}-\d{{2}}-\d{{2}}$", re.M
    )
    if not heading.search(changelog):
        raise ValueError(f"CHANGELOG.md has no dated entry for version {version!r}")
    return version


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tag", help="Release tag, for example v1.2.3")
    arguments = parser.parse_args()
    version = verify(arguments.tag)
    print(f"Release metadata OK: {arguments.tag} == {version}")


if __name__ == "__main__":
    main()
