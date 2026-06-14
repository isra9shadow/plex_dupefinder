"""Parity lock: the ported scoring must match the legacy engine EXACTLY (CTO-12).

This is the oracle that makes the eventual native migration safe — if the port
ever drifts from ``plex_dupefinder.get_score``, these fail.

Run with:  pytest tests/regression/test_dedupe_scoring_parity.py -q
"""

from __future__ import annotations

import copy

import plex_dupefinder as pd
import pytest
from modules.media.dedupe.scoring import ScoreWeights, score


def _mi(**over):
    base = {
        "audio_codec": "truehd",
        "video_codec": "hevc",
        "video_resolution": "4k",
        "video_bitrate": 12000,
        "video_duration": 7_200_000,
        "video_width": 3840,
        "video_height": 2160,
        "audio_channels": 8,
        "file": ["/movies/Dune (2021)/Dune.2021.2160p.BluRay.REMUX.HDR.mkv"],
        "has_hdr": True,
        "has_dv": False,
        "subtitle_count": 3,
        "audio_track_count": 2,
        "file_size": 50_000_000_000,
    }
    base.update(over)
    return base


_CASES = [
    _mi(),
    _mi(audio_codec="aac", video_codec="h264", video_resolution="1080", has_hdr=False),
    _mi(file=["/tv/Show/S01E01.HDTV.x264.mkv"], has_hdr=False, has_dv=False),
    _mi(file=["/m/movie.WEB-DL.mkv"], video_resolution="720", audio_channels=2),
    _mi(has_dv=True, file=["/m/movie.bluray.remux.DV.mkv"]),
    _mi(subtitle_count=0, audio_track_count=0, file=["/m/cam.HDCAM.mkv"]),
    _mi(
        video_bitrate=0,
        video_duration=0,
        video_width=0,
        video_height=0,
        audio_channels=0,
        file=["/m/plain.mkv"],
        has_hdr=False,
    ),
    _mi(file=["/m/movie.dvdrip.mkv"], video_codec="UNKNOWN", audio_codec="Unknown"),
]


@pytest.fixture
def weights():
    return ScoreWeights.from_config(pd.cfg)


@pytest.mark.parametrize("media_info", _CASES)
def test_score_matches_legacy(media_info, weights):
    legacy_total, legacy_breakdown = pd.get_score(copy.deepcopy(media_info))
    ported_total, ported_breakdown = score(media_info, weights)
    assert ported_total == legacy_total
    assert ported_breakdown == legacy_breakdown


def test_filesize_toggle_parity(monkeypatch, weights):
    base = copy.deepcopy(pd.cfg)
    base["SCORE_FILESIZE"] = True
    monkeypatch.setattr(pd, "cfg", base)
    w = ScoreWeights.from_config(base)
    mi = _mi()
    assert score(mi, w) == pd.get_score(copy.deepcopy(mi))
    assert "file_size" in score(mi, w)[1]
