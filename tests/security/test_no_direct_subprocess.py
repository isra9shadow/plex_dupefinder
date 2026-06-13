"""Security: subprocess only happens in adapters/command.py (the audited boundary)."""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCAN_DIRS = ["modules", "integrations", "adapters"]
ALLOWED = {ROOT / "adapters" / "command.py"}
# Match real usage (import or call), not the word in a docstring/comment.
PATTERN = re.compile(r"\bimport subprocess\b|\bsubprocess\.\w|\bos\.system\s*\(|\bos\.popen\s*\(")


def _files() -> Iterator[Path]:
    for name in SCAN_DIRS:
        base = ROOT / name
        if base.exists():
            for path in base.rglob("*.py"):
                if path not in ALLOWED:
                    yield path


def test_no_direct_subprocess_outside_command() -> None:
    offenders = [
        str(path.relative_to(ROOT))
        for path in _files()
        if PATTERN.search(path.read_text(encoding="utf-8"))
    ]
    assert not offenders, f"subprocess used outside adapters/command: {offenders}"
