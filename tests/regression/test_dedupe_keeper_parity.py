"""Parity lock: the ported keeper selection must match the legacy EXACTLY (CTO-12).

This is the oracle that makes the eventual native migration safe — if the port
ever drifts from ``plex_dupefinder.select_keeper`` (any skip guard, any field of
the decision dict, any skip-reason string), these fail.

Run with:  pytest tests/regression/test_dedupe_keeper_parity.py -q
"""

from __future__ import annotations

import copy

import plex_dupefinder as pd
import pytest
from modules.media.dedupe.keeper import KeeperPolicy, select_keeper


def _ex(**over):
    """A single parts_existence entry (one file on disk)."""
    base = {
        "exists": True,
        "local_check": True,
        "plex_check": True,
        "reason": "local=True, plex=True",
        "file": "/movies/Title/file.mkv",
        "plex_path": None,
        "age_hours": 1000.0,
    }
    base.update(over)
    return base


def _part(**over):
    """A part_info with sane metadata and one existing file by default."""
    base = {
        "id": 100,
        "score": 5000,
        "exists": True,
        "file": ["/movies/Title/file.mkv"],
        "file_size": 10_000_000_000,
        "video_duration": 7_200_000,
        "video_bitrate": 12000,
        "video_codec": "hevc",
        "parts_existence": [_ex()],
    }
    base.update(over)
    return base


# --- representative parts batteries --------------------------------------

def _clear_winner():
    return {
        1: _part(id=1, score=8000, file_size=12_000_000_000,
                 file=["/m/a.mkv"], parts_existence=[_ex(file="/m/a.mkv")]),
        2: _part(id=2, score=3000, file_size=10_000_000_000,
                 file=["/m/b.mkv"], parts_existence=[_ex(file="/m/b.mkv")]),
    }


def _near_tie():
    # delta = 1, below a threshold > 1 -> skip when MIN_SCORE_DIFFERENCE set.
    return {
        1: _part(id=1, score=5000, file=["/m/a.mkv"], parts_existence=[_ex(file="/m/a.mkv")]),
        2: _part(id=2, score=4999, file=["/m/b.mkv"], parts_existence=[_ex(file="/m/b.mkv")]),
    }


def _size_ratio_trip():
    # keeper (highest score) is tiny vs a sibling -> ratio guard trips.
    return {
        1: _part(id=1, score=9000, file_size=1_000_000_000,
                 file=["/m/keeper.mkv"], parts_existence=[_ex(file="/m/keeper.mkv")]),
        2: _part(id=2, score=1000, file_size=50_000_000_000,
                 file=["/m/huge.mkv"], parts_existence=[_ex(file="/m/huge.mkv")]),
    }


def _size_ratio_ok():
    return {
        1: _part(id=1, score=9000, file_size=12_000_000_000,
                 file=["/m/keeper.mkv"], parts_existence=[_ex(file="/m/keeper.mkv")]),
        2: _part(id=2, score=1000, file_size=10_000_000_000,
                 file=["/m/other.mkv"], parts_existence=[_ex(file="/m/other.mkv")]),
    }


def _young_cooldown():
    # one part below the 24h default cooldown.
    return {
        1: _part(id=1, score=8000, file=["/m/a.mkv"],
                 parts_existence=[_ex(file="/m/a.mkv", age_hours=2.5)]),
        2: _part(id=2, score=3000, file=["/m/b.mkv"],
                 parts_existence=[_ex(file="/m/b.mkv", age_hours=500.0)]),
    }


def _single():
    return {7: _part(id=7, score=4242, file=["/m/only.mkv"],
                     parts_existence=[_ex(file="/m/only.mkv")])}


def _all_missing():
    return {
        1: _part(id=1, exists=False, parts_existence=[_ex(exists=False)]),
        2: _part(id=2, exists=False, parts_existence=[_ex(exists=False)]),
    }


def _one_missing():
    return {
        1: _part(id=1, score=8000, exists=False, file=["/m/a.mkv"],
                 parts_existence=[_ex(file="/m/a.mkv", exists=False)]),
        2: _part(id=2, score=3000, file=["/m/b.mkv"],
                 parts_existence=[_ex(file="/m/b.mkv")]),
    }


def _bad_metadata():
    return {
        1: _part(id=1, score=8000, video_codec="Unknown",
                 file=["/m/a.mkv"], parts_existence=[_ex(file="/m/a.mkv")]),
        2: _part(id=2, score=3000, file=["/m/b.mkv"],
                 parts_existence=[_ex(file="/m/b.mkv")]),
    }


def _zero_duration():
    return {
        1: _part(id=1, score=8000, video_duration=0,
                 file=["/m/a.mkv"], parts_existence=[_ex(file="/m/a.mkv")]),
        2: _part(id=2, score=3000, file=["/m/b.mkv"],
                 parts_existence=[_ex(file="/m/b.mkv")]),
    }


def _no_local_access():
    # every existence entry has local_check=None -> fs not reachable.
    return {
        1: _part(id=1, score=8000, file=["/m/a.mkv"],
                 parts_existence=[_ex(file="/m/a.mkv", local_check=None, plex_check=True)]),
        2: _part(id=2, score=3000, file=["/m/b.mkv"],
                 parts_existence=[_ex(file="/m/b.mkv", local_check=None, plex_check=True)]),
    }


def _tie_scores():
    # exact tie -> stable-sort keeps insertion order; delta 0.
    return {
        1: _part(id=1, score=5000, file=["/m/a.mkv"], parts_existence=[_ex(file="/m/a.mkv")]),
        2: _part(id=2, score=5000, file=["/m/b.mkv"], parts_existence=[_ex(file="/m/b.mkv")]),
    }


def _multi_part():
    # a media item with two physical parts; youngest age drives the cooldown.
    return {
        1: _part(
            id=1, score=8000, file=["/m/a-cd1.mkv", "/m/a-cd2.mkv"],
            file_size=12_000_000_000,
            parts_existence=[
                _ex(file="/m/a-cd1.mkv", age_hours=900.0),
                _ex(file="/m/a-cd2.mkv", age_hours=3.0),
            ],
        ),
        2: _part(id=2, score=3000, file=["/m/b.mkv"],
                 parts_existence=[_ex(file="/m/b.mkv", age_hours=800.0)]),
    }


_CASES = {
    "clear_winner": _clear_winner,
    "near_tie": _near_tie,
    "size_ratio_trip": _size_ratio_trip,
    "size_ratio_ok": _size_ratio_ok,
    "young_cooldown": _young_cooldown,
    "single": _single,
    "all_missing": _all_missing,
    "one_missing": _one_missing,
    "bad_metadata": _bad_metadata,
    "zero_duration": _zero_duration,
    "no_local_access": _no_local_access,
    "tie_scores": _tie_scores,
    "multi_part": _multi_part,
}


def _assert_parity(parts, cfg):
    """The port and the legacy must agree on the full decision dict, byte-for-byte."""
    legacy = pd.select_keeper(copy.deepcopy(parts))
    ported = select_keeper(copy.deepcopy(parts), KeeperPolicy.from_config(cfg))
    assert ported == legacy
    return ported


@pytest.mark.parametrize("name", list(_CASES))
def test_keeper_matches_legacy_default_cfg(name):
    """Default config (MIN_SCORE_DIFFERENCE=0, MIN_FILE_AGE_HOURS=24, MAX_SIZE_RATIO=5)."""
    _assert_parity(_CASES[name](), pd.cfg)


@pytest.mark.parametrize("name", list(_CASES))
def test_keeper_matches_legacy_min_score_difference(monkeypatch, name):
    """MIN_SCORE_DIFFERENCE > 1 turns the near-tie into a skip."""
    base = copy.deepcopy(pd.cfg)
    base["MIN_SCORE_DIFFERENCE"] = 100
    monkeypatch.setattr(pd, "cfg", base)
    _assert_parity(_CASES[name](), base)


@pytest.mark.parametrize("name", list(_CASES))
def test_keeper_matches_legacy_no_min_age(monkeypatch, name):
    """MIN_FILE_AGE_HOURS=0 disables the cooldown guard entirely."""
    base = copy.deepcopy(pd.cfg)
    base["MIN_FILE_AGE_HOURS"] = 0
    monkeypatch.setattr(pd, "cfg", base)
    _assert_parity(_CASES[name](), base)


@pytest.mark.parametrize("name", list(_CASES))
def test_keeper_matches_legacy_no_max_ratio(monkeypatch, name):
    """MAX_SIZE_RATIO=0 disables the size-ratio guard."""
    base = copy.deepcopy(pd.cfg)
    base["MAX_SIZE_RATIO"] = 0
    monkeypatch.setattr(pd, "cfg", base)
    _assert_parity(_CASES[name](), base)


@pytest.mark.parametrize("name", list(_CASES))
def test_keeper_matches_legacy_require_local_fs(monkeypatch, name):
    """REQUIRE_LOCAL_FS_ACCESS=True trips the 'no_local_access' case to a skip."""
    base = copy.deepcopy(pd.cfg)
    base["REQUIRE_LOCAL_FS_ACCESS"] = True
    monkeypatch.setattr(pd, "cfg", base)
    _assert_parity(_CASES[name](), base)


@pytest.mark.parametrize("name", list(_CASES))
def test_keeper_matches_legacy_filepaths_only(monkeypatch, name):
    """FIND_DUPLICATE_FILEPATHS_ONLY=True picks the lowest media id, ignoring score."""
    base = copy.deepcopy(pd.cfg)
    base["FIND_DUPLICATE_FILEPATHS_ONLY"] = True
    monkeypatch.setattr(pd, "cfg", base)
    _assert_parity(_CASES[name](), base)


def test_keeper_filepaths_only_lowest_id(monkeypatch):
    """Explicit check: paths-only mode keeps the lowest id even when score favours another."""
    base = copy.deepcopy(pd.cfg)
    base["FIND_DUPLICATE_FILEPATHS_ONLY"] = True
    monkeypatch.setattr(pd, "cfg", base)
    parts = {
        9: _part(id=9, score=9999, file=["/m/hi.mkv"], parts_existence=[_ex(file="/m/hi.mkv")]),
        4: _part(id=4, score=10, file=["/m/lo.mkv"], parts_existence=[_ex(file="/m/lo.mkv")]),
    }
    decision = _assert_parity(parts, base)
    assert decision["keeper_id"] == 4
    assert decision["skip"] is False


def test_keeper_near_tie_skips_under_threshold(monkeypatch):
    """Explicit check: the near-tie really does skip with a large threshold."""
    base = copy.deepcopy(pd.cfg)
    base["MIN_SCORE_DIFFERENCE"] = 100
    monkeypatch.setattr(pd, "cfg", base)
    decision = _assert_parity(_near_tie(), base)
    assert decision["skip"] is True
    assert decision["reason"] == "score delta too small"


def test_keeper_size_ratio_message_parity():
    """Explicit check: the size-ratio skip string (with bytes_to_string) matches byte-for-byte."""
    decision = _assert_parity(_size_ratio_trip(), pd.cfg)
    assert decision["skip"] is True
    assert decision["reason"] == "size ratio protection"
