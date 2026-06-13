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
