"""Tests for adapters.ffprobe (parsing, injected runner — no ffmpeg)."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

from adapters import ffprobe
from adapters.command import CommandResult


def _runner(info_json: str, *, smoke_ok: bool = True):
    def run(argv: Sequence[str]) -> CommandResult:
        if argv[0] == "ffprobe":
            return CommandResult(tuple(argv), 0, info_json, "")
        return CommandResult(tuple(argv), 0 if smoke_ok else 1, "", "")

    return run


def test_probe_parses_streams_and_duration() -> None:
    info = json.dumps(
        {
            "streams": [{"codec_type": "video"}, {"codec_type": "audio"}],
            "format": {"duration": "3600.5"},
        }
    )
    probe = ffprobe.probe(Path("x.mkv"), runner=_runner(info))
    assert probe.has_video
    assert probe.has_audio
    assert probe.duration_seconds == 3600.5
    assert probe.decodes_ok


def test_probe_smoke_failure() -> None:
    info = json.dumps({"streams": [{"codec_type": "video"}], "format": {"duration": 120}})
    probe = ffprobe.probe(Path("x.mkv"), runner=_runner(info, smoke_ok=False))
    assert not probe.decodes_ok
    assert not probe.has_audio


def test_probe_ffprobe_failure() -> None:
    def run(argv: Sequence[str]) -> CommandResult:
        return CommandResult(tuple(argv), 1, "", "boom")

    probe = ffprobe.probe(Path("x"), runner=run, smoke=False)
    assert not probe.has_video
    assert probe.duration_seconds == 0.0
    assert probe.decodes_ok


def test_probe_tags_reads_tags_and_languages() -> None:
    info = json.dumps(
        {
            "format": {"duration": "1440.0", "tags": {"title": "Akira", "show": "X", "BPS": 1}},
            "streams": [
                {"codec_type": "video"},
                {"codec_type": "audio", "tags": {"language": "jpn"}},
                {"codec_type": "audio", "tags": {"language": "eng"}},
            ],
        }
    )
    mt = ffprobe.probe_tags(Path("x.mkv"), runner=_runner(info))
    assert mt.duration_seconds == 1440.0
    assert mt.tags["title"] == "Akira"
    assert mt.tags["bps"] == "1"  # lower-cased, stringified
    assert mt.audio_languages == ["jpn", "eng"]


def test_probe_tags_failure_is_empty() -> None:
    def run(argv: Sequence[str]) -> CommandResult:
        return CommandResult(tuple(argv), 1, "", "no ffprobe")

    mt = ffprobe.probe_tags(Path("x"), runner=run)
    assert mt.duration_seconds == 0.0
    assert mt.tags == {}
    assert mt.audio_languages == []
