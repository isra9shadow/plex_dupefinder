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
import unicodedata
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol

from adapters import ffprobe
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

_HINTS_SEP = "   |HINTS| "


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
_SE_RE = re.compile(r"[SsTt](\d{1,2})[Ee](\d{1,3})|(?<!\d)(\d{1,2})x(\d{2,3})(?!\d)")
_YEAR_RE = re.compile(r"\((\d{4})\)")
# Spanish releases: "Temporada N ... Cap/Capitulo NNN"
_TEMPORADA_RE = re.compile(r"(?i)temporada\s*(\d{1,2})")
_CAP_RE = re.compile(r"(?i)cap(?:[ií]tulo)?\.?\s*0*(\d{1,4})")
# Year as a prefix ("2022 - Title") or embedded bare ("..._2012_...").
_YEAR_PREFIX_RE = re.compile(r"^\s*((?:19|20)\d{2})\s*[-_.\s]\s*(.+)$")
_BARE_YEAR_RE = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")
_DATE_RE = re.compile(r"(?:19|20)\d{2}[-_.]\d{2}[-_.]\d{2}")  # a timestamp, not a film year
_EDITION_TAIL_RE = re.compile(
    r"(?i)\s*[-_.]\s*(NEW|VOSE|VOSI|VO|MULTI|DUAL|SUB|SUBS|REMUX|HD|MUDA|MUDO|SONORA)\b.*$"
)
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
    episode_title: str | None = None
    tmdb_id: int | None = None
    tvdb_id: int | None = None
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


def _ffprobe_hint(path: Path) -> str:
    """Compact identification clue from the file itself (duration / embedded
    tags / audio languages), or '' if ffprobe is unavailable. Helps the AI on
    cryptically-named files."""
    try:
        meta = ffprobe.probe_tags(path)
    except Exception:  # probing must never break identification
        return ""
    bits: list[str] = []
    if meta.duration_seconds > 0:
        bits.append(f"dur={round(meta.duration_seconds / 60)}min")
    for key in ("title", "show", "season_number", "episode_sort", "episode_id", "date"):
        value = meta.tags.get(key)
        if value:
            bits.append(f"{key}={value}")
    if meta.audio_languages:
        bits.append("audio=" + ",".join(meta.audio_languages[:4]))
    return "; ".join(bits)


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


def _deaccent(text: str) -> str:
    """Strip accents/diacritics (á->a, ñ->n) so filenames stay ASCII-clean."""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def _safe_name(text: str) -> str:
    """Deaccented, path-separator-free name fragment, trimmed."""
    return _deaccent(text).replace("/", "-").replace("\\", "-").strip(" .-_")


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
    episode_title: str | None = None,
    tmdb_id: int | None = None,
    tvdb_id: int | None = None,
    video_format: str | None = None,
    video_codec: str | None = None,
) -> str | None:
    """Build the canonical Radarr/Sonarr-style destination, or None when we lack
    the essentials. Names are ASCII (no accents/ñ); missing quality/id/episode
    tags are dropped cleanly (no ``None`` / empty parens).

    Movie : <movies_root>/{n} ({y})/{n} ({y}) ({vf}-{vc}) (tmdbid-{id}){ext}
    Series: <series_root>/{n} ({y})/Season {ss}/
                {n} ({y}) - S{ss}E{ee} - {episode} ({vf}-{vc}) (tvdbid-{id}){ext}
    """
    title = _safe_name(title)
    if not title:
        return None
    quality = _quality_suffix(video_format, video_codec)
    if kind == "movie":
        base = f"{title} ({year})" if year else title
        ids = f" (tmdbid-{tmdb_id})" if tmdb_id else ""
        return str(Path(movies_root) / base / f"{base}{quality}{ids}{ext}")
    if kind == "series" and season is not None and episode is not None:
        base = f"{title} ({year})" if year else title
        tag = f"S{season:02d}E{episode:02d}"
        et = _safe_name(episode_title) if episode_title else ""
        et_part = f" - {et}" if et else ""
        ids = f" (tvdbid-{tvdb_id})" if tvdb_id else ""
        fname = f"{base} - {tag}{et_part}{quality}{ids}{ext}"
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
    title = _deaccent(title_raw.strip()) if isinstance(title_raw, str) else ""
    year = _opt_int(raw.get("year"))
    season = _opt_int(raw.get("season"))
    episode = _opt_int(raw.get("episode"))
    confidence = _clamp_confidence(raw.get("confidence"))
    ep_title_raw = raw.get("episode_title")
    episode_title = _deaccent(ep_title_raw.strip()) if isinstance(ep_title_raw, str) else None
    tmdb_id = _opt_int(raw.get("tmdb_id"))
    tvdb_id = _opt_int(raw.get("tvdb_id"))
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
        episode_title=episode_title,
        tmdb_id=tmdb_id,
        tvdb_id=tvdb_id,
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
        episode_title=episode_title or None,
        tmdb_id=tmdb_id,
        tvdb_id=tvdb_id,
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


def _movie_entry(
    rel_path: str, title: str, year: int, vf: str | None, vc: str | None, confidence: int
) -> dict[str, object]:
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
        "confidence": confidence,
    }


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
    norm_stem = re.sub(r"[._]+", " ", stem)  # underscores/dots -> spaces for matching
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
        # Episode title: text after SxxEyy, before the [..]/(..) tags, minus a
        # leading absolute-episode number (e.g. " - 492 - Title").
        after = re.split(r"[\[(]", stem[se.end() :], maxsplit=1)[0]
        after = re.sub(r"^[\s\-_.]*(?:\d{1,4}[\s\-_.]+)?", "", after)
        episode_title = _clean_title(after) or None
        return {
            "filename": rel_path,
            "type": "series",
            "title": title,
            "year": year,
            "season": season,
            "episode": episode,
            "episode_title": episode_title,
            "video_format": vf,
            "video_codec": vc,
            "tvdb_id": None,
            "confidence": 95,
        }
    # Spanish releases: "<title> Temporada N ... Cap/Capitulo NNN".
    temp_m = _TEMPORADA_RE.search(norm_stem)
    cap_m = _CAP_RE.search(norm_stem)
    if temp_m and cap_m:
        title = _clean_title(norm_stem[: temp_m.start()])
        if not title and len(parts) >= 2:
            title = _clean_title(_YEAR_RE.sub("", parts[0]))
        if title:
            return {
                "filename": rel_path,
                "type": "series",
                "title": title,
                "year": year,
                "season": int(temp_m.group(1)),
                "episode": int(cap_m.group(1)),
                "episode_title": None,
                "video_format": vf,
                "video_codec": vc,
                "tvdb_id": None,
                "confidence": 90,
            }

    if year and year_m:
        # If there is descriptive text AFTER the year (e.g. "- EP. 01 - ...",
        # "- Documentary", "- Part 4"), the simple "title before year" would
        # collapse distinct files onto the same name (collisions). Defer those
        # to the AI, which can disambiguate them properly.
        tail = re.split(r"[\[(]", stem[year_m.end() :], maxsplit=1)[0]
        if re.search(r"[A-Za-z0-9]", tail):
            return None
        title = _clean_title(stem[: year_m.start()])
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
    # Year as a prefix or embedded bare (no parentheses). Skip timestamp-named
    # files (e.g. home videos "video_2026-06-17_...") via the date guard.
    if not _DATE_RE.search(norm_stem):
        ypre = _YEAR_PREFIX_RE.match(norm_stem)
        if ypre:
            rest = re.split(r"[\[(]", ypre.group(2), maxsplit=1)[0]
            title = _clean_title(_EDITION_TAIL_RE.sub("", rest))
            if title and re.search(r"[A-Za-z]", title):
                return _movie_entry(rel_path, title, int(ypre.group(1)), vf, vc, 88)
        bare = _BARE_YEAR_RE.search(norm_stem)
        if bare:
            title = _clean_title(norm_stem[: bare.start()])
            if title and len(title) >= 3 and re.search(r"[A-Za-z]", title):
                return _movie_entry(rel_path, title, int(bare.group(1)), vf, vc, 85)
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


def _escalate_below(ctx: RunContext) -> float:
    """Confidence floor below which an AI answer is kept but NOT accepted as final,
    so the file is ALSO sent to the next provider for a second opinion (e.g. a
    low-confidence local Ollama guess gets verified by Gemini). We keep whichever
    answer wins. Default 0 = accept any answer (no escalation, back-compat)."""
    value = ctx.config.integrations.get("ai", {}).get("escalate_below", 0)
    if isinstance(value, bool) or not isinstance(value, int | float):
        return 0.0
    return float(value)


def _conf(entry: dict[str, object]) -> float:
    """Read an entry's self-reported confidence as a float (0 when absent/bad)."""
    value = entry.get("confidence", 0)
    if isinstance(value, bool) or not isinstance(value, int | float):
        return 0.0
    return float(value)


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
        # Enrich each unresolved file with ffprobe clues (duration/tags/langs) so
        # the model can identify cryptically-named files; echo-map back to the rel.
        input_to_rel: dict[str, str] = {}
        ai_inputs: list[str] = []
        for rel in unresolved:
            hint = _ffprobe_hint(by_rel[rel])
            line = f"{rel}{_HINTS_SEP}{hint}" if hint else rel
            ai_inputs.append(line)
            input_to_rel[line] = rel
            input_to_rel[rel] = rel  # in case the model echoes the bare path
        # Keep the BEST (highest-confidence) answer per file across providers. An
        # answer below `escalate_below` is kept but NOT accepted as final, so the
        # file also flows to the next provider (low-conf Ollama -> Gemini); the
        # higher-confidence answer wins. escalate_below=0 -> accept any (no dup).
        best: dict[str, dict[str, object]] = {}
        escalate_below = _escalate_below(ctx)
        for name in _ai_providers(ctx):
            if not ai_inputs:
                break
            try:
                client = _build_ai_client(ctx, name)
                errors: list[IntegrationError] = []
                got = client.identify(ai_inputs, errors=errors)
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
            accepted: set[str] = set()
            for entry in got:
                echoed = entry.get("filename")
                mapped: str | None = None
                if isinstance(echoed, str):
                    mapped = input_to_rel.get(echoed) or input_to_rel.get(
                        echoed.split(_HINTS_SEP, 1)[0].strip()
                    )
                if mapped is None:
                    continue
                entry["filename"] = mapped  # normalise to the real relative path
                if mapped not in best or _conf(entry) > _conf(best[mapped]):
                    best[mapped] = entry
                if _conf(entry) >= escalate_below:
                    accepted.add(mapped)
            ai_inputs = [ln for ln in ai_inputs if input_to_rel[ln] not in accepted]
        raw.extend(best.values())
        unresolved = [input_to_rel[ln] for ln in ai_inputs if input_to_rel[ln] not in best]

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
