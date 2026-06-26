"""tdarr → Plex targeted refresh — resolve *false* duplicates after a re-encode.

Problem (see docs/design/db-repair-and-tdarr-plex.md, DESIGN 2): when tdarr
re-encodes a file in place, Plex keeps STALE metadata (``duration 0``, stale
bitrate/codec). The dupefinder's ``has_sane_metadata()`` then fails
(``"video_duration <= 0"`` / ``"...Plex analysis may be incomplete"``) and
``select_keeper()`` SAFELY SKIPS the group — the right call (it refuses to delete
based on garbage metadata). But the file lingers as a *false* duplicate until
Plex re-analyzes. This module removes the cause: it detects the affected items
and asks Plex to do a TARGETED re-analyze / partial folder scan of just those
items, so the next dupefinder pass sees sane metadata and the false duplicate
resolves normally. It NEVER overrides the dupefinder safety logic — it only
shrinks the skipped set over time.

Detection sources (config ``sources``, either/both):

  1. ``dupefinder_report`` — parse the latest ``dupefinder_report_*.json`` (same
     loader shape as ``analyst._load_dupefinder``) and select groups whose skip
     reason matches the incomplete-metadata buckets. Needs no tdarr access at
     all and directly targets the false-duplicate symptom.
  2. ``recent_files`` — walk configured ``paths`` (e.g. a tdarr output dir) for
     media files modified within ``recent_window_hours`` (a tdarr re-encode
     bumps mtime). A ``watch_list`` of explicit files is always included.

This module is strictly READ-ONLY w.r.t. the filesystem (INVARIANT I1 trivially
satisfied — it never moves, deletes or quarantines anything): the only side
effect is asking Plex to refresh/analyze, which is idempotent and re-readable.
Default is DRY_RUN-safe — in dry-run it lists the items it WOULD refresh and
never calls ``analyze()`` / ``update_section()``. All Plex I/O goes through an
injected client so tests stay offline; ``subprocess`` is never touched.

Config (config.json):
  integrations.plexrefresh :
    sources               : list, subset of ["dupefinder_report", "recent_files"]
    dupefinder_reports    : dir holding dupefinder_report_*.json (reuses analyst key)
    paths                 : list of dirs to scan for recently-modified media
    watch_list            : explicit list of files to always refresh
    sections              : optional list of Plex section names to scope to
    recent_window_hours   : mtime window for recent_files detection (default 24)
    analyze_timeout_s     : per-item analyze timeout (default 30)
    max_items_per_run     : cap on items refreshed per run (default 200)
    media_extensions      : extensions counted as media (default common video)
    ledger_db             : Sqlite-ledger path under reporting.dir (idempotency)
  integrations.plex     : {base_url, token_ref}  # token via core/secrets
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from core.cache import SqliteCache
from core.registry import register
from core.types import FailureRecord, ModuleResult, RunContext, SafetyMode

_DEFAULT_WINDOW_HOURS = 24.0
_DEFAULT_ANALYZE_TIMEOUT = 30.0
_DEFAULT_MAX_ITEMS = 200
_DEFAULT_LEDGER_DB = "cache/plexrefresh.db"
_DEFAULT_SOURCES = ("dupefinder_report", "recent_files")
_DEFAULT_EXTENSIONS = (
    ".mkv",
    ".mp4",
    ".avi",
    ".m4v",
    ".mov",
    ".wmv",
    ".mpg",
    ".mpeg",
    ".ts",
    ".m2ts",
)
# Substrings (lowercased) of a dupefinder skip reason that mean "Plex metadata is
# stale/incomplete" — exactly the false-duplicate symptom this module fixes. See
# plex_dupefinder.has_sane_metadata / select_keeper.
_STALE_REASON_MARKERS = (
    "video_duration <= 0",
    "video_bitrate <= 0",
    "video_codec missing",
    "plex analysis may be incomplete",
    "plex metadata may be stale",
)


# --- injected Plex client interface (tests inject a fake; never hits Plex) ------


class PlexItem(Protocol):
    """Minimal view of a PlexAPI item the refresh needs (stubbed in tests)."""

    @property
    def key(self) -> str: ...

    @property
    def type(self) -> str: ...

    @property
    def section(self) -> str: ...

    @property
    def folder(self) -> str:
        """The folder to partial-scan for this item (show root / movie dir)."""
        ...


class PlexRefreshClient(Protocol):
    """Thin, injectable Plex surface — the only boundary that touches Plex.

    A real implementation wraps PlexAPI (``PlexServer`` + ``library.section``),
    reusing the existing ``refresh_plex_item`` / ``refresh_plex_targets`` logic
    from ``plex_dupefinder.py``. Tests pass a fake that records calls.
    """

    def find_items_by_path(self, path: str) -> list[PlexItem]:
        """Return Plex items whose ``locations`` include ``path`` (may be empty)."""
        ...

    def analyze_item(self, item: PlexItem, timeout_s: float) -> str:
        """Trigger ``item.analyze()`` and poll. Return a status string, one of
        ``sane_and_changed`` / ``sane_unchanged`` / ``timeout`` / ``analyze_failed``
        (mirrors ``plex_dupefinder.refresh_plex_item``)."""
        ...

    def update_section(self, section: str, folder: str) -> None:
        """Partial-scan just ``folder`` of ``section`` (``section.update(path=...)``)."""
        ...


# A refresh succeeded (item demonstrably re-read or already current) for these.
_OK_STATUSES = frozenset({"sane_and_changed", "sane_unchanged"})


@dataclass(frozen=True)
class _Settings:
    sources: tuple[str, ...]
    dupefinder_reports: str | None
    paths: list[str]
    watch_list: list[str]
    sections: list[str] | None
    recent_window_hours: float
    analyze_timeout_s: float
    max_items_per_run: int
    media_extensions: tuple[str, ...]
    ledger_db: str


@dataclass
class ItemOutcome:
    """What happened to one candidate path during this run."""

    path: str
    source: str
    status: (
        str  # refreshed status, or 'dry_run' / 'fallback_section' / 'unresolved' / 'skipped_ledger'
    )
    items: int = 0
    sections: list[str] = field(default_factory=list)
    detail: str = ""


def _str_list(raw: object) -> list[str]:
    """Coerce a config value into a clean list of non-empty strings."""
    if not isinstance(raw, list):
        return []
    return [item.strip() for item in raw if isinstance(item, str) and item.strip()]


def _pos_float(raw: object, default: float) -> float:
    return (
        float(raw)
        if isinstance(raw, int | float) and not isinstance(raw, bool) and raw > 0
        else default
    )


def _pos_int(raw: object, default: int) -> int:
    return raw if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0 else default


def _settings(ctx: RunContext) -> _Settings:
    """Read ``integrations.plexrefresh`` into a typed settings object."""
    cfg = ctx.config.integrations.get("plexrefresh", {})
    raw_sources = _str_list(cfg.get("sources"))
    sources = tuple(s for s in raw_sources if s in _DEFAULT_SOURCES) or _DEFAULT_SOURCES
    reports = cfg.get("dupefinder_reports")
    raw_ext = _str_list(cfg.get("media_extensions"))
    extensions = (
        tuple(e.lower() if e.startswith(".") else f".{e.lower()}" for e in raw_ext)
        if raw_ext
        else _DEFAULT_EXTENSIONS
    )
    raw_sections = cfg.get("sections")
    sections = _str_list(raw_sections) if isinstance(raw_sections, list) else None
    ledger = cfg.get("ledger_db")
    return _Settings(
        sources=sources,
        dupefinder_reports=reports if isinstance(reports, str) and reports else None,
        paths=_str_list(cfg.get("paths")),
        watch_list=_str_list(cfg.get("watch_list")),
        sections=sections,
        recent_window_hours=_pos_float(cfg.get("recent_window_hours"), _DEFAULT_WINDOW_HOURS),
        analyze_timeout_s=_pos_float(cfg.get("analyze_timeout_s"), _DEFAULT_ANALYZE_TIMEOUT),
        max_items_per_run=_pos_int(cfg.get("max_items_per_run"), _DEFAULT_MAX_ITEMS),
        media_extensions=extensions,
        ledger_db=ledger if isinstance(ledger, str) and ledger else _DEFAULT_LEDGER_DB,
    )


# --- detection (pure: I/O passed in) -------------------------------------------


def _group_skip_reason(group: dict[str, object]) -> str:
    """Find a skip reason in a dupefinder group (revalidation or discovery).

    Same shape as ``analyst._group_skip_reason``.
    """
    reval = group.get("revalidation")
    if isinstance(reval, dict):
        reason = reval.get("reason")
        if isinstance(reason, str):
            return reason
    decision = group.get("discovery_decision")
    if isinstance(decision, dict):
        skip_reason = decision.get("skip_reason")
        if isinstance(skip_reason, str):
            return skip_reason
    skip_reason = group.get("skip_reason")
    return skip_reason if isinstance(skip_reason, str) else ""


def is_stale_metadata_reason(reason: str) -> bool:
    """True iff a dupefinder skip reason indicates stale/incomplete Plex metadata
    (the false-duplicate symptom this module fixes)."""
    low = reason.lower()
    return any(marker in low for marker in _STALE_REASON_MARKERS)


def _group_paths(group: dict[str, object]) -> list[str]:
    """Best-effort extraction of on-disk file paths from a dupefinder group.

    Groups carry per-candidate ``parts``/``files`` records; we collect any
    string that looks like a path so the item can be resolved in Plex.
    """
    out: list[str] = []
    candidates = group.get("candidates")
    items = candidates if isinstance(candidates, list) else []
    for cand in items:
        if not isinstance(cand, dict):
            continue
        files = cand.get("file") or cand.get("files")
        if isinstance(files, str):
            out.append(files)
        elif isinstance(files, list):
            out.extend(f for f in files if isinstance(f, str) and f.strip())
    # Some reports also keep a flat top-level "files" list.
    flat = group.get("files")
    if isinstance(flat, list):
        out.extend(f for f in flat if isinstance(f, str) and f.strip())
    return out


def detect_from_report(report: dict[str, object]) -> list[str]:
    """Return de-duplicated file paths of groups skipped for stale-metadata reasons."""
    groups = report.get("groups")
    if not isinstance(groups, list):
        return []
    seen: dict[str, None] = {}
    for group in groups:
        if not isinstance(group, dict):
            continue
        if not is_stale_metadata_reason(_group_skip_reason(group)):
            continue
        for path in _group_paths(group):
            seen.setdefault(path, None)
    return list(seen)


def _latest_report(directory: Path) -> dict[str, object] | None:
    """Parse the newest ``dupefinder_report_*.json`` in ``directory`` (or None)."""
    if not directory.is_dir():
        return None
    reports = sorted(directory.glob("dupefinder_report_*.json"), key=lambda p: p.stat().st_mtime)
    if not reports:
        return None
    try:
        data = json.loads(reports[-1].read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def detect_recent_files(
    roots: Iterable[str],
    *,
    extensions: tuple[str, ...],
    window_hours: float,
    now: float,
) -> list[str]:
    """Return media files under ``roots`` modified within ``window_hours``.

    ``now`` is injected so tests are deterministic. Unreadable trees are skipped
    (never raise). Paths are de-duplicated, order-preserving.
    """
    cutoff = now - window_hours * 3600.0
    seen: dict[str, None] = {}
    for root in roots:
        base = Path(root)
        if not base.exists():
            continue
        if base.is_file():
            _maybe_add_recent(base, extensions, cutoff, seen)
            continue
        for dirpath, _dirs, files in os.walk(base):
            for name in files:
                _maybe_add_recent(Path(dirpath) / name, extensions, cutoff, seen)
    return list(seen)


def _maybe_add_recent(
    path: Path, extensions: tuple[str, ...], cutoff: float, seen: dict[str, None]
) -> None:
    if path.suffix.lower() not in extensions:
        return
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return
    if mtime >= cutoff:
        seen.setdefault(str(path), None)


# --- ledger (idempotency) ------------------------------------------------------

_LEDGER_NAMESPACE = "plexrefresh"


def _ledger_marks(cache: SqliteCache, paths: Iterable[str]) -> set[str]:
    """Return the subset of ``paths`` already recorded as successfully refreshed."""
    done: set[str] = set()
    for path in paths:
        record = cache.get(Path(path))
        if record is not None and record.media_id == _LEDGER_NAMESPACE:
            done.add(path)
    return done


def _ledger_record(cache: SqliteCache, path: str, status: str) -> None:
    """Mark ``path`` as refreshed so future runs skip it (fingerprint = mtime+size,
    so a *new* re-encode of the same path naturally re-qualifies)."""
    cache.put(Path(path), media_id=_LEDGER_NAMESPACE, extra={"status": status})


# --- refresh orchestration -----------------------------------------------------


def _refresh_one(
    path: str,
    source: str,
    client: PlexRefreshClient,
    sections: list[str] | None,
    timeout_s: float,
) -> ItemOutcome:
    """Resolve ``path`` to Plex item(s) and refresh; fall back to a folder scan.

    Per-item ``analyze()`` is precise; if no item resolves we partial-scan the
    item folder(s) (``section.update(path=...)``). A failure here is captured in
    the outcome, never raised (one bad path must not abort the run).
    """
    try:
        items = client.find_items_by_path(path)
    except Exception as exc:
        return ItemOutcome(path=path, source=source, status="error", detail=str(exc))

    if sections is not None:
        wanted = set(sections)
        items = [it for it in items if it.section in wanted]

    if not items:
        return ItemOutcome(path=path, source=source, status="unresolved")

    statuses: list[str] = []
    refreshed_sections: list[str] = []
    for item in items:
        try:
            status = client.analyze_item(item, timeout_s)
        except Exception as exc:
            statuses.append("analyze_failed")
            return ItemOutcome(
                path=path,
                source=source,
                status="analyze_failed",
                items=len(items),
                detail=str(exc),
            )
        statuses.append(status)
        if status not in _OK_STATUSES:
            # Per-item analyze did not demonstrably refresh — fall back to a
            # targeted partial scan of just this item's folder.
            try:
                client.update_section(item.section, item.folder)
                refreshed_sections.append(item.section)
            except Exception as exc:
                return ItemOutcome(
                    path=path,
                    source=source,
                    status="error",
                    items=len(items),
                    detail=f"section fallback failed: {exc}",
                )

    if refreshed_sections:
        return ItemOutcome(
            path=path,
            source=source,
            status="fallback_section",
            items=len(items),
            sections=sorted(set(refreshed_sections)),
            detail=",".join(statuses),
        )
    # All items returned an OK analyze status.
    final = "sane_and_changed" if "sane_and_changed" in statuses else "sane_unchanged"
    return ItemOutcome(path=path, source=source, status=final, items=len(items))


def _candidates(ctx: RunContext, settings: _Settings, now: float) -> list[tuple[str, str]]:
    """Collect (path, source) candidates from every configured source, de-duped."""
    seen: dict[str, str] = {}

    def add(path: str, source: str) -> None:
        seen.setdefault(path, source)

    if "dupefinder_report" in settings.sources and settings.dupefinder_reports:
        report = _latest_report(Path(settings.dupefinder_reports))
        if report is not None:
            for path in detect_from_report(report):
                add(path, "dupefinder_report")
    if "recent_files" in settings.sources:
        for path in detect_recent_files(
            settings.paths,
            extensions=settings.media_extensions,
            window_hours=settings.recent_window_hours,
            now=now,
        ):
            add(path, "recent_files")
    for path in settings.watch_list:
        add(path, "watch_list")
    return [(path, source) for path, source in seen.items()]


def _write_report(
    ctx: RunContext,
    settings: _Settings,
    outcomes: list[ItemOutcome],
    dry_run: bool,
    note: str,
) -> None:
    out_dir = ctx.config.reporting.dir / "plexrefresh"
    out_dir.mkdir(parents=True, exist_ok=True)
    refreshed = [o for o in outcomes if o.status in _OK_STATUSES or o.status == "fallback_section"]
    still_stale = [o for o in outcomes if o.status in ("timeout", "analyze_failed", "unresolved")]
    (out_dir / "plan.json").write_text(
        json.dumps(
            {
                "dry_run": dry_run,
                "sources": list(settings.sources),
                "candidates": len(outcomes),
                "refreshed": len(refreshed),
                "still_stale": len(still_stale),
                "note": note,
                "items": [
                    {
                        "path": o.path,
                        "source": o.source,
                        "status": o.status,
                        "items": o.items,
                        "sections": o.sections,
                        "detail": o.detail,
                    }
                    for o in outcomes
                ],
            },
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    lines = [
        "# Plex targeted refresh (tdarr false-duplicate fix)",
        "",
        f"Mode: {'DRY-RUN (no Plex writes)' if dry_run else 'LIVE'}",
        f"Sources: {', '.join(settings.sources)}",
        f"Candidates: {len(outcomes)}",
        f"Refreshed: {len(refreshed)}",
        f"Still stale / unresolved: {len(still_stale)}",
        "",
        "## Items",
    ]
    if outcomes:
        for o in sorted(outcomes, key=lambda x: (x.status, x.path)):
            suffix = f" [{o.detail}]" if o.detail else ""
            lines.append(f"- {o.status}: {o.path} ({o.source}){suffix}")
    else:
        lines.append("(no candidates detected)")
    if note:
        lines += ["", f"> {note}"]
    (out_dir / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _clock() -> float:
    import time

    return time.time()


@register("plexrefresh")
def run(
    ctx: RunContext,
    *,
    client: PlexRefreshClient | None = None,
    now: float | None = None,
) -> ModuleResult:
    """Detect tdarr-re-encoded items with stale Plex metadata and refresh them.

    ``client`` (the Plex boundary) and ``now`` (clock) are injected so tests run
    offline and deterministically. In DRY_RUN (default) it lists candidates and
    never calls Plex.
    """
    result = ModuleResult(module="plexrefresh", run_id=ctx.run_id, mode=ctx.mode)
    settings = _settings(ctx)
    when = now if now is not None else _clock()
    dry_run = ctx.mode != SafetyMode.LIVE

    candidates = _candidates(ctx, settings, when)

    # Idempotency: drop paths already refreshed (unchanged since), cap the batch.
    ledger = SqliteCache(ctx.config.reporting.dir / settings.ledger_db)
    outcomes: list[ItemOutcome] = []
    try:
        done = _ledger_marks(ledger, (p for p, _ in candidates))
        pending = [(p, s) for p, s in candidates if p not in done]
        capped = pending[: settings.max_items_per_run]
        truncated = len(pending) - len(capped)

        if dry_run:
            # List what we WOULD refresh; never touch Plex.
            outcomes = [ItemOutcome(path=p, source=s, status="dry_run") for p, s in capped]
        elif client is None:
            result.add_failure(
                FailureRecord(
                    category="config",
                    message="plexrefresh in LIVE mode requires an injected Plex client",
                )
            )
        else:
            for path, source in capped:
                outcome = _refresh_one(
                    path, source, client, settings.sections, settings.analyze_timeout_s
                )
                outcomes.append(outcome)
                if outcome.status in _OK_STATUSES or outcome.status == "fallback_section":
                    _ledger_record(ledger, path, outcome.status)
                else:
                    result.add_failure(
                        FailureRecord(
                            category="integration",
                            message=f"refresh not confirmed for {path}: {outcome.status}",
                            src=path,
                        )
                    )
            ledger.save()

        note = ""
        if not candidates:
            note = "No stale-metadata candidates detected."
        elif truncated > 0:
            note = f"capped at max_items_per_run; {truncated} candidate(s) deferred to next run"
        elif done:
            note = f"{len(done)} candidate(s) already refreshed (ledger) — skipped"
    finally:
        ledger.close()

    _write_report(ctx, settings, outcomes, dry_run, note)
    refreshed = sum(
        1 for o in outcomes if o.status in _OK_STATUSES or o.status == "fallback_section"
    )
    ctx.logger.info(
        "plexrefresh done",
        candidates=len(candidates),
        considered=len(outcomes),
        refreshed=refreshed,
        dry_run=dry_run,
    )
    result.metrics["candidates"] = float(len(candidates))
    result.metrics["refreshed"] = float(refreshed)
    # Read-only w.r.t. files; "actions" counts the Plex refreshes we issued (0 in dry-run).
    result.actions = 0 if dry_run else refreshed
    return result
