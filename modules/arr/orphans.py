"""ARR orphan detection (report-only): on-disk folders not mapped in Radarr/Sonarr.

Parity target: legacy ``arr_orphans_auto.sh``. **Report-only** — never moves
anything. Moving orphans to quarantine (via ``core/fs``) is enabled only after a
parity window (MIGRATION_PLAN §9). Comparison is by top-level folder name, which
avoids ARR↔host path-prefix mapping entirely.
"""

from __future__ import annotations

import json
from pathlib import Path

from core import secrets
from core.errors import ConfigError, IntegrationError, SecretError
from core.registry import register
from core.types import FailureRecord, ModuleResult, RunContext
from integrations.radarr import RadarrClient
from integrations.sonarr import SonarrClient

_EXCLUDE = {
    "@eaDir",
    ".actors",
    ".Recycle.Bin",
    "#recycle",
    "lost+found",
    "PeliculasHuerfanas",
    "SeriesHuerfanas",
}


def _top_level_dirs(root: Path) -> set[str]:
    if not root.is_dir():
        return set()
    return {child.name for child in root.iterdir() if child.is_dir()}


def _mapped_names(items: list[dict[str, object]]) -> set[str]:
    names: set[str] = set()
    for item in items:
        path = item.get("path")
        if isinstance(path, str) and path:
            names.add(Path(path).name)
    return names


def find_orphans(on_disk: set[str], mapped: set[str]) -> list[str]:
    return sorted(name for name in on_disk - mapped if name not in _EXCLUDE)


def _service(settings: dict[str, object]) -> tuple[str, str]:
    url = settings.get("url")
    ref = settings.get("api_key_ref")
    if not isinstance(url, str) or not isinstance(ref, str):
        raise ConfigError("integration entry needs string 'url' and 'api_key_ref'")
    return url, secrets.require(ref)


def _radarr(ctx: RunContext) -> RadarrClient:
    url, key = _service(ctx.config.integrations.get("radarr", {}))
    return RadarrClient(url, key)


def _sonarr(ctx: RunContext) -> SonarrClient:
    url, key = _service(ctx.config.integrations.get("sonarr", {}))
    return SonarrClient(url, key)


def _write_report(ctx: RunContext, movies: list[str], series: list[str]) -> None:
    out_dir = ctx.config.reporting.dir / "arr"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "orphans.json").write_text(
        json.dumps({"movies": movies, "series": series}, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    lines = [
        "# ARR Orphans (report-only)",
        "",
        f"## Movies ({len(movies)})",
        *[f"- {m}" for m in movies],
        "",
        f"## Series ({len(series)})",
        *[f"- {s}" for s in series],
        "",
    ]
    (out_dir / "orphans.md").write_text("\n".join(lines), encoding="utf-8")


@register("arr_orphans")
def run(ctx: RunContext) -> ModuleResult:
    result = ModuleResult(module="arr_orphans", run_id=ctx.run_id, mode=ctx.mode)
    try:
        movies_mapped = _mapped_names(_radarr(ctx).movies())
        series_mapped = _mapped_names(_sonarr(ctx).series())
    except (ConfigError, SecretError) as exc:
        result.add_failure(FailureRecord(category="config", message=str(exc)))
        return result
    except IntegrationError as exc:
        result.add_failure(FailureRecord(category="integration", message=str(exc)))
        return result

    paths = ctx.config.paths
    movies_orphans = find_orphans(
        _top_level_dirs(Path(paths.get("movies_root", "."))), movies_mapped
    )
    series_orphans = find_orphans(
        _top_level_dirs(Path(paths.get("series_root", "."))), series_mapped
    )
    _write_report(ctx, movies_orphans, series_orphans)
    ctx.logger.info(
        "arr orphans (report-only)", movies=len(movies_orphans), series=len(series_orphans)
    )
    result.actions = len(movies_orphans) + len(series_orphans)
    return result
