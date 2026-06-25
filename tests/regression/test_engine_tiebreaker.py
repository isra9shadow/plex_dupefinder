"""Opt-in score-delta tiebreaker (SCORE_DELTA_TIEBREAKER) in the legacy engine.

Default OFF = a near-tie is skipped (unchanged, parity-locked elsewhere). When
'size' is set, the closest-scored group is decided deterministically: largest
file, then oldest, then lowest media id — instead of being skipped.
"""

from __future__ import annotations

import copy

import plex_dupefinder as pd
import pytest


def _ex(**over):
    base = {"exists": True, "local_check": True, "plex_check": True, "age_hours": 1000.0}
    base.update(over)
    return base


def _part(**over):
    base = {
        "id": 100,
        "score": 5000,
        "exists": True,
        "file": ["/m/x.mkv"],
        "file_size": 10_000_000_000,
        "video_duration": 7_200_000,
        "video_bitrate": 12000,
        "video_codec": "hevc",
        "parts_existence": [_ex()],
    }
    base.update(over)
    return base


def _cfg(**over):
    base = copy.deepcopy(pd.cfg)
    base["MIN_SCORE_DIFFERENCE"] = 100
    base["MIN_FILE_AGE_HOURS"] = 0
    base["MAX_SIZE_RATIO"] = 0
    base.update(over)
    return base


def test_default_off_still_skips_near_tie(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pd, "cfg", _cfg())  # tiebreaker '' by default
    parts = {
        1: _part(id=1, score=5000, file_size=10_000_000_000),
        2: _part(id=2, score=4999, file_size=12_000_000_000),
    }
    decision = pd.select_keeper(parts)
    assert decision["skip"] is True
    assert decision["reason"] == "score delta too small"


def test_size_tiebreaker_keeps_largest(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pd, "cfg", _cfg(SCORE_DELTA_TIEBREAKER="size"))
    parts = {
        1: _part(id=1, score=5000, file_size=10_000_000_000),  # higher score, smaller
        2: _part(id=2, score=4999, file_size=12_000_000_000),  # lower score, LARGER
    }
    decision = pd.select_keeper(parts)
    assert decision["skip"] is False
    assert decision["keeper_id"] == 2  # largest file wins the tie
    assert "tiebreaker" in decision["reason"]


def test_tiebreaker_age_breaks_equal_size(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pd, "cfg", _cfg(SCORE_DELTA_TIEBREAKER="size"))
    size = 10_000_000_000
    parts = {
        1: _part(id=1, score=5000, file_size=size, parts_existence=[_ex(age_hours=100.0)]),
        2: _part(id=2, score=4999, file_size=size, parts_existence=[_ex(age_hours=500.0)]),
    }
    decision = pd.select_keeper(parts)
    assert decision["keeper_id"] == 2  # equal size -> oldest kept


def test_tiebreaker_not_reached_when_delta_clear(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pd, "cfg", _cfg(SCORE_DELTA_TIEBREAKER="size"))
    parts = {
        1: _part(id=1, score=8000, file_size=10_000_000_000),  # clear winner by score
        2: _part(id=2, score=3000, file_size=12_000_000_000),  # larger but far lower score
    }
    decision = pd.select_keeper(parts)
    assert decision["keeper_id"] == 1  # score decides; tiebreaker not triggered
    assert "highest score" in decision["reason"]
