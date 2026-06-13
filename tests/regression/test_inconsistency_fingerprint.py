"""Tests for the opt-in stable-fingerprint consistency check (IMP-05 / Agente C).

The fingerprint identity (media_id + size + duration_s + codec + partial_hash)
must ignore cosmetic drift (rename, bitrate re-estimate, sub-second duration
jitter) yet still trip on real content change (size/codec/hash) and on the
always-material signals (existence flip, keeper change).

Run with:  pytest tests/regression/test_inconsistency_fingerprint.py -q
"""

from __future__ import annotations

import copy

import plex_dupefinder as pd
import pytest


@pytest.fixture
def cfg(monkeypatch):
    base = copy.deepcopy(pd.cfg)
    base.update(INCONSISTENCY_USE_FINGERPRINT=True, PARTIAL_HASH_ENABLED=True)
    monkeypatch.setattr(pd, "cfg", base)
    return base


def _part(
    *,
    mid=1,
    file="/m/x.mkv",
    size=1000,
    dur=3_600_000,
    codec="hevc",
    head="h",
    tail="t",
    exists=True,
):
    return {
        "id": mid,
        "file": [file],
        "file_size": size,
        "exists": exists,
        "video_bitrate": 8000,
        "video_duration": dur,
        "video_codec": codec,
        "parts_existence": [{"partial_hash": {"head_sha256": head, "tail_sha256": tail}}],
    }


_DEC = {"keeper_id": 1, "skip": False, "skip_reason": None}


def _diffs(snap, fresh, snap_dec=None, fresh_dec=None):
    return pd.detect_inconsistencies(
        {1: snap}, {1: fresh}, snap_dec or dict(_DEC), fresh_dec or dict(_DEC)
    )


def test_rename_is_consistent(cfg):
    # The headline case: file renamed/moved, bytes identical -> NOT inconsistent.
    assert _diffs(_part(file="/old/name.mkv"), _part(file="/new/name.mkv")) == []


def test_bitrate_reestimate_is_consistent(cfg):
    s = _part()
    f = _part()
    f["video_bitrate"] = 8123  # Plex re-estimate, same bytes
    assert _diffs(s, f) == []


def test_subsecond_duration_jitter_is_consistent(cfg):
    assert _diffs(_part(dur=3_600_000), _part(dur=3_600_400)) == []  # same whole second


def test_real_size_change_trips(cfg):
    assert any("fingerprint" in d for d in _diffs(_part(size=1000), _part(size=900)))


def test_codec_change_trips(cfg):
    assert any("fingerprint" in d for d in _diffs(_part(codec="hevc"), _part(codec="h264")))


def test_partial_hash_change_trips(cfg):
    assert any("fingerprint" in d for d in _diffs(_part(head="h"), _part(head="DIFFERENT")))


def test_existence_flip_always_trips(cfg):
    assert any("existence" in d for d in _diffs(_part(exists=True), _part(exists=False)))


def test_keeper_change_always_trips(cfg):
    diffs = _diffs(_part(), _part(), fresh_dec={"keeper_id": 2, "skip": False, "skip_reason": None})
    assert any("keeper changed" in d for d in diffs)


def test_default_mode_still_trips_on_rename(monkeypatch):
    # Compatibility guard: with the flag OFF, behaviour is unchanged (rename trips).
    base = copy.deepcopy(pd.cfg)
    base.update(INCONSISTENCY_USE_FINGERPRINT=False, PARTIAL_HASH_ENABLED=False)
    monkeypatch.setattr(pd, "cfg", base)
    diffs = _diffs(_part(file="/old.mkv"), _part(file="/new.mkv"))
    assert any("files changed" in d for d in diffs)
