"""Media integrity (report-only): ffprobe + expected runtime → verdict.

Parity target: legacy ``media_triage_daily.sh`` (1422 lines). **Report-only** —
classifies each file OK / DOUBTFUL / BAD and writes a report; it never moves or
deletes. Quarantining bad/doubtful files (via ``core/fs``) is enabled only after a
parity window (MIGRATION_PLAN §9).
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path

from adapters import ffprobe
from adapters.ffprobe import MediaProbe
from core import secrets
from core.cache import SqliteCache
from core.errors import IntegrationError, SecretError
from core.registry import register
from core.timing import Profiler
from core.types import ModuleResult, RunContext, Verdict
from integrations.tmdb import TmdbClient

_MEDIA_EXT = {".mkv", ".mp4", ".m4v", ".avi", ".mov", ".ts", ".m2ts", ".wmv"}


@dataclass(frozen=True)
class _Thresholds:
    min_seconds: int
    soft_abs: int
    hard_abs: int
    soft_pct: float
    hard_pct: float


_THRESHOLDS = {
    "movie": _Thresholds(1200, 20, 35, 0.20, 0.35),
    "series": _Thresholds(240, 8, 14, 0.25, 0.40),
}


def _shortfall(actual_min: float, expected_min: float, thr: _Thresholds) -> int:
    if expected_min <= 0 or actual_min >= expected_min:
        return 0
    diff = expected_min - actual_min
    soft = max(thr.soft_abs, expected_min * thr.soft_pct)
    hard = max(thr.hard_abs, expected_min * thr.hard_pct)
    if diff > hard:
        return 2
    if diff > soft:
        return 1
    return 0


def classify(
    probe: MediaProbe, kind: str, expected_min: float | None = None
) -> tuple[Verdict, str]:
    thr = _THRESHOLDS[kind]
    if not probe.has_video:
        return Verdict.BAD, "no_video_stream"
    if probe.duration_seconds <= 0:
        if probe.decodes_ok:
            return Verdict.DOUBTFUL, "no_duration_decodes"
        return Verdict.BAD, "no_duration_decode_error"
    if not probe.decodes_ok:
        return Verdict.BAD, "decode_error"
    if probe.duration_seconds < thr.min_seconds:
        return Verdict.BAD, "too_short"
    if expected_min is not None:
        cls = _shortfall(probe.duration_seconds / 60.0, expected_min, thr)
        if cls == 2:
            return Verdict.BAD, "runtime_shortfall_hard"
        if cls == 1:
            return Verdict.DOUBTFUL, "runtime_shortfall_soft"
    return Verdict.OK, "ok"


def _title_year(folder: str) -> tuple[str, int | None]:
    match = re.search(r"\((\d{4})\)", folder)
    year = int(match.group(1)) if match else None
    title = re.sub(r"\(\d{4}\).*$", "", folder)
    title = re.sub(r"[._]+", " ", title).strip()
    return (title or folder), year


def _maybe_tmdb(ctx: RunContext) -> TmdbClient | None:
    settings = ctx.config.integrations.get("tmdb")
    if not settings:
        return None
    ref = settings.get("bearer_ref")
    if not isinstance(ref, str):
        return None
    try:
        return TmdbClient(secrets.require(ref))
    except SecretError:
        return None


def _iter_media(root: Path) -> Iterator[Path]:
    if not root.is_dir():
        return
    for path in root.rglob("*"):
        if path.is_file() and path.suffix.lower() in _MEDIA_EXT:
            yield path


def _expected_min(tmdb: TmdbClient | None, kind: str, path: Path) -> float | None:
    if tmdb is None or kind != "movie":
        return None
    title, year = _title_year(path.parent.name)
    try:
        return tmdb.expected_movie_runtime(title, year)
    except IntegrationError:
        return None


def _probe_from_dict(data: dict[str, object]) -> MediaProbe | None:
    duration = data.get("duration_seconds")
    if not isinstance(duration, int | float):
        return None
    return MediaProbe(
        bool(data.get("has_video")),
        bool(data.get("has_audio")),
        float(duration),
        bool(data.get("decodes_ok")),
    )


_DEFAULT_WORKERS = 6
# Cache rows not seen for this long (file deleted/moved) are pruned each run.
_CACHE_RETENTION_DAYS = 30


def _workers() -> int:
    raw = os.environ.get("IZUMI_DISCOVERY_WORKERS", "")
    return int(raw) if raw.isdigit() and int(raw) > 0 else _DEFAULT_WORKERS


def _safe_probe(media: Path) -> MediaProbe:
    """ffprobe.probe that never raises — one unreadable file must not abort the
    whole batch (and lose the run's cache progress). A failure is reported as a
    non-decoding probe, which the verdict engine flags downstream (CTO-19)."""
    try:
        return ffprobe.probe(media)
    except Exception:
        return MediaProbe(has_video=False, has_audio=False, duration_seconds=0.0, decodes_ok=False)


def _probe_all(files: list[Path], cache: SqliteCache, workers: int) -> dict[Path, MediaProbe]:
    """Probe ``files``, serving cache hits and probing misses in parallel (read-only).

    Every present file is re-``put`` (refreshing ``last_seen``) so files that
    vanish age out via ``prune``; the expensive ffprobe subprocess runs only on
    misses.
    """
    results: dict[Path, MediaProbe] = {}
    misses: list[Path] = []
    for media in files:
        cached = cache.get(media)
        probe = (
            _probe_from_dict(cached.extra)
            if cached is not None and isinstance(cached.extra, dict)
            else None
        )
        if probe is not None:
            results[media] = probe
        else:
            misses.append(media)
    if misses:
        if workers > 1 and len(misses) > 1:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                probed = list(pool.map(_safe_probe, misses))
        else:
            probed = [_safe_probe(media) for media in misses]
        for media, probe in zip(misses, probed, strict=True):
            results[media] = probe
    for media in files:
        probe = results[media]
        cache.put(media, duration=probe.duration_seconds, extra=asdict(probe))
    return results


def _scan(
    root: Path,
    kind: str,
    tmdb: TmdbClient | None,
    cache: SqliteCache,
    profiler: Profiler,
    workers: int,
) -> list[dict[str, object]]:
    files = list(_iter_media(root))
    with profiler.phase("probe"):
        probes = _probe_all(files, cache, workers)
    records: list[dict[str, object]] = []
    for media in files:
        probe = probes[media]
        with profiler.phase("runtime"):
            expected = _expected_min(tmdb, kind, media)
        with profiler.phase("classify"):
            verdict, reason = classify(probe, kind, expected)
        records.append(
            {
                "file": str(media),
                "kind": kind,
                "verdict": verdict.value,
                "reason": reason,
                "duration_min": round(probe.duration_seconds / 60.0, 1),
            }
        )
    return records


def _write_report(ctx: RunContext, records: list[dict[str, object]]) -> None:
    out_dir = ctx.config.reporting.dir / "media"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "integrity.json").write_text(
        json.dumps(records, indent=2, sort_keys=True), encoding="utf-8"
    )
    flagged = [r for r in records if r["verdict"] != "ok"]
    lines = [
        "# Media Integrity (report-only)",
        "",
        f"_{len(records)} scanned, {len(flagged)} flagged_",
        "",
        *[
            f"- [{r['verdict']}] {r['reason']} — {r['file']} ({r['duration_min']}m)"
            for r in flagged
        ],
        "",
    ]
    (out_dir / "integrity.md").write_text("\n".join(lines), encoding="utf-8")


@register("media_integrity")
def run(ctx: RunContext) -> ModuleResult:
    result = ModuleResult(module="media_integrity", run_id=ctx.run_id, mode=ctx.mode)
    tmdb = _maybe_tmdb(ctx)
    cache = SqliteCache(ctx.config.reporting.dir / "cache" / "media.db")
    profiler = Profiler()
    workers = _workers()
    paths = ctx.config.paths
    movies = Path(paths.get("movies_root", "."))
    series = Path(paths.get("series_root", "."))
    try:
        records = _scan(movies, "movie", tmdb, cache, profiler, workers)
        records += _scan(series, "series", tmdb, cache, profiler, workers)
        cache.record_run(ctx.run_id, profiler.metrics())
        cache.save()
        pruned = cache.prune(older_than_days=_CACHE_RETENTION_DAYS)
    finally:
        cache.close()
    if pruned:
        ctx.logger.info("media cache pruned", entries=pruned)
    _write_report(ctx, records)
    flagged = sum(1 for r in records if r["verdict"] != "ok")
    ctx.logger.info("media integrity (report-only)", scanned=len(records), flagged=flagged)
    result.actions = flagged
    result.metrics = profiler.metrics()
    return result
