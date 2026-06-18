"""Pure, typed port of the legacy ``get_score`` (CTO-12 / MIGRATION_PLAN §9).

Same arithmetic as ``plex_dupefinder.get_score``, but with NO global ``cfg``: all
weights are passed explicitly via ``ScoreWeights``. Locked to the legacy by
``tests/regression/test_dedupe_scoring_parity.py`` so the eventual migration is
verifiable, not a leap of faith.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from fnmatch import fnmatch

# (SOURCE_SCORES key, exact word tokens, multi-word substrings) — mirrors legacy.
_SOURCE_DETECTORS = (
    ("remux", ("remux", "bdremux", "brremux"), ()),
    ("bluray", ("bluray", "bdrip", "brrip"), ("blu ray",)),
    ("web-dl", ("webdl",), ("web dl",)),
    ("webrip", ("webrip",), ("web rip",)),
    ("hdtv", ("hdtv", "pdtv", "hdrip", "dsr"), ()),
    ("dvd", ("dvdrip", "dvd"), ()),
    ("cam", ("cam", "hdcam", "telesync", "telecine", "hdts"), ()),
)


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _int(value: object) -> int:
    return int(value) if isinstance(value, int | float) and not isinstance(value, bool) else 0


def _files(value: object) -> list[str]:
    return [str(f) for f in value] if isinstance(value, list) else []


def _scores(raw: object) -> dict[str, int]:
    if not isinstance(raw, dict):
        return {}
    return {str(k): _int(v) for k, v in raw.items()}


@dataclass(frozen=True, slots=True)
class ScoreWeights:
    """All tunables ``get_score`` reads from config, resolved once."""

    audio_codec: Mapping[str, int]
    video_codec: Mapping[str, int]
    video_resolution: Mapping[str, int]
    source: Mapping[str, int]
    filename: Mapping[str, int]
    filename_cap: int
    bitrate_weight: float
    hdr: int
    dolby_vision: int
    subtitle_per_track: int
    audio_track: int
    score_filesize: bool

    @classmethod
    def from_config(cls, cfg: Mapping[str, object]) -> ScoreWeights:
        def num(key: str, default: float) -> float:
            v = cfg.get(key, default)
            return float(v) if isinstance(v, int | float) and not isinstance(v, bool) else default

        return cls(
            audio_codec=_scores(cfg.get("AUDIO_CODEC_SCORES")),
            video_codec=_scores(cfg.get("VIDEO_CODEC_SCORES")),
            video_resolution=_scores(cfg.get("VIDEO_RESOLUTION_SCORES")),
            source=_scores(cfg.get("SOURCE_SCORES")),
            filename=_scores(cfg.get("FILENAME_SCORES")),
            filename_cap=int(num("FILENAME_SCORE_CAP", 0)),
            bitrate_weight=num("BITRATE_SCORE_WEIGHT", 0.1),
            hdr=int(num("HDR_SCORE", 0)),
            dolby_vision=int(num("DOLBY_VISION_SCORE", 0)),
            subtitle_per_track=int(num("SUBTITLE_SCORE_PER_TRACK", 0)),
            audio_track=int(num("AUDIO_TRACK_SCORE", 0)),
            score_filesize=bool(cfg.get("SCORE_FILESIZE", False)),
        )


def _source_score(files: list[str], source_scores: Mapping[str, int]) -> tuple[int, str | None]:
    text = " ".join(os.path.basename(f).lower() for f in files)
    norm = re.sub(r"[._\-]+", " ", text)
    tokens = set(norm.split())
    for key, words, phrases in _SOURCE_DETECTORS:
        if any(w in tokens for w in words) or any(p in norm for p in phrases):
            return int(source_scores.get(key, 0)), key
    return 0, None


def score(media_info: Mapping[str, object], weights: ScoreWeights) -> tuple[int, dict[str, object]]:
    """Return ``(total_score, breakdown)`` identical to the legacy ``get_score``."""
    breakdown: dict[str, object] = {}

    audio_codec_pts = weights.audio_codec.get(_text(media_info.get("audio_codec")).lower(), 0)
    breakdown["audio_codec"] = audio_codec_pts
    video_codec_pts = weights.video_codec.get(_text(media_info.get("video_codec")).lower(), 0)
    breakdown["video_codec"] = video_codec_pts
    resolution_pts = weights.video_resolution.get(
        _text(media_info.get("video_resolution")).lower(), 0
    )
    breakdown["resolution"] = resolution_pts

    files = _files(media_info.get("file"))
    source_pts, source_key = _source_score(files, weights.source)
    breakdown["source"] = source_pts
    if source_key:
        breakdown["source_type"] = source_key

    filename_pts = 0
    filename_matches: list[dict[str, object]] = []
    basenames = [os.path.basename(f.lower()) for f in files]
    for pattern, kw_score in weights.filename.items():
        kw = pattern.lower()
        for base in basenames:
            if fnmatch(base, kw):
                filename_pts += int(kw_score)
                filename_matches.append({"pattern": pattern, "score": int(kw_score)})
    if weights.filename_cap > 0 and filename_pts > weights.filename_cap:
        filename_pts = weights.filename_cap
    breakdown["filename"] = filename_pts
    if filename_matches:
        breakdown["filename_matches"] = filename_matches

    bitrate_pts = int(_int(media_info.get("video_bitrate")) * weights.bitrate_weight)
    breakdown["bitrate"] = bitrate_pts
    duration_pts = int(_int(media_info.get("video_duration")) / 300)
    breakdown["duration"] = duration_pts
    dimensions_pts = (
        _int(media_info.get("video_width")) * 2 + _int(media_info.get("video_height")) * 2
    )
    breakdown["dimensions"] = dimensions_pts
    audio_channel_pts = _int(media_info.get("audio_channels")) * 1000
    breakdown["audio_channels"] = audio_channel_pts

    total = (
        audio_codec_pts
        + video_codec_pts
        + resolution_pts
        + source_pts
        + filename_pts
        + bitrate_pts
        + duration_pts
        + dimensions_pts
        + audio_channel_pts
    )

    if media_info.get("has_hdr"):
        breakdown["hdr"] = weights.hdr
        total += weights.hdr
    if media_info.get("has_dv"):
        breakdown["dolby_vision"] = weights.dolby_vision
        total += weights.dolby_vision

    subtitle_pts = _int(media_info.get("subtitle_count")) * weights.subtitle_per_track
    if subtitle_pts:
        breakdown["subtitle_tracks"] = subtitle_pts
        total += subtitle_pts
    audio_track_pts = _int(media_info.get("audio_track_count")) * weights.audio_track
    if audio_track_pts:
        breakdown["audio_tracks"] = audio_track_pts
        total += audio_track_pts

    if weights.score_filesize:
        size_pts = int(_int(media_info.get("file_size")) / 100000)
        breakdown["file_size"] = size_pts
        total += size_pts

    return int(total), breakdown
