"""Tests for the plex_dupefinder wrapper module (behavior-preserving integration)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest
from adapters.command import CommandResult
from core.types import SafetyMode
from modules.media import plex_dupefinder as pdf
from tests.fakes import make_context


def _fake_run(returncode: int, stderr: str = "", *, capture: dict | None = None) -> object:
    def runner(
        argv: Sequence[str], *, timeout: float = 0.0, env: Mapping[str, str] | None = None
    ) -> CommandResult:
        if capture is not None:
            capture["env"] = dict(env or {})
        return CommandResult(tuple(argv), returncode, "output", stderr)

    return runner


def test_invokes_and_succeeds(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(pdf.command, "run", _fake_run(0))
    result = pdf.run(make_context(tmp_path))
    assert result.ok
    assert result.module == "plex_dupefinder"


def test_dry_run_propagates_audit_mode(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    captured: dict = {}
    monkeypatch.setattr(pdf.command, "run", _fake_run(0, capture=captured))
    pdf.run(make_context(tmp_path, mode=SafetyMode.DRY_RUN))
    assert captured["env"]["IZUMI_EXECUTION_MODE"] == "AUDIT"


def test_live_propagates_quarantine_never_delete(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: dict = {}
    monkeypatch.setattr(pdf.command, "run", _fake_run(0, capture=captured))
    pdf.run(make_context(tmp_path, mode=SafetyMode.LIVE))
    # LIVE must map to QUARANTINE (reversible), never DELETE.
    assert captured["env"]["IZUMI_EXECUTION_MODE"] == "QUARANTINE"


def test_records_failure_on_nonzero(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(pdf.command, "run", _fake_run(2, "boom"))
    result = pdf.run(make_context(tmp_path))
    assert not result.ok
    assert result.failures[0].category == "integration"


def test_missing_script(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(pdf, "_SCRIPT", tmp_path / "does_not_exist.py")
    result = pdf.run(make_context(tmp_path))
    assert not result.ok
    assert result.failures[0].category == "config"
