"""Tests for the opt-in parallel pre-gather in discovery_pass (IMP-04 / Agente B).

The pre-gather is read-only and best-effort: the serial loop still owns
decisions, ordering and error handling. workers=1 must be byte-identical to the
historical serial path; a prefetch failure for one item must not affect others.

Run with:  pytest tests/regression/test_discovery_parallel.py -q
"""

from __future__ import annotations

import copy

import plex_dupefinder as pd

_DEC = {
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


def _wire(monkeypatch, items, build=None):
    monkeypatch.setattr(pd, "get_dupes", lambda section: items)
    monkeypatch.setattr(pd, "_item_title", lambda it: it.key)
    monkeypatch.setattr(pd, "_build_parts_for_item", build or (lambda it, ch: {}))
    monkeypatch.setattr(pd, "select_keeper", lambda parts: dict(_DEC))


def _cfg(monkeypatch, **over):
    base = copy.deepcopy(pd.cfg)
    base.update(PRE_ANALYZE_DUPLICATES=False, PARTIAL_HASH_ENABLED=False, **over)
    monkeypatch.setattr(pd, "cfg", base)


def test_parallel_prefetch_used(monkeypatch):
    _cfg(monkeypatch, DISCOVERY_GATHER_WORKERS=4)
    _wire(monkeypatch, [_Item("/a"), _Item("/b"), _Item("/c")])

    groups = pd.discovery_pass(["Movies"])

    assert len(groups) == 3
    bd = pd.run_report["phases"]["discovery_breakdown"]
    assert bd["gather_workers"] == 4
    assert bd["gather_prefetched"] == 3


def test_serial_when_workers_1(monkeypatch):
    _cfg(monkeypatch, DISCOVERY_GATHER_WORKERS=1)
    _wire(monkeypatch, [_Item("/a"), _Item("/b")])

    groups = pd.discovery_pass(["Movies"])

    assert len(groups) == 2
    assert pd.run_report["phases"]["discovery_breakdown"]["gather_prefetched"] == 0


def test_prefetch_failure_falls_back_without_affecting_others(monkeypatch):
    _cfg(monkeypatch, DISCOVERY_GATHER_WORKERS=4)

    def _build(it, ch):
        if it.key == "/bad":  # raises in prefetch AND on serial recompute
            raise RuntimeError("boom")
        return {}

    _wire(monkeypatch, [_Item("/good"), _Item("/bad")], build=_build)

    groups = pd.discovery_pass(["Movies"])

    # The good item is gathered; the bad one is isolated to the loop's error path.
    keys = [g["item_key"] for g in groups]
    assert "/good" in keys
    assert "/bad" not in keys
    assert pd.run_report["phases"]["discovery_breakdown"]["gather_prefetched"] == 1
