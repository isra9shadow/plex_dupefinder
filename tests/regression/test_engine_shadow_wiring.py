"""Guards for the engine's READ-ONLY shadow-compare wiring (CTO-12, opt-in).

The native-vs-legacy parity itself is locked by ``test_dedupe_shadow.py``. This
file guards the *engine wiring*:

  * ``SHADOW_COMPARE`` ships OFF (a run must never turn the harness on by accident).
  * The exact accumulation the engine does around ``select_keeper`` records zero
    drift when native==legacy and names the drift when they diverge — i.e. the
    harness the engine runs is observational and correct.
"""

from __future__ import annotations

import copy

import plex_dupefinder as pd
from modules.media.dedupe.keeper import KeeperPolicy
from modules.media.dedupe.scoring import ScoreWeights
from modules.media.dedupe.shadow import diff_decision, native_decision


def _part(**over):
    base = {
        "id": 1,
        "exists": True,
        "file": ["/m/a.mkv"],
        "file_size": 10_000_000_000,
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
        "parts_existence": [
            {
                "exists": True,
                "local_check": True,
                "plex_check": True,
                "reason": "local=True, plex=True",
                "file": "/m/a.mkv",
                "plex_path": None,
                "age_hours": 1000.0,
            }
        ],
    }
    base.update(over)
    return base


def _clear_winner():
    return {
        1: _part(id=1, file=["/m/a.mkv"], video_bitrate=20000),
        2: _part(
            id=2,
            file=["/m/b.mkv"],
            audio_codec="aac",
            audio_channels=2,
            video_codec="h264",
            video_resolution="1080",
            video_bitrate=4000,
            video_width=1920,
            video_height=1080,
            has_hdr=False,
        ),
    }


def _engine_shadow_pass(groups, cfg):
    """Replicate the engine's inline accumulation around select_keeper.

    Mirrors plex_dupefinder discover(): for each group, take the legacy decision
    and compare the native port's decision, accumulating (checked, drift_groups)
    exactly as the engine records into run_report['phases']['shadow_compare'].
    """
    weights = ScoreWeights.from_config(cfg)
    policy = KeeperPolicy.from_config(cfg)
    checked = 0
    drift_groups = []
    for title, parts in groups:
        # Mirror the engine: parts arrive already scored (by _build_parts_for_item)
        # before select_keeper runs. Score here unless paths-only mode ignores it.
        built = copy.deepcopy(parts)
        if not cfg["FIND_DUPLICATE_FILEPATHS_ONLY"]:
            for pi in built.values():
                pi["score"], pi["score_breakdown"] = pd.get_score(pi)
        legacy = pd.select_keeper(copy.deepcopy(built))
        native = native_decision(copy.deepcopy(built), weights, policy)
        diffs = diff_decision(native, legacy)
        checked += 1
        if diffs:
            drift_groups.append({"title": title, "diffs": diffs})
    return {"checked": checked, "drift": len(drift_groups), "drift_groups": drift_groups}


def test_shadow_compare_ships_off() -> None:
    """The opt-in harness must default OFF in the shipped config (never auto-on)."""
    assert bool(pd.cfg.get("SHADOW_COMPARE", False)) is False


def test_engine_shadow_pass_records_no_drift() -> None:
    """The engine's accumulation records zero drift when native==legacy."""
    result = _engine_shadow_pass([("Clear Winner", _clear_winner())], pd.cfg)
    assert result == {"checked": 1, "drift": 0, "drift_groups": []}


def test_engine_shadow_pass_records_drift_when_diverging(monkeypatch) -> None:
    """If the legacy decision is perturbed, the engine's pass names the drift."""

    real_select = pd.select_keeper

    def _perturbed(parts):
        decision = real_select(parts)
        decision = dict(decision)
        decision["keeper_id"] = -999  # force a divergence vs the native port
        return decision

    monkeypatch.setattr(pd, "select_keeper", _perturbed)
    result = _engine_shadow_pass([("Clear Winner", _clear_winner())], pd.cfg)
    assert result["checked"] == 1
    assert result["drift"] == 1
    assert result["drift_groups"][0]["title"] == "Clear Winner"
    assert any(d.startswith("keeper_id:") for d in result["drift_groups"][0]["diffs"])
