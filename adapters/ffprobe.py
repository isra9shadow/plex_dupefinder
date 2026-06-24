"""Read-only media probing via ffprobe/ffmpeg (through the command adapter)."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from adapters import command
from adapters.command import CommandResult

Runner = Callable[[Sequence[str]], CommandResult]
_SMOKE_SECONDS = "10"


@dataclass(frozen=True)
class MediaProbe:
    has_video: bool
    has_audio: bool
    duration_seconds: float
    decodes_ok: bool


@dataclass(frozen=True)
class MediaTags:
    """Identification hints read from the container (best-effort)."""

    duration_seconds: float
    tags: dict[str, str]  # format-level tags (title/show/season_number/date...)
    audio_languages: list[str]


def _as_dict(value: object) -> dict[str, object]:
    return {str(k): v for k, v in value.items()} if isinstance(value, dict) else {}


def _streams(data: dict[str, object]) -> list[dict[str, object]]:
    raw = data.get("streams")
    return [_as_dict(item) for item in raw] if isinstance(raw, list) else []


def _duration(data: dict[str, object]) -> float:
    value = _as_dict(data.get("format")).get("duration")
    if isinstance(value, int | float) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return 0.0
    return 0.0


def _str_tags(value: object) -> dict[str, str]:
    out: dict[str, str] = {}
    if isinstance(value, dict):
        for k, v in value.items():
            if isinstance(v, str | int | float) and not isinstance(v, bool):
                out[str(k).lower()] = str(v)
    return out


def probe_tags(path: Path, *, runner: Runner = command.run) -> MediaTags:
    """Read duration + embedded container tags + audio languages (best-effort,
    never raises). Used to give the AI extra clues for hard-to-name files."""
    info = runner(
        [
            "ffprobe",
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(path),
        ]
    )
    if not info.ok:
        return MediaTags(0.0, {}, [])
    try:
        data = _as_dict(json.loads(info.stdout))
    except json.JSONDecodeError:
        return MediaTags(0.0, {}, [])
    tags = _str_tags(_as_dict(data.get("format")).get("tags"))
    langs: list[str] = []
    for stream in _streams(data):
        if stream.get("codec_type") == "audio":
            lang = _str_tags(stream.get("tags")).get("language")
            if lang and lang not in langs:
                langs.append(lang)
    return MediaTags(_duration(data), tags, langs)


def probe(path: Path, *, runner: Runner = command.run, smoke: bool = True) -> MediaProbe:
    """Probe a media file. Returns a best-effort MediaProbe; never raises."""
    info = runner(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type",
            "-show_entries",
            "format=duration",
            "-of",
            "json",
            str(path),
        ]
    )
    has_video = has_audio = False
    duration = 0.0
    if info.ok:
        try:
            data = _as_dict(json.loads(info.stdout))
        except json.JSONDecodeError:
            data = {}
        for stream in _streams(data):
            codec = stream.get("codec_type")
            if codec == "video":
                has_video = True
            elif codec == "audio":
                has_audio = True
        duration = _duration(data)
    decodes_ok = True
    if smoke:
        decodes_ok = runner(
            [
                "ffmpeg",
                "-v",
                "error",
                "-xerror",
                "-i",
                str(path),
                "-map",
                "0:v:0?",
                "-an",
                "-sn",
                "-t",
                _SMOKE_SECONDS,
                "-f",
                "null",
                "-",
            ]
        ).ok
    return MediaProbe(has_video, has_audio, duration, decodes_ok)
