"""Post-dupefinder media organizer (cleanup is real, reorganization is a plan).

Two responsibilities, both safe-by-default:

  1. CLEANUP (acts) — junk sidecars (``.nfo`` / ``.txt`` / ``.url``), zero-byte
     files and empty directories are MOVED TO QUARANTINE via ``core/fs`` (never
     ``rm`` — INVARIANT I1) so they are recoverable. Honours DRY_RUN.

  2. IDENTIFY (report-only) — every media file is sent to Gemini, which returns a
     structured title/type/season/episode plus a self-reported confidence %. We
     turn that into a suggested target path under the Movies/Series roots and
     write it to a plan report.

  3. APPLY (acts, opt-in) — when ``integrations.gemini.apply`` is True, every
     suggestion that clears the confidence gate AND resolved to a target is MOVED
     into its canonical path via ``core/fs`` ``relocate`` (INVARIANT I1: the only
     sanctioned mover besides quarantine). It never clobbers an existing file and
     honours DRY_RUN. Defaults to False, so the module stays report-only unless
     explicitly opted in (cf. arr_orphans started report-only too).

Config (config.json):
  paths.organizer_source : directory to scan (the "manuales" dump)
  paths.movies_root      : target root for movie suggestions
  paths.series_root      : target root for series suggestions
  integrations.gemini    : {api_key_ref, model, batch_size, confidence_threshold,
                            apply}  # apply: bool, default False
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from core import secrets
from core.errors import (
    ConfigError,
    IntegrationError,
    SafetyError,
    SecretError,
    ValidationError,
)
from core.registry import register
from core.types import FailureRecord, ModuleResult, RunContext
from integrations.gemini import GeminiClient

_JUNK_SUFFIXES = {".nfo", ".txt", ".url"}
_MEDIA_SUFFIXES = {
    ".mkv",
    ".mp4",
    ".avi",
    ".m4v",
    ".mov",
    ".wmv",
    ".mpg",
    ".mpeg",
    ".ts",
    ".flv",
    ".webm",
    ".m2ts",
}
_DEFAULT_THRESHOLD = 90.0


@dataclass(frozen=True)
class Suggestion:
    filename: str
    kind: str  # "movie" | "series" | "unknown"
    title: str
    year: int | None
    season: int | None
    episode: int | None
    confidence: float
    target: str | None


# --- scanning (pure, testable) -------------------------------------------------


def is_junk(path: Path) -> bool:
    """A removable sidecar (.nfo/.txt/.url) or a zero-byte file."""
    if path.suffix.lower() in _JUNK_SUFFIXES:
        return True
    try:
        return path.is_file() and path.stat().st_size == 0
    except OSError:  # pragma: no cover - defensive
        return False


def is_media(path: Path) -> bool:
    return path.suffix.lower() in _MEDIA_SUFFIXES


def scan(root: Path) -> tuple[list[Path], list[Path]]:
    """Return ``(junk_files, media_files)`` found anywhere under ``root``."""
    junk: list[Path] = []
    media: list[Path] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if is_junk(path):
            junk.append(path)
        elif is_media(path):
            media.append(path)
    return junk, media


def empty_dirs(root: Path) -> list[Path]:
    """Directories under ``root`` that currently hold nothing (deepest first)."""
    found: list[Path] = []
    for path in sorted(root.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if path.is_dir() and not any(path.iterdir()):
            found.append(path)
    return found


# --- suggestion shaping (pure, testable) ---------------------------------------


def _opt_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


def _clamp_confidence(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return 0.0
    return float(min(100, max(0, value)))


def suggested_target(
    kind: str,
    title: str,
    year: int | None,
    season: int | None,
    episode: int | None,
    ext: str,
    movies_root: str,
    series_root: str,
) -> str | None:
    """Build a canonical destination path, or None when we lack the essentials."""
    if not title:
        return None
    if kind == "movie":
        folder = f"{title} ({year})" if year else title
        return str(Path(movies_root) / folder / f"{folder}{ext}")
    if kind == "series" and season is not None and episode is not None:
        show = f"{title} ({year})" if year else title
        fname = f"{title} - S{season:02d}E{episode:02d}{ext}"
        return str(Path(series_root) / show / f"Season {season:02d}" / fname)
    return None


def normalize_suggestion(
    raw: dict[str, object], fallback_name: str, movies_root: str, series_root: str
) -> Suggestion:
    """Validate one raw model entry into a typed, path-resolved Suggestion."""
    name = raw.get("filename")
    filename = name if isinstance(name, str) and name else fallback_name
    kind_raw = raw.get("type")
    kind = kind_raw if kind_raw in ("movie", "series", "unknown") else "unknown"
    title_raw = raw.get("title")
    title = title_raw.strip() if isinstance(title_raw, str) else ""
    year = _opt_int(raw.get("year"))
    season = _opt_int(raw.get("season"))
    episode = _opt_int(raw.get("episode"))
    confidence = _clamp_confidence(raw.get("confidence"))
    ext = Path(filename).suffix
    target = suggested_target(kind, title, year, season, episode, ext, movies_root, series_root)
    return Suggestion(filename, kind, title, year, season, episode, confidence, target)


# --- wiring --------------------------------------------------------------------


def _gemini(ctx: RunContext) -> GeminiClient:
    settings = ctx.config.integrations.get("gemini", {})
    ref = settings.get("api_key_ref", "GEMINI_API_KEY")
    model = settings.get("model", "gemini-2.0-flash")
    batch = settings.get("batch_size", 50)
    if not isinstance(ref, str) or not isinstance(model, str):
        raise ConfigError("integrations.gemini needs string 'api_key_ref' and 'model'")
    if isinstance(batch, bool) or not isinstance(batch, int):
        raise ConfigError("integrations.gemini.batch_size must be an integer")
    return GeminiClient(secrets.require(ref), model=model, batch_size=batch)


def _threshold(ctx: RunContext) -> float:
    settings = ctx.config.integrations.get("gemini", {})
    value = settings.get("confidence_threshold", _DEFAULT_THRESHOLD)
    if isinstance(value, bool) or not isinstance(value, int | float):
        return _DEFAULT_THRESHOLD
    return float(value)


def _apply_enabled(ctx: RunContext) -> bool:
    settings = ctx.config.integrations.get("gemini", {})
    value = settings.get("apply", False)
    if not isinstance(value, bool):
        raise ConfigError("integrations.gemini.apply must be a boolean")
    return value


def _cleanup(ctx: RunContext, junk: list[Path], empties: list[Path]) -> int:
    """Quarantine junk files and empty dirs. Returns count moved (or planned)."""
    moved = 0
    for path in junk + empties:
        try:
            ctx.fs.quarantine(path, reason="organizer cleanup")
            moved += 1
        except Exception as exc:
            ctx.logger.warning("organizer cleanup skipped", path=str(path), error=str(exc))
    return moved


def _applicable(suggestions: list[Suggestion], threshold: float) -> list[Suggestion]:
    """Suggestions that clear the confidence gate AND resolved to a target."""
    return [s for s in suggestions if s.confidence >= threshold and s.target]


def _apply(
    ctx: RunContext, suggestions: list[Suggestion], threshold: float, by_name: dict[str, Path]
) -> list[dict[str, object]]:
    """Relocate every applicable suggestion into its canonical path.

    Returns one move record per relocate (planned in DRY_RUN, executed in LIVE).
    A per-item SafetyError (collision) or ValidationError (missing/identical src)
    is logged and skipped without aborting the rest (mirrors ``_cleanup``).
    """
    moves: list[dict[str, object]] = []
    for suggestion in _applicable(suggestions, threshold):
        src = by_name.get(suggestion.filename)
        if src is None or suggestion.target is None:  # pragma: no cover - defensive
            continue
        dest = Path(suggestion.target)
        try:
            ctx.fs.relocate(src, dest, reason="organizer apply")
            moves.append({"src": str(src), "dest": str(dest), "dry_run": ctx.fs.dry_run})
        except (SafetyError, ValidationError) as exc:
            ctx.logger.warning(
                "organizer apply skipped", src=str(src), dest=str(dest), error=str(exc)
            )
    return moves


def _write_report(
    ctx: RunContext,
    suggestions: list[Suggestion],
    threshold: float,
    cleaned: int,
    applied: list[dict[str, object]],
) -> None:
    out_dir = ctx.config.reporting.dir / "organizer"
    out_dir.mkdir(parents=True, exist_ok=True)
    confident = [s for s in suggestions if s.confidence >= threshold and s.target]
    review = [s for s in suggestions if s not in confident]
    (out_dir / "plan.json").write_text(
        json.dumps(
            {
                "threshold": threshold,
                "cleaned_to_quarantine": cleaned,
                "applied": applied,
                "confident": [asdict(s) for s in confident],
                "needs_review": [asdict(s) for s in review],
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    header = (
        "# Organizer plan (apply enabled)"
        if applied
        else "# Organizer plan (report-only — no media moved)"
    )
    lines = [
        header,
        "",
        f"Cleaned to quarantine: {cleaned}",
        f"Confidence threshold: {threshold:.0f}%",
        "",
        f"## Applied ({len(applied)})",
        *[f"- {m['src']} -> {m['dest']}" + (" (dry-run)" if m["dry_run"] else "") for m in applied],
        "",
        f"## Confident ({len(confident)})",
        *[f"- [{s.confidence:.0f}%] {s.filename} -> {s.target}" for s in confident],
        "",
        f"## Needs review ({len(review)})",
        *[f"- [{s.confidence:.0f}%] {s.filename} ({s.kind})" for s in review],
        "",
    ]
    (out_dir / "plan.md").write_text("\n".join(lines), encoding="utf-8")


@register("organizer")
def run(ctx: RunContext) -> ModuleResult:
    result = ModuleResult(module="organizer", run_id=ctx.run_id, mode=ctx.mode)
    paths = ctx.config.paths
    source = Path(paths.get("organizer_source", ""))
    if not paths.get("organizer_source") or not source.is_dir():
        result.add_failure(
            FailureRecord(category="config", message=f"organizer_source not a dir: {source}")
        )
        return result
    movies_root = paths.get("movies_root", "Movies")
    series_root = paths.get("series_root", "Series")

    junk, media = scan(source)
    cleaned = _cleanup(ctx, junk, empty_dirs(source))
    result.quarantined = cleaned

    suggestions: list[Suggestion] = []
    if media:
        try:
            raw = _gemini(ctx).identify([p.name for p in media])
            for path, entry in zip(media, raw, strict=False):
                suggestions.append(normalize_suggestion(entry, path.name, movies_root, series_root))
        except (ConfigError, SecretError) as exc:
            result.add_failure(FailureRecord(category="config", message=str(exc)))
        except IntegrationError as exc:
            result.add_failure(FailureRecord(category="integration", message=str(exc)))

    threshold = _threshold(ctx)
    confident = sum(1 for s in suggestions if s.confidence >= threshold and s.target)

    applied: list[dict[str, object]] = []
    try:
        if _apply_enabled(ctx):
            by_name = {p.name: p for p in media}
            applied = _apply(ctx, suggestions, threshold, by_name)
    except ConfigError as exc:
        result.add_failure(FailureRecord(category="config", message=str(exc)))

    relocated = len(applied)
    result.metrics["relocated"] = float(relocated)
    _write_report(ctx, suggestions, threshold, cleaned, applied)
    ctx.logger.info(
        "organizer done",
        cleaned=cleaned,
        media=len(media),
        suggestions=len(suggestions),
        confident=confident,
        relocated=relocated,
        dry_run=ctx.fs.dry_run,
    )
    result.actions = cleaned + confident + relocated
    return result
