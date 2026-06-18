"""Coverage for the legacy engine's pure helpers (CTO-02 ratchet).

These functions have no Plex/network/FS dependency, so they are cheap to pin and
they harden the component that actually moves/deletes media. No production code
changes — tests only.

Run with:  pytest tests/regression/test_engine_pure.py -q
"""

from __future__ import annotations

import copy

import plex_dupefinder as pd
import pytest

# --- formatters --------------------------------------------------------------


def test_millis_to_string():
    assert pd.millis_to_string(0) == "00:00:00"
    assert pd.millis_to_string(3_661_000) == "01:01:01"  # 1h1m1s


def test_bytes_to_string():
    assert pd.bytes_to_string(1) == "1 byte"
    assert pd.bytes_to_string(0) == "0 bytes"
    assert pd.bytes_to_string(1024) == "1 KB"
    assert pd.bytes_to_string(5 * 1024**3).endswith("GB")


def test_kbps_to_string():
    assert pd.kbps_to_string(500) == "500 Kbps"
    assert pd.kbps_to_string(2048) == "2.00 Mbps"


# --- PATH_MAPPINGS resolution (correctness-critical) -------------------------


@pytest.fixture
def cfg(monkeypatch):
    base = copy.deepcopy(pd.cfg)
    monkeypatch.setattr(pd, "cfg", base)
    return base


def test_resolve_no_mapping_is_passthrough(cfg):
    cfg["PATH_MAPPINGS"] = {}
    assert pd.resolve_fs_path("/tv/Show/ep.mkv") == "/tv/Show/ep.mkv"
    assert pd._resolve_with_mapping("/tv/x")[1] is None  # no matched prefix


def test_resolve_longest_prefix_wins(cfg):
    cfg["PATH_MAPPINGS"] = {"/media": "/mnt/a", "/media/tv": "/mnt/tv"}
    resolved, prefix = pd._resolve_with_mapping("/media/tv/Show/ep.mkv")
    assert resolved == "/mnt/tv/Show/ep.mkv"
    assert prefix == "/media/tv"


def test_resolve_empty_path(cfg):
    assert pd._resolve_with_mapping("") == ("", None)


# --- classify_exception ------------------------------------------------------


def test_classify_exception_buckets():
    assert pd.classify_exception(None) == "UNKNOWN"
    assert pd.classify_exception(PermissionError()) == "PERMISSION_DENIED"
    assert pd.classify_exception(FileNotFoundError()) == "PATH_NOT_FOUND"
    assert pd.classify_exception(OSError()) == "MOVE_FAILED"
    assert pd.classify_exception(ValueError("x")) == "UNKNOWN"


# --- should_skip -------------------------------------------------------------


def test_should_skip(cfg):
    cfg["SKIP_LIST"] = ["/protected/", ".sample."]
    assert pd.should_skip(["/protected/movie.mkv"]) is True
    assert pd.should_skip(["/media/movie.sample.mkv"]) is True
    assert pd.should_skip(["/media/movie.mkv"]) is False


# --- compute_lowest_confidence_groups ----------------------------------------


def _group(title, *, skip=False, delta=None, keeper_id=1):
    return {
        "title": title,
        "library": "Movies",
        "item_key": f"/k/{title}",
        "parts": {keeper_id: {"file": [f"/m/{title}.mkv"]}},
        "decision": {
            "skip": skip,
            "score_delta": delta,
            "keeper_id": keeper_id,
            "top_score": 100,
            "second_score": 100 - (delta or 0),
        },
    }


def test_lowest_confidence_ranks_and_filters():
    groups = [
        _group("close", delta=5),
        _group("wide", delta=500),
        _group("skipped", skip=True, delta=1),
        _group("single", delta=None),
    ]
    ranked = pd.compute_lowest_confidence_groups(groups, limit=10)
    titles = [r["title"] for r in ranked]
    assert titles == ["close", "wide"]  # skipped + single excluded, sorted by delta
    assert ranked[0]["keeper_files"] == ["/m/close.mkv"]


def test_lowest_confidence_limit():
    groups = [_group(str(i), delta=i) for i in range(5)]
    assert len(pd.compute_lowest_confidence_groups(groups, limit=2)) == 2


# --- _snapshot_diff ----------------------------------------------------------


def test_snapshot_diff_detects_field_change():
    before = {
        "updatedAt": "1",
        "media": [{"id": 1, "bitrate": 8000, "parts": [{"id": 9, "size": 10}]}],
    }
    after = {
        "updatedAt": "1",
        "media": [{"id": 1, "bitrate": 9000, "parts": [{"id": 9, "size": 10}]}],
    }
    assert pd._snapshot_diff(before, after) == ["media[0].bitrate"]


def test_snapshot_diff_media_count_change():
    before = {"updatedAt": "1", "media": [{"id": 1}]}
    after = {"updatedAt": "1", "media": []}
    assert pd._snapshot_diff(before, after) == ["media_count(1->0)"]


def test_snapshot_diff_identical_is_empty():
    snap = {"updatedAt": "1", "media": [{"id": 1, "parts": [{"id": 9}]}]}
    assert pd._snapshot_diff(snap, copy.deepcopy(snap)) == []
