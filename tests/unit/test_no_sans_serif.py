"""
Discipline audit: the Erebus design locked in an all-mono type system.
This test fails if anyone reintroduces a sans-serif family name in the
app code. Whitelist any legitimate occurrences explicitly.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


FORBIDDEN = (
    "Inter",
    "Roboto",
    "Space Grotesk",
    "Helvetica",
    "Arial",
    "Segoe UI",
    "sans-serif",
)

# Files and directories we never audit — tests, docs, packaging metadata,
# the plan file. The plan references "Inter" historically as a rejected
# option so it's allowed to mention it.
EXCLUDED_DIRS = {
    "tests",
    ".git",
    ".pytest_cache",
    "__pycache__",
    ".venv",
    "dist",
    "build",
}
EXCLUDED_SUFFIXES = {".md", ".toml", ".txt", ".otf", ".ttf", ".woff2"}


def _audit_roots() -> list[Path]:
    repo_root = Path(__file__).resolve().parents[2]
    return [repo_root / "flashback_sampler"]


def test_no_sans_serif_family_names_in_app_code():
    offenders: list[tuple[Path, int, str, str]] = []
    for root in _audit_roots():
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            if any(part in EXCLUDED_DIRS for part in path.parts):
                continue
            if path.suffix in EXCLUDED_SUFFIXES:
                continue
            if path.suffix != ".py":
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:  # pragma: no cover
                continue
            for lineno, line in enumerate(text.splitlines(), start=1):
                stripped = line.strip()
                # Skip pure comments and docstring-ish lines
                if stripped.startswith("#"):
                    continue
                for forbidden in FORBIDDEN:
                    # Use word-boundary match so "Roboto Mono" is excluded
                    # (we want Roboto Mono to be forbidden too — it's a
                    # legacy reference, Monaspace replaces it).
                    if re.search(rf'"{re.escape(forbidden)}"', line):
                        offenders.append(
                            (path.relative_to(root.parent), lineno, forbidden, line)
                        )

    if offenders:
        lines = ["sans-serif leak detected — Erebus is all-mono:"]
        for path, lineno, word, line in offenders:
            lines.append(f"  {path}:{lineno}  found {word!r} in {line.strip()}")
        pytest.fail("\n".join(lines))
