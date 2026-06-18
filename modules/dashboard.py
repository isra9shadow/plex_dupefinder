"""HomeLab dashboard (IMP-10 v1, read-only): aggregate every module's latest report
into one `reports/dashboard.md` — see the whole homelab in <1 minute.
"""

from __future__ import annotations

import json
from pathlib import Path

from core.registry import register
from core.types import ModuleResult, RunContext


def _load(path: Path) -> object | None:
    if not path.is_file():
        return None
    try:
        data: object = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return data


def _count_list(value: object) -> int:
    return len(value) if isinstance(value, list) else 0


@register("dashboard")
def run(ctx: RunContext) -> ModuleResult:
    result = ModuleResult(module="dashboard", run_id=ctx.run_id, mode=ctx.mode)
    base = ctx.config.reporting.dir
    lines = ["# HomeLab Dashboard", "", f"_run {ctx.run_id}_", ""]
    sections = 0

    docker = _load(base / "inventory" / "docker_inventory.json")
    if isinstance(docker, list):
        lines.append(f"- **Docker:** {len(docker)} containers")
        sections += 1

    disk = _load(base / "inventory" / "disk_inventory.json")
    if isinstance(disk, dict):
        disks = disk.get("disks")
        if isinstance(disks, list):
            warns = sum(1 for d in disks if isinstance(d, dict) and d.get("smart_warning"))
            lines.append(f"- **Disks:** {len(disks)} ({warns} ⚠️ SMART)")
            sections += 1

    media = _load(base / "media" / "integrity.json")
    if isinstance(media, list):
        flagged = sum(1 for r in media if isinstance(r, dict) and r.get("verdict") != "ok")
        lines.append(f"- **Media integrity:** {len(media)} scanned, {flagged} flagged")
        sections += 1

    orphans = _load(base / "arr" / "orphans.json")
    if isinstance(orphans, dict):
        movies = _count_list(orphans.get("movies"))
        series = _count_list(orphans.get("series"))
        lines.append(f"- **ARR orphans:** {movies} movies, {series} series")
        sections += 1

    stalled = _load(base / "downloads" / "stalled.json")
    if isinstance(stalled, list):
        lines.append(f"- **Stalled downloads:** {len(stalled)}")
        sections += 1

    if sections == 0:
        lines.append("_no module reports found — run the pipelines first_")
    base.mkdir(parents=True, exist_ok=True)
    (base / "dashboard.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    ctx.logger.info("dashboard", sections=sections)
    result.actions = sections
    return result
