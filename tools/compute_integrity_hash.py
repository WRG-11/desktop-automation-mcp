"""Compute or verify the SHA-256 integrity list for `src/`'s production code.

Automates the manual PowerShell command from
`docs/design/package-integrity-and-rollback.md` ("integrity hash"): hashes
every `*.py` file under `src/` (excluding `__pycache__`), sorted by
relative path, in the `HASH  path` format `INTEGRITY.sha256` uses.

Usage:
    py -3.12 tools/compute_integrity_hash.py write   # (re)writes INTEGRITY.sha256
    py -3.12 tools/compute_integrity_hash.py check   # compares tree against it

This script only reads/writes `INTEGRITY.sha256` at the repo root and files
under `src/`; it never touches git, never touches `tests/`/`docs/`/`schema/`/
`tools/` content (those are deliberately excluded from the integrity claim —
see the design doc's rationale), and makes no network calls.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
INTEGRITY_FILE = REPO_ROOT / "INTEGRITY.sha256"


def _hashed_files() -> list[Path]:
    """Every `*.py` under `src/`, excluding `__pycache__`, sorted by path.

    Sorting matters: the file's order is part of what a byte-for-byte
    `INTEGRITY.sha256` comparison checks, so it must be deterministic
    across machines/runs, not directory-iteration order.
    """
    return sorted(
        path for path in SRC_DIR.rglob("*.py") if "__pycache__" not in path.parts
    )


def _relative(path: Path) -> str:
    return str(path.relative_to(REPO_ROOT))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest().upper()


def compute_lines() -> list[str]:
    """One `HASH  path` line per hashed file, in the file's own iteration
    order (already sorted by `_hashed_files`)."""
    files = _hashed_files()
    if not files:
        # A probe that finds nothing under src/ is not the same claim as
        # "the tree is clean" — src/ existing-but-empty of .py files is a
        # broken-probe condition worth saying out loud, not silently
        # writing an empty INTEGRITY.sha256.
        raise RuntimeError(
            f"no .py files found under src/ ({SRC_DIR}); "
            "this is not an 'empty result', probably run from the wrong directory."
        )
    return [f"{_sha256(path)}  {_relative(path)}" for path in files]


def write_integrity_file() -> Path:
    lines = compute_lines()
    INTEGRITY_FILE.write_text("\n".join(lines) + "\n", encoding="ascii")
    return INTEGRITY_FILE


def check_integrity_file() -> tuple[bool, list[str]]:
    """Compare the current tree against the stored `INTEGRITY.sha256`.

    Returns (matches, problems). `problems` lists every discrepancy by
    name — a missing INTEGRITY.sha256 is its own distinct problem, not
    silently treated as "0 mismatches" (broken-probe ≠ absence).
    """
    if not INTEGRITY_FILE.exists():
        return False, [f"{INTEGRITY_FILE} not found; run 'write' first."]

    recorded: dict[str, str] = {}
    for line_number, line in enumerate(
        INTEGRITY_FILE.read_text(encoding="ascii").splitlines(), start=1
    ):
        if not line.strip():
            continue
        parts = line.split("  ", 1)
        if len(parts) != 2:
            return False, [f"{INTEGRITY_FILE}:{line_number} malformed: {line!r}"]
        recorded[parts[1]] = parts[0]

    current = {_relative(path): _sha256(path) for path in _hashed_files()}

    problems: list[str] = []
    for path, hash_value in sorted(recorded.items()):
        if path not in current:
            problems.append(f"recorded but now missing: {path}")
        elif current[path] != hash_value:
            problems.append(f"hash mismatch: {path}")
    for path in sorted(set(current) - set(recorded)):
        problems.append(f"in tree but not recorded: {path}")

    return not problems, problems


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[1] not in ("write", "check"):
        print(f"usage: {argv[0]} write|check", file=sys.stderr)
        return 2
    if argv[1] == "write":
        try:
            path = write_integrity_file()
        except RuntimeError as exc:
            print(f"PROBE ERROR: {exc}", file=sys.stderr)
            return 2
        print(f"wrote: {path}")
        return 0
    matches, problems = check_integrity_file()
    if matches:
        print("OK: tree matches INTEGRITY.sha256.")
        return 0
    print("INVALID: tree does not match INTEGRITY.sha256:")
    for problem in problems:
        print(f"  - {problem}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
