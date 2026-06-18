"""Tests for the opt-in inconsistency tolerances (production finding #2).

`groups_skipped_inconsistent` grew run-to-run because Plex re-estimates
bitrate/duration between passes without the file changing. These tolerances
absorb that drift ONLY when the file size is identical — a real transcode
changes size and still trips inconsistency.

Run with:  pytest tests/test_inconsistency_tolerance.py -q
"""

from __future__ import annotations

import copy

import plex_dupefinder as pd
import pytest


@pytest.fixture
def cfg():
    base = copy.deepcopy(pd.cfg)
    base.update(
        PARTIAL_HASH_ENABLED=False,
        INCONSISTENCY_BITRATE_TOLERANCE_PCT=0,
        INCONSISTENCY_DURATION_TOLERANCE_MS=0,
    )
    pd.cfg = base
    return base


def _part(file="/m/x.mkv", size=1000, bitrate=8000, duration=3_600_000, codec="hevc"):
    return {
        "file": [file],
        "file_size": size,
        "exists": True,
        "video_bitrate": bitrate,
        "video_duration": duration,
        "video_codec": codec,
        "parts_existence": [],
    }


_DEC = {"skip": False, "keeper_id": 1, "skip_reason": None}


def test_bitrate_drift_trips_when_strict(cfg):
    snap = {1: _part(bitrate=8000)}
    fresh = {1: _part(bitrate=8002)}
    diffs = pd.detect_inconsistencies(snap, fresh, _DEC, _DEC)
    assert any("bitrate" in d for d in diffs)


def test_bitrate_drift_tolerated_when_size_unchanged(cfg):
    cfg["INCONSISTENCY_BITRATE_TOLERANCE_PCT"] = 1.0  # 1% tolerance
    snap = {1: _part(bitrate=8000)}
    fresh = {1: _part(bitrate=8050)}  # ~0.6% drift, same size
    diffs = pd.detect_inconsistencies(snap, fresh, _DEC, _DEC)
    assert diffs == []


def test_bitrate_tolerance_does_not_mask_size_change(cfg):
    cfg["INCONSISTENCY_BITRATE_TOLERANCE_PCT"] = 50.0  # generous
    # A real transcode: bitrate AND size change -> still inconsistent.
    snap = {1: _part(bitrate=8000, size=1000)}
    fresh = {1: _part(bitrate=8050, size=900)}
    diffs = pd.detect_inconsistencies(snap, fresh, _DEC, _DEC)
    assert any("size" in d for d in diffs)


def test_duration_drift_tolerated_when_size_unchanged(cfg):
    cfg["INCONSISTENCY_DURATION_TOLERANCE_MS"] = 100
    snap = {1: _part(duration=3_600_000)}
    fresh = {1: _part(duration=3_600_050)}  # +50 ms, same size
    diffs = pd.detect_inconsistencies(snap, fresh, _DEC, _DEC)
    assert diffs == []


def test_large_drift_still_trips_despite_tolerance(cfg):
    cfg["INCONSISTENCY_BITRATE_TOLERANCE_PCT"] = 1.0
    snap = {1: _part(bitrate=8000)}
    fresh = {1: _part(bitrate=9000)}  # 12.5% drift > 1%
    diffs = pd.detect_inconsistencies(snap, fresh, _DEC, _DEC)
    assert any("bitrate" in d for d in diffs)
