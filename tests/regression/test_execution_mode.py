"""Tests for the unified ExecutionMode (IMP-01 / Agente A).

One explicit mode {AUDIT, QUARANTINE, DELETE} derived from — or, when
EXECUTION_MODE is set, normalizing — the legacy DRY_RUN/AUDIT_MODE/
QUARANTINE_MODE flags. Backward compatible and fail-safe.

Run with:  pytest tests/regression/test_execution_mode.py -q
"""

from __future__ import annotations

import plex_dupefinder as pd

# --- resolve_execution_mode: pure derivation from legacy flags ---------------


def test_resolve_defaults_to_audit():
    assert pd.resolve_execution_mode({}) == "AUDIT"  # DRY_RUN defaults True


def test_resolve_dry_run_is_audit():
    assert pd.resolve_execution_mode({"DRY_RUN": True}) == "AUDIT"


def test_resolve_audit_mode_overrides_acting():
    assert pd.resolve_execution_mode({"DRY_RUN": False, "AUDIT_MODE": True}) == "AUDIT"


def test_resolve_quarantine():
    cfg = {"DRY_RUN": False, "AUDIT_MODE": False, "QUARANTINE_MODE": True}
    assert pd.resolve_execution_mode(cfg) == "QUARANTINE"


def test_resolve_delete():
    cfg = {"DRY_RUN": False, "AUDIT_MODE": False, "QUARANTINE_MODE": False}
    assert pd.resolve_execution_mode(cfg) == "DELETE"


# --- apply_execution_mode: EXECUTION_MODE is the single source of truth -------


def test_apply_absent_does_not_mutate():
    cfg = {"DRY_RUN": False, "AUDIT_MODE": False, "QUARANTINE_MODE": False}
    assert pd.apply_execution_mode(cfg) == "DELETE"
    assert cfg == {"DRY_RUN": False, "AUDIT_MODE": False, "QUARANTINE_MODE": False}


def test_apply_audit_normalizes_flags():
    cfg = {
        "DRY_RUN": False,
        "AUDIT_MODE": False,
        "QUARANTINE_MODE": False,
        "EXECUTION_MODE": "AUDIT",
    }
    assert pd.apply_execution_mode(cfg) == "AUDIT"
    assert cfg["DRY_RUN"] is True and cfg["AUDIT_MODE"] is True


def test_apply_quarantine_normalizes_flags():
    cfg = {"DRY_RUN": True, "EXECUTION_MODE": "quarantine"}  # case-insensitive
    assert pd.apply_execution_mode(cfg) == "QUARANTINE"
    assert cfg["DRY_RUN"] is False and cfg["AUDIT_MODE"] is False and cfg["QUARANTINE_MODE"] is True


def test_apply_delete_normalizes_flags():
    cfg = {"DRY_RUN": True, "EXECUTION_MODE": "DELETE"}
    assert pd.apply_execution_mode(cfg) == "DELETE"
    assert cfg["DRY_RUN"] is False and cfg["QUARANTINE_MODE"] is False


def test_apply_invalid_fails_safe_to_audit():
    cfg = {"DRY_RUN": False, "AUDIT_MODE": False, "EXECUTION_MODE": "nuke-everything"}
    assert pd.apply_execution_mode(cfg) == "AUDIT"
    assert cfg["DRY_RUN"] is True  # safe default: observe only


def test_apply_delete_makes_run_acting():
    # Downstream `acting = (not DRY_RUN) and (not AUDIT_MODE)` must be True for DELETE.
    cfg = {"DRY_RUN": True, "EXECUTION_MODE": "DELETE"}
    pd.apply_execution_mode(cfg)
    acting = (not cfg["DRY_RUN"]) and (not cfg["AUDIT_MODE"])
    assert acting is True


def test_env_var_wins_over_config(monkeypatch):
    # The platform wrapper sets IZUMI_EXECUTION_MODE; it must beat the config key.
    monkeypatch.setenv("IZUMI_EXECUTION_MODE", "AUDIT")
    cfg = {"DRY_RUN": False, "AUDIT_MODE": False, "EXECUTION_MODE": "DELETE"}
    assert pd.apply_execution_mode(cfg) == "AUDIT"
    assert cfg["DRY_RUN"] is True  # env-driven AUDIT normalised the legacy flags
