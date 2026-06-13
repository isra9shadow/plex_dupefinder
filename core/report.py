"""Single run report: a ``RunReport`` dataclass serialised to JSON (no schema dep).

Metrics are counters carried inside the report (ADR-0008: no separate metrics
module/textfile until dashboards exist).
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from core.types import RunReport


def to_dict(report: RunReport) -> dict[str, object]:
    return asdict(report)


def write_report(report: RunReport, directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    out = directory / f"{report.run_id}.json"
    out.write_text(
        json.dumps(asdict(report), indent=2, default=str, sort_keys=True),
        encoding="utf-8",
    )
    return out
