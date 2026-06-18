"""Tests for the discovery per-phase profiling + PASS0 fast-path counting (IMP-03).

Drives ``discovery_pass`` with fakes (no Plex) to verify the read-only breakdown
is recorded and that a fingerprint cache hit skips ``refresh_plex_item``.

Run with:  pytest tests/test_discovery_breakdown.py -q
"""

from __future__ import annotations

import copy

import plex_dupefinder as pd

_DECISION = {
    "keeper_id": 1,
    "skip": False,
    "skip_reason": None,
    "reason": "r",
    "top_score": 1,
    "second_score": 0,
    "score_delta": 1,
    "max_size_ratio": 1.0,
    "youngest_age_hours": 100,
}


class _Item:
    def __init__(self, key: str) -> None:
        self.key = key


def _wire(monkeypatch, item):
    monkeypatch.setattr(pd, "get_dupes", lambda section: [item])
    monkeypatch.setattr(pd, "_item_title", lambda it: "Title")
    monkeypatch.setattr(pd, "_build_parts_for_item", lambda it, ch: {})
    monkeypatch.setattr(pd, "select_keeper", lambda parts: dict(_DECISION))


def test_breakdown_recorded_without_pass0(monkeypatch):
    base = copy.deepcopy(pd.cfg)
    base.update(PRE_ANALYZE_DUPLICATES=False, PARTIAL_HASH_ENABLED=False)
    monkeypatch.setattr(pd, "cfg", base)
    _wire(monkeypatch, _Item("/k1"))

    groups = pd.discovery_pass(["Movies"])

    assert len(groups) == 1
    bd = pd.run_report["phases"]["discovery_breakdown"]
    assert {"get_dupes_seconds", "pass0_seconds", "gather_seconds", "pass0_cache_hits"} <= set(bd)
    assert bd["pass0_cache_hits"] == 0


def test_fingerprint_cache_hit_skips_refresh(monkeypatch):
    base = copy.deepcopy(pd.cfg)
    base.update(
        PRE_ANALYZE_DUPLICATES=True,
        PASS0_FINGERPRINT_CACHE=True,
        PARTIAL_HASH_ENABLED=False,
        ANALYZE_TIMEOUT_SECONDS=1,
    )
    monkeypatch.setattr(pd, "cfg", base)
    item = _Item("/k2")
    _wire(monkeypatch, item)

    monkeypatch.setattr(pd, "_pass0_fingerprint", lambda it: "FP")
    monkeypatch.setattr(pd, "_load_pass0_cache", lambda: {item.key: {"fingerprint": "FP"}})
    monkeypatch.setattr(pd, "_save_pass0_cache", lambda cache: None)

    called = {"refresh": False}

    def _boom(it, timeout):  # must NOT run on a cache hit
        called["refresh"] = True
        return it, {"attempted": True, "success": True, "status": "sane_unchanged"}

    monkeypatch.setattr(pd, "refresh_plex_item", _boom)

    groups = pd.discovery_pass(["Movies"])

    assert called["refresh"] is False
    assert pd.run_report["phases"]["discovery_breakdown"]["pass0_cache_hits"] == 1
    assert groups[0]["pass0_status"]["from_cache"] is True
