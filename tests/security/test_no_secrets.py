"""Security gate: no hardcoded secrets in platform source (INVARIANT I3).

Scans the new platform packages (and bash adapters) for secret-like literals.
The legacy root scripts are intentionally out of scope here; they are remediated
during migration. This is the mechanical enforcement referenced by ADR and
``docs/INVARIANTS.md``.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCAN_DIRS = ["core", "modules", "integrations", "adapters"]

PATTERNS = [
    re.compile(r"eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}"),  # JWT
    re.compile(r"\b\d{8,10}:AA[A-Za-z0-9_-]{30,}\b"),  # Telegram bot token
    re.compile(r"(?i)(api[_-]?key|token|password)\s*=\s*['\"][^'\"\n]{12,}['\"]"),
]


def _iter_source_files() -> list[Path]:
    files: list[Path] = []
    for name in SCAN_DIRS:
        base = ROOT / name
        if base.exists():
            files.extend(base.rglob("*.py"))
            files.extend(base.rglob("*.sh"))
    return files


def test_no_hardcoded_secrets_in_platform_source() -> None:
    offenders: list[str] = []
    for path in _iter_source_files():
        text = path.read_text(encoding="utf-8", errors="ignore")
        for pattern in PATTERNS:
            if pattern.search(text):
                offenders.append(f"{path.relative_to(ROOT)} :: /{pattern.pattern}/")
    assert not offenders, "Possible hardcoded secrets:\n" + "\n".join(offenders)
