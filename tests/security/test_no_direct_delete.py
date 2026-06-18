"""Security: destructive filesystem ops only happen in core/fs.py (INVARIANT I1)."""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCAN_DIRS = ["modules", "integrations", "adapters"]
PATTERN = re.compile(r"(os\.remove|os\.unlink|shutil\.rmtree|shutil\.move|\.unlink\()")


def _files() -> Iterator[Path]:
    for name in SCAN_DIRS:
        base = ROOT / name
        if base.exists():
            yield from base.rglob("*.py")


def test_no_direct_delete_outside_core_fs() -> None:
    offenders = [
        str(path.relative_to(ROOT))
        for path in _files()
        if PATTERN.search(path.read_text(encoding="utf-8"))
    ]
    assert not offenders, f"destructive fs op outside core/fs: {offenders}"
