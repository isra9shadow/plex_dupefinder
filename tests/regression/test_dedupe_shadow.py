"""Parity lock: the READ-ONLY native/legacy shadow comparator (CTO-12).

This proves two things at once:

1. Across the same battery used by the keeper parity test, the native pipeline
   (``shadow.native_decision`` = ``scoring.score`` + ``keeper.select_keeper``)
   produces a decision whose action-relevant fields match the legacy
   ``plex_dupefinder`` pipeline (``pd.get_score`` per part, then
   ``pd.select_keeper``) — so ``diff_decision(native, legacy) == []``.
2. The comparator itself actually detects drift: a hand-crafted differing legacy
   decision makes ``diff_decision`` report exactly the wrong fields.

It is read-only by construction: it builds plain dicts and calls pure functions
— no Plex, no filesystem, no global ``cfg`` mutation beyond monkeypatch.

Run with:  pytest tests/regression/test_dedupe_shadow.py -q
"""

from __future__ import annotations

import copy

import plex_dupefinder as pd
import pytest
from modules.media.dedupe.keeper import KeeperPolicy
from modules.media.dedupe.scoring import ScoreWeights
from modules.media.dedupe.shadow import diff_decision, native_decision


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
    """A part_info with sane metadata, full media-info fields, and one file.

    Unlike the keeper-parity fixtures, every part also carries the scoring inputs
    (``audio_codec`` / ``video_*`` / ``audio_channels`` / ...) so the LEGACY
    ``pd.get_score`` can score it exactly as the native ``scoring.score`` does.
    """
    base = {
        "id": 100,
        "exists": True,
        "file": ["/movies/Title/file.mkv"],
        "file_size": 10_000_000_000,
        # scoring inputs (mirrors get_media_info defaults / real values)
        "audio_codec": "truehd",
        "audio_channels": 8,
        "video_codec": "hevc",
        "video_resolution": "4k",
        "video_bitrate": 12000,
        "video_duration": 7_200_000,
        "video_width": 3840,
        "video_height": 2160,
        "has_hdr": True,
        "has_dv": False,
        "subtitle_count": 3,
        "audio_track_count": 2,
        "parts_existence": [_ex()],
    }
    base.update(over)
    return base


# --- representative parts batteries (mirrors keeper parity) ---------------


def _clear_winner():
    # winner via richer audio (8ch) + higher bitrate vs a weaker sibling.
    return {
        1: _part(
            id=1,
            file=["/m/a.2160p.BluRay.REMUX.mkv"],
            file_size=12_000_000_000,
            audio_channels=8,
            video_bitrate=20000,
            parts_existence=[_ex(file="/m/a.2160p.BluRay.REMUX.mkv")],
        ),
        2: _part(
            id=2,
            file=["/m/b.1080p.WEB-DL.mkv"],
            file_size=10_000_000_000,
            audio_codec="aac",
            audio_channels=2,
            video_codec="h264",
            video_resolution="1080",
            video_bitrate=4000,
            video_width=1920,
            video_height=1080,
            has_hdr=False,
            parts_existence=[_ex(file="/m/b.1080p.WEB-DL.mkv")],
        ),
    }


def _near_tie():
    # two parts that score within a hair of each other -> skip under threshold.
    return {
        1: _part(
            id=1,
            file=["/m/a.mkv"],
            video_bitrate=12000,
            parts_existence=[_ex(file="/m/a.mkv")],
        ),
        2: _part(
            id=2,
            file=["/m/b.mkv"],
            video_bitrate=11999,  # 1-pt bitrate delta after weight floor
            parts_existence=[_ex(file="/m/b.mkv")],
        ),
    }


def _size_ratio_trip():
    # keeper (richer, higher score) is tiny vs a huge low-score sibling.
    return {
        1: _part(
            id=1,
            file=["/m/keeper.mkv"],
            file_size=1_000_000_000,
            audio_channels=8,
            video_bitrate=20000,
            parts_existence=[_ex(file="/m/keeper.mkv")],
        ),
        2: _part(
            id=2,
            file=["/m/huge.mkv"],
            file_size=50_000_000_000,
            audio_codec="aac",
            audio_channels=2,
            video_codec="h264",
            video_resolution="1080",
            video_bitrate=4000,
            video_width=1920,
            video_height=1080,
            has_hdr=False,
            parts_existence=[_ex(file="/m/huge.mkv")],
        ),
    }


def _size_ratio_ok():
    return {
        1: _part(
            id=1,
            file=["/m/keeper.mkv"],
            file_size=12_000_000_000,
            audio_channels=8,
            video_bitrate=20000,
            parts_existence=[_ex(file="/m/keeper.mkv")],
        ),
        2: _part(
            id=2,
            file=["/m/other.mkv"],
            file_size=10_000_000_000,
            audio_codec="aac",
            audio_channels=2,
            video_bitrate=4000,
            has_hdr=False,
            parts_existence=[_ex(file="/m/other.mkv")],
        ),
    }


def _young_cooldown():
    # one part below the 24h default cooldown.
    return {
        1: _part(
            id=1,
            file=["/m/a.mkv"],
            audio_channels=8,
            parts_existence=[_ex(file="/m/a.mkv", age_hours=2.5)],
        ),
        2: _part(
            id=2,
            file=["/m/b.mkv"],
            audio_channels=2,
            parts_existence=[_ex(file="/m/b.mkv", age_hours=500.0)],
        ),
    }


def _single():
    return {
        7: _part(id=7, file=["/m/only.mkv"], parts_existence=[_ex(file="/m/only.mkv")]),
    }


def _all_missing():
    return {
        1: _part(id=1, exists=False, parts_existence=[_ex(exists=False)]),
        2: _part(id=2, exists=False, parts_existence=[_ex(exists=False)]),
    }


def _one_missing():
    return {
        1: _part(
            id=1,
            exists=False,
            file=["/m/a.mkv"],
            audio_channels=8,
            parts_existence=[_ex(file="/m/a.mkv", exists=False)],
        ),
        2: _part(
            id=2,
            file=["/m/b.mkv"],
            audio_channels=2,
            parts_existence=[_ex(file="/m/b.mkv")],
        ),
    }


def _bad_metadata():
    # codec Unknown on an existing candidate -> metadata-sanity skip.
    return {
        1: _part(
            id=1,
            video_codec="Unknown",
            file=["/m/a.mkv"],
            audio_channels=8,
            parts_existence=[_ex(file="/m/a.mkv")],
        ),
        2: _part(
            id=2,
            file=["/m/b.mkv"],
            audio_channels=2,
            parts_existence=[_ex(file="/m/b.mkv")],
        ),
    }


def _zero_duration():
    return {
        1: _part(
            id=1,
            video_duration=0,
            file=["/m/a.mkv"],
            audio_channels=8,
            parts_existence=[_ex(file="/m/a.mkv")],
        ),
        2: _part(
            id=2,
            file=["/m/b.mkv"],
            audio_channels=2,
            parts_existence=[_ex(file="/m/b.mkv")],
        ),
    }


def _no_local_access():
    # every existence entry has local_check=None -> fs not reachable.
    return {
        1: _part(
            id=1,
            file=["/m/a.mkv"],
            audio_channels=8,
            parts_existence=[_ex(file="/m/a.mkv", local_check=None, plex_check=True)],
        ),
        2: _part(
            id=2,
            file=["/m/b.mkv"],
            audio_channels=2,
            parts_existence=[_ex(file="/m/b.mkv", local_check=None, plex_check=True)],
        ),
    }


def _tie_scores():
    # identical media-info -> identical score -> exact tie, delta 0.
    return {
        1: _part(id=1, file=["/m/a.mkv"], parts_existence=[_ex(file="/m/a.mkv")]),
        2: _part(id=2, file=["/m/b.mkv"], parts_existence=[_ex(file="/m/b.mkv")]),
    }


def _multi_part():
    # a media item with two physical parts; youngest age drives the cooldown.
    return {
        1: _part(
            id=1,
            file=["/m/a-cd1.mkv", "/m/a-cd2.mkv"],
            file_size=12_000_000_000,
            audio_channels=8,
            parts_existence=[
                _ex(file="/m/a-cd1.mkv", age_hours=900.0),
                _ex(file="/m/a-cd2.mkv", age_hours=3.0),
            ],
        ),
        2: _part(
            id=2,
            file=["/m/b.mkv"],
            audio_channels=2,
            parts_existence=[_ex(file="/m/b.mkv", age_hours=800.0)],
        ),
    }


def _both_flags():
    # paths-only + require-local-fs both on (set via the cfg fixtures below):
    # paths-only mode ignores score entirely and keeps the lowest media id.
    return {
        9: _part(
            id=9,
            file=["/m/hi.mkv"],
            audio_channels=8,
            parts_existence=[_ex(file="/m/hi.mkv")],
        ),
        4: _part(
            id=4,
            file=["/m/lo.mkv"],
            audio_channels=2,
            parts_existence=[_ex(file="/m/lo.mkv")],
        ),
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
    "both_flags": _both_flags,
}


def _legacy_decision(parts, cfg):
    """The legacy pipeline: per-part ``get_score`` (unless paths-only) then select."""
    built = copy.deepcopy(parts)
    if not cfg["FIND_DUPLICATE_FILEPATHS_ONLY"]:
        for pi in built.values():
            pi["score"], pi["score_breakdown"] = pd.get_score(pi)
    return pd.select_keeper(built)


def _native(parts, cfg):
    return native_decision(
        copy.deepcopy(parts),
        ScoreWeights.from_config(cfg),
        KeeperPolicy.from_config(cfg),
    )


def _assert_no_drift(parts, cfg):
    legacy = _legacy_decision(parts, cfg)
    native = _native(parts, cfg)
    assert diff_decision(native, legacy) == []
    return native, legacy


# --- native vs legacy: no drift across the battery & config variants -----


@pytest.mark.parametrize("name", list(_CASES))
def test_shadow_no_drift_default_cfg(name):
    """Default config: native and legacy decisions agree on action fields."""
    _assert_no_drift(_CASES[name](), pd.cfg)


@pytest.mark.parametrize("name", list(_CASES))
def test_shadow_no_drift_min_score_difference(monkeypatch, name):
    base = copy.deepcopy(pd.cfg)
    base["MIN_SCORE_DIFFERENCE"] = 100
    monkeypatch.setattr(pd, "cfg", base)
    _assert_no_drift(_CASES[name](), base)


@pytest.mark.parametrize("name", list(_CASES))
def test_shadow_no_drift_no_min_age(monkeypatch, name):
    base = copy.deepcopy(pd.cfg)
    base["MIN_FILE_AGE_HOURS"] = 0
    monkeypatch.setattr(pd, "cfg", base)
    _assert_no_drift(_CASES[name](), base)


@pytest.mark.parametrize("name", list(_CASES))
def test_shadow_no_drift_no_max_ratio(monkeypatch, name):
    base = copy.deepcopy(pd.cfg)
    base["MAX_SIZE_RATIO"] = 0
    monkeypatch.setattr(pd, "cfg", base)
    _assert_no_drift(_CASES[name](), base)


@pytest.mark.parametrize("name", list(_CASES))
def test_shadow_no_drift_require_local_fs(monkeypatch, name):
    base = copy.deepcopy(pd.cfg)
    base["REQUIRE_LOCAL_FS_ACCESS"] = True
    monkeypatch.setattr(pd, "cfg", base)
    _assert_no_drift(_CASES[name](), base)


@pytest.mark.parametrize("name", list(_CASES))
def test_shadow_no_drift_both_flags(monkeypatch, name):
    """Paths-only AND require-local-fs both on (the 'both flags' battery axis)."""
    base = copy.deepcopy(pd.cfg)
    base["FIND_DUPLICATE_FILEPATHS_ONLY"] = True
    base["REQUIRE_LOCAL_FS_ACCESS"] = True
    monkeypatch.setattr(pd, "cfg", base)
    _assert_no_drift(_CASES[name](), base)


def test_shadow_clear_winner_picks_keeper():
    """Explicit: the clear winner is kept and nothing is skipped."""
    native, legacy = _assert_no_drift(_clear_winner(), pd.cfg)
    assert native["keeper_id"] == 1
    assert native["skip"] is False
    assert legacy["keeper_id"] == 1


def test_shadow_near_tie_skips_under_threshold(monkeypatch):
    """Explicit: the near-tie skips with a large threshold, and they still agree."""
    base = copy.deepcopy(pd.cfg)
    base["MIN_SCORE_DIFFERENCE"] = 100
    monkeypatch.setattr(pd, "cfg", base)
    native, _ = _assert_no_drift(_near_tie(), base)
    assert native["skip"] is True


def test_shadow_paths_only_lowest_id(monkeypatch):
    """Explicit: paths-only mode keeps the lowest id and native==legacy on it."""
    base = copy.deepcopy(pd.cfg)
    base["FIND_DUPLICATE_FILEPATHS_ONLY"] = True
    monkeypatch.setattr(pd, "cfg", base)
    native, _ = _assert_no_drift(_both_flags(), base)
    assert native["keeper_id"] == 4
    assert native["skip"] is False


# --- the comparator itself detects drift ---------------------------------


def test_diff_decision_identical_is_empty():
    """A decision compared to itself reports no drift."""
    native = _native(_clear_winner(), pd.cfg)
    assert diff_decision(native, copy.deepcopy(native)) == []


def test_diff_decision_reports_drift_fields():
    """Hand-crafted differing decision: diff names exactly the wrong fields."""
    native = _native(_clear_winner(), pd.cfg)
    legacy = copy.deepcopy(native)
    # Deliberately corrupt three action-relevant fields.
    legacy["keeper_id"] = 999
    legacy["top_score"] = -1
    legacy["skip"] = True

    diffs = diff_decision(native, legacy)
    joined = "\n".join(diffs)
    assert any(d.startswith("keeper_id:") for d in diffs)
    assert any(d.startswith("top_score:") for d in diffs)
    assert any(d.startswith("skip:") for d in diffs)
    # Fields that were NOT changed must not be reported.
    assert not any(d.startswith("score_delta:") for d in diffs)
    assert not any(d.startswith("skip_reason:") for d in diffs)
    # The strings carry both sides so a human can read the drift.
    assert "native=" in joined and "legacy=" in joined
    assert len(diffs) == 3


def test_diff_decision_detects_skip_reason_drift():
    """skip_reason is an action-relevant field and drift in it is reported."""
    native = _native(_clear_winner(), pd.cfg)
    legacy = copy.deepcopy(native)
    legacy["skip_reason"] = "fabricated divergent reason"
    diffs = diff_decision(native, legacy)
    assert diffs == [
        f"skip_reason: native={native['skip_reason']!r} legacy={legacy['skip_reason']!r}"
    ]


def test_diff_decision_ignores_non_action_fields():
    """Differences in explanatory-only fields (reason, candidates) are NOT drift."""
    native = _native(_clear_winner(), pd.cfg)
    legacy = copy.deepcopy(native)
    legacy["reason"] = "totally different explanation"
    legacy["candidates_existing"] = [42]
    legacy["youngest_age_hours"] = 0.0
    legacy["max_size_ratio"] = 99.0
    assert diff_decision(native, legacy) == []
