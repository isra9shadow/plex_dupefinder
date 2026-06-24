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
import re
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol

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
from integrations.ollama import OllamaClient


class _Identifier(Protocol):
    """Common surface of the AI backends (Gemini / Ollama) the cascade uses."""

    def identify(
        self, paths: Sequence[str], *, errors: list[IntegrationError] | None = None
    ) -> list[dict[str, object]]: ...


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

# --- local heuristic parser (no AI / no quota) ---------------------------------
# Most files already carry all the info in their name/path (Sonarr/Radarr style),
# so a deterministic parser identifies them for free; the AI is only a fallback
# for what these patterns cannot resolve.
_SE_RE = re.compile(r"[Ss](\d{1,2})[Ee](\d{1,3})|(?<!\d)(\d{1,2})x(\d{2,3})(?!\d)")
_YEAR_RE = re.compile(r"\((\d{4})\)")
_SOURCE_RE = re.compile(
    r"(?i)\b(remux|blu-?ray|bdrip|brrip|web-?dl|webrip|hdtv|pdtv|dvdrip|dvd|hdrip|sdtv)\b"
)
_RES_RE = re.compile(r"(?i)\b(2160p|1080p|1080i|720p|576p|480p|4k)\b")
_CODEC_RE = re.compile(r"(?i)\b(x265|h\.?265|hevc|x264|h\.?264|avc|av1|xvid|vp9)\b")
_SOURCE_NORM = {
    "remux": "Remux",
    "bluray": "Bluray",
    "bdrip": "Bluray",
    "brrip": "Bluray",
    "webdl": "WEBDL",
    "webrip": "WEBRip",
    "hdrip": "WEBRip",
    "hdtv": "HDTV",
    "pdtv": "HDTV",
    "dvdrip": "DVD",
    "dvd": "DVD",
    "sdtv": "SDTV",
}
_CODEC_NORM = {
    "x265": "x265",
    "h265": "x265",
    "hevc": "x265",
    "x264": "x264",
    "h264": "x264",
    "avc": "x264",
    "av1": "AV1",
    "xvid": "XviD",
    "vp9": "VP9",
}


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
    tmdb_id: int | None = None
    video_format: str | None = None
    video_codec: str | None = None


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


def _safe_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:  # pragma: no cover - defensive
        return 0


def _under_any(path: Path, roots: list[Path]) -> bool:
    """True if ``path`` resolves inside any of ``roots`` (output-dir exclusion)."""
    try:
        resolved = path.resolve()
    except OSError:  # pragma: no cover - defensive
        return False
    for root in roots:
        try:
            resolved.relative_to(root)
            return True
        except ValueError:
            continue
    return False


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


def _clean_token(value: object) -> str | None:
    """A safe, non-empty filename token (no path separators), else None."""
    if not isinstance(value, str):
        return None
    token = value.strip().replace("/", "-").replace("\\", "-")
    return token or None


def _quality_suffix(video_format: str | None, video_codec: str | None) -> str:
    """``(Bluray-1080p-x265)`` style tag, dropping whichever part is missing."""
    parts = [p for p in (video_format, video_codec) if p]
    return f" ({'-'.join(parts)})" if parts else ""


def _id_suffix(tmdb_id: int | None) -> str:
    return f" (tmdbid-{tmdb_id})" if tmdb_id else ""


def suggested_target(
    kind: str,
    title: str,
    year: int | None,
    season: int | None,
    episode: int | None,
    ext: str,
    movies_root: str,
    series_root: str,
    *,
    tmdb_id: int | None = None,
    video_format: str | None = None,
    video_codec: str | None = None,
) -> str | None:
    """Build the canonical Radarr/Sonarr-style destination, or None when we lack
    the essentials. Missing quality/id tags are dropped cleanly (no ``None``).

    Movie : <movies_root>/{n} ({y})/{n} ({y}) ({vf}-{vc}) (tmdbid-{id}){ext}
    Series: <series_root>/{n} ({y})/Season {ss}/
                {n} ({y}) - S{ss}E{ee} ({vf}-{vc}) (tmdbid-{id}){ext}
    """
    if not title:
        return None
    quality = _quality_suffix(video_format, video_codec)
    ids = _id_suffix(tmdb_id)
    if kind == "movie":
        base = f"{title} ({year})" if year else title
        fname = f"{base}{quality}{ids}{ext}"
        return str(Path(movies_root) / base / fname)
    if kind == "series" and season is not None and episode is not None:
        base = f"{title} ({year})" if year else title
        tag = f"S{season:02d}E{episode:02d}"
        fname = f"{base} - {tag}{quality}{ids}{ext}"
        return str(Path(series_root) / base / f"Season {season:02d}" / fname)
    return None


def normalize_suggestion(
    raw: dict[str, object], fallback_name: str, movies_root: str, series_root: str
) -> Suggestion:
    """Validate one raw model entry into a typed, path-resolved Suggestion."""
    name = raw.get("filename")
    filename = name if isinstance(name, str) and name else fallback_name
    kind_raw = raw.get("type")
    kind = str(kind_raw) if kind_raw in ("movie", "series", "unknown") else "unknown"
    title_raw = raw.get("title")
    title = title_raw.strip() if isinstance(title_raw, str) else ""
    year = _opt_int(raw.get("year"))
    season = _opt_int(raw.get("season"))
    episode = _opt_int(raw.get("episode"))
    confidence = _clamp_confidence(raw.get("confidence"))
    tmdb_id = _opt_int(raw.get("tmdb_id"))
    video_format = _clean_token(raw.get("video_format"))
    video_codec = _clean_token(raw.get("video_codec"))
    ext = Path(filename).suffix
    target = suggested_target(
        kind,
        title,
        year,
        season,
        episode,
        ext,
        movies_root,
        series_root,
        tmdb_id=tmdb_id,
        video_format=video_format,
        video_codec=video_codec,
    )
    return Suggestion(
        filename,
        kind,
        title,
        year,
        season,
        episode,
        confidence,
        target,
        tmdb_id=tmdb_id,
        video_format=video_format,
        video_codec=video_codec,
    )


def _clean_title(text: str) -> str:
    """Tidy a raw title fragment: dots/underscores -> spaces, trim separators."""
    out = re.sub(r"[._]+", " ", text)
    return re.sub(r"\s{2,}", " ", out).strip(" -._")


def _parse_quality(path: str) -> tuple[str | None, str | None]:
    """Best-effort (video_format, video_codec) from the name/path tags."""
    src_m = _SOURCE_RE.search(path)
    res_m = _RES_RE.search(path)
    cod_m = _CODEC_RE.search(path)
    source = _SOURCE_NORM.get(re.sub(r"[^a-z0-9]", "", src_m.group(1).lower())) if src_m else None
    res = res_m.group(1).lower() if res_m else None
    if res == "4k":
        res = "2160p"
    if source and res:
        vf: str | None = f"{source}-{res}"
    else:
        vf = source or res
    vc = _CODEC_NORM.get(re.sub(r"[^a-z0-9]", "", cod_m.group(1).lower())) if cod_m else None
    return vf, vc


def parse_media_filename(rel_path: str) -> dict[str, object] | None:
    """Identify a media file from its relative path WITHOUT calling the AI.

    Returns a raw entry shaped like a Gemini suggestion (so it flows through
    ``normalize_suggestion`` unchanged), or None when the patterns cannot resolve
    it confidently — those are left for the AI fallback. ``tmdb_id`` is always
    None here (the filename does not carry it; *arr import or the AI can add it).
    """
    norm = rel_path.replace("\\", "/")
    parts = [p for p in norm.split("/") if p]
    stem = parts[-1].rsplit(".", 1)[0] if parts else norm
    vf, vc = _parse_quality(norm)
    year_m = _YEAR_RE.search(stem)
    year = int(year_m.group(1)) if year_m else None
    se = _SE_RE.search(stem)
    if se:
        season = int(se.group(1) or se.group(3))
        episode = int(se.group(2) or se.group(4))
        title = _clean_title(_YEAR_RE.sub("", stem[: se.start()]))
        if not title and len(parts) >= 2:  # fall back to the show folder name
            title = _clean_title(_YEAR_RE.sub("", parts[0]))
        if not title:
            return None
        return {
            "filename": rel_path,
            "type": "series",
            "title": title,
            "year": year,
            "season": season,
            "episode": episode,
            "video_format": vf,
            "video_codec": vc,
            "tmdb_id": None,
            "confidence": 95,
        }
    if year:
        title = _clean_title(stem[: year_m.start()]) if year_m else ""
        if not title and len(parts) >= 2:
            title = _clean_title(_YEAR_RE.sub("", parts[-2]))
        if not title:
            return None
        return {
            "filename": rel_path,
            "type": "movie",
            "title": title,
            "year": year,
            "season": None,
            "episode": None,
            "video_format": vf,
            "video_codec": vc,
            "tmdb_id": None,
            "confidence": 90,
        }
    return None  # ambiguous -> let the AI try


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
    delay = settings.get("request_delay", 0)
    if isinstance(delay, bool) or not isinstance(delay, int | float):
        raise ConfigError("integrations.gemini.request_delay must be a number")
    return GeminiClient(
        secrets.require(ref), model=model, batch_size=batch, request_delay=float(delay)
    )


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


def _ai_fallback_enabled(ctx: RunContext) -> bool:
    """Master switch for using ANY AI backend on files the parser can't resolve.
    Default True; set integrations.gemini.ai_fallback=false to stay 100% local
    (parser only, zero AI calls)."""
    value = ctx.config.integrations.get("gemini", {}).get("ai_fallback", True)
    return value if isinstance(value, bool) else True


def _ai_providers(ctx: RunContext) -> list[str]:
    """Ordered AI backends to try for unresolved files. Default ['gemini'] for
    back-compat; recommended ['ollama', 'gemini'] = local 4060 first (free,
    unlimited), Gemini only as a last resort (quota-limited)."""
    value = ctx.config.integrations.get("ai", {}).get("providers", ["gemini"])
    if isinstance(value, list):
        return [p for p in value if isinstance(p, str)]
    return ["gemini"]


def _ollama(ctx: RunContext) -> OllamaClient:
    settings = ctx.config.integrations.get("ollama", {})
    base = settings.get("base_url", "http://localhost:11434")
    model = settings.get("model", "qwen3:8b")
    if not isinstance(base, str) or not isinstance(model, str):
        raise ConfigError("integrations.ollama needs string 'base_url' and 'model'")
    batch = settings.get("batch_size", 50)
    if isinstance(batch, bool) or not isinstance(batch, int):
        raise ConfigError("integrations.ollama.batch_size must be an integer")
    delay = settings.get("request_delay", 0)
    if isinstance(delay, bool) or not isinstance(delay, int | float):
        raise ConfigError("integrations.ollama.request_delay must be a number")
    return OllamaClient(base_url=base, model=model, batch_size=batch, request_delay=float(delay))


def _build_ai_client(ctx: RunContext, name: str) -> _Identifier:
    if name == "ollama":
        return _ollama(ctx)
    if name == "gemini":
        return _gemini(ctx)
    raise ConfigError(f"unknown AI provider: {name!r} (use 'ollama' or 'gemini')")


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
    ctx: RunContext,
    suggestions: list[Suggestion],
    threshold: float,
    by_name: dict[str, Path],
    allowed_roots: list[Path],
) -> list[dict[str, object]]:
    """Relocate every applicable suggestion into its canonical path.

    Returns one move record per relocate (planned in DRY_RUN, executed in LIVE).
    A per-item SafetyError (collision, or a dest outside ``allowed_roots`` — a
    hallucinated target) or ValidationError (missing/identical src) is logged and
    skipped without aborting the rest (mirrors ``_cleanup``).
    """
    moves: list[dict[str, object]] = []
    for suggestion in _applicable(suggestions, threshold):
        src = by_name.get(suggestion.filename)
        if src is None or suggestion.target is None:  # pragma: no cover - defensive
            continue
        dest = Path(suggestion.target)
        try:
            ctx.fs.relocate(src, dest, reason="organizer apply", allowed_roots=allowed_roots)
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
    # Never re-process our own output: skip files already under the Radarr/Sonarr
    # destination roots (which may live inside the source dump).
    out_roots = [Path(movies_root).resolve(), Path(series_root).resolve()]
    media = [p for p in media if not _under_any(p, out_roots)]
    # Largest files first: organise/free the big ones sooner (space optimisation).
    media.sort(key=_safe_size, reverse=True)
    cleaned = _cleanup(ctx, junk, empty_dirs(source))
    result.quarantined = cleaned

    # Identify by RELATIVE path (folders + filename) so the model can use folder
    # names as title hints; map results back by the echoed path, not by position
    # (a skipped/failed batch must not misalign the rest).
    by_rel = {p.relative_to(source).as_posix(): p for p in media}
    # Local parser first (free, no quota); AI only for what it cannot resolve.
    raw: list[dict[str, object]] = []
    unresolved: list[str] = []
    for rel in by_rel:
        entry = parse_media_filename(rel)
        if entry is not None:
            raw.append(entry)
        else:
            unresolved.append(rel)

    if unresolved and _ai_fallback_enabled(ctx):
        for name in _ai_providers(ctx):
            if not unresolved:
                break
            try:
                client = _build_ai_client(ctx, name)
                errors: list[IntegrationError] = []
                got = client.identify(unresolved, errors=errors)
            except (ConfigError, SecretError) as exc:
                result.add_failure(FailureRecord(category="config", message=f"{name}: {exc}"))
                continue
            except IntegrationError as exc:
                result.add_failure(FailureRecord(category="integration", message=f"{name}: {exc}"))
                continue
            for batch_exc in errors:
                result.add_failure(
                    FailureRecord(category="integration", message=f"{name}: {batch_exc}")
                )
            raw.extend(got)
            resolved = {e.get("filename") for e in got if isinstance(e.get("filename"), str)}
            unresolved = [r for r in unresolved if r not in resolved]

    # Anything still unresolved -> surface as 'unknown' so it lands in needs_review.
    for rel in unresolved:
        raw.append({"filename": rel, "type": "unknown", "confidence": 0})

    suggestions: list[Suggestion] = []
    for entry in raw:
        echoed = entry.get("filename")
        fallback = echoed if isinstance(echoed, str) and echoed else ""
        suggestions.append(normalize_suggestion(entry, fallback, movies_root, series_root))

    threshold = _threshold(ctx)
    confident = sum(1 for s in suggestions if s.confidence >= threshold and s.target)

    applied: list[dict[str, object]] = []
    try:
        if _apply_enabled(ctx):
            allowed_roots = [Path(movies_root), Path(series_root)]
            applied = _apply(ctx, suggestions, threshold, by_rel, allowed_roots)
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
