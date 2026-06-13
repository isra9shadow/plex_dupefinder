"""Safety net for the irreversible removal path (CTO-05).

remove_item is the gate for everything destructive. These tests assert, with the
Plex delete (remove_plex_metadata) and the mover (quarantine_files) mocked:

  * AUDIT/DRY_RUN never touches Plex or disk;
  * EXECUTION_MODE=AUDIT forces that even with acting legacy flags;
  * QUARANTINE acts by moving (then clears metadata only on a clean move);
  * DELETE is the only path that calls Plex delete without quarantining.

Run with:  pytest tests/regression/test_delete_path.py -q
"""

from __future__ import annotations

import copy

import plex_dupefinder as pd
import pytest


def _part():
    return {
        "id": 5,
        "show_key": "/library/metadata/5",
        "file": ["/m/loser.mkv"],
        "file_size": 1000,
        "parts_existence": [{"local_check": True}],  # present, not a stale orphan
    }


@pytest.fixture
def spy_delete(monkeypatch):
    calls: list[tuple] = []

    def _spy(show_key, media_id):
        calls.append((show_key, media_id))
        return True, "deleted"

    monkeypatch.setattr(pd, "remove_plex_metadata", _spy)
    return calls


def _set_cfg(monkeypatch, **over):
    base = copy.deepcopy(pd.cfg)
    base.update(FIND_DUPLICATE_FILEPATHS_ONLY=False, **over)
    monkeypatch.setattr(pd, "cfg", base)
    return base


def test_dry_run_never_deletes(monkeypatch, spy_delete):
    _set_cfg(monkeypatch, DRY_RUN=True)
    result = pd.remove_item(_part(), "dup")
    assert result["mode"] == "dry_run"
    assert result["plex_delete"] is None
    assert spy_delete == []  # Plex delete NEVER called


def test_audit_execution_mode_forces_no_delete(monkeypatch, spy_delete):
    # Even with acting legacy flags, EXECUTION_MODE=AUDIT must block deletion.
    cfg = _set_cfg(monkeypatch, DRY_RUN=False, AUDIT_MODE=False, QUARANTINE_MODE=False)
    cfg["EXECUTION_MODE"] = "AUDIT"
    pd.apply_execution_mode(cfg)
    result = pd.remove_item(_part(), "dup")
    assert result["mode"] == "dry_run"
    assert spy_delete == []


def test_quarantine_mode_moves_then_clears_metadata(monkeypatch, spy_delete):
    _set_cfg(monkeypatch, DRY_RUN=False, AUDIT_MODE=False, QUARANTINE_MODE=True)
    moved = {"moved": ["/m/loser.mkv"], "errors": []}
    monkeypatch.setattr(pd, "quarantine_files", lambda *a, **k: moved)
    result = pd.remove_item(_part(), "dup", title="X", library_name="Movies")
    assert result["mode"] == "quarantine"
    assert result["success"] is True
    assert len(spy_delete) == 1  # metadata cleared only after a clean move


def test_quarantine_incomplete_preserves_plex_entry(monkeypatch, spy_delete):
    _set_cfg(monkeypatch, DRY_RUN=False, AUDIT_MODE=False, QUARANTINE_MODE=True)
    partial = {"moved": ["/m/a.mkv"], "errors": [{"file": "/m/b.mkv", "error": "EIO"}]}
    monkeypatch.setattr(pd, "quarantine_files", lambda *a, **k: partial)
    result = pd.remove_item(_part(), "dup", title="X", library_name="Movies")
    # A partial move must NOT delete the Plex entry (would orphan the unmoved part).
    assert spy_delete == []
    assert "incomplete" in (result["error"] or "")


def test_direct_delete_is_only_acting_non_quarantine_path(monkeypatch, spy_delete):
    _set_cfg(monkeypatch, DRY_RUN=False, AUDIT_MODE=False, QUARANTINE_MODE=False)
    result = pd.remove_item(_part(), "dup")
    assert result["mode"] == "direct"
    assert result["success"] is True
    assert spy_delete == [("/library/metadata/5", 5)]  # deletes the loser only
