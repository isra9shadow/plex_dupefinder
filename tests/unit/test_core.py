"""Unit tests for the Sprint 0 core foundations (errors, types, secrets)."""

from __future__ import annotations

from pathlib import Path

import pytest
from core import errors, secrets, types


def test_error_hierarchy_and_category() -> None:
    assert issubclass(errors.ConfigError, errors.IzumiError)
    assert errors.SecretError("missing").category == "secret"
    assert errors.IzumiError("x", category="custom").category == "custom"
    assert errors.IzumiError("plain").category == "general"


def test_safety_mode_default_is_dry_run() -> None:
    assert types.SafetyMode.default() is types.SafetyMode.DRY_RUN
    assert types.SafetyMode.DRY_RUN.value == "dry_run"
    assert types.Verdict.BAD.value == "bad"


def test_module_result_ok_flips_with_failures() -> None:
    result = types.ModuleResult(module="demo", run_id="r1", mode=types.SafetyMode.DRY_RUN)
    assert result.ok is True
    result.add_failure(types.FailureRecord(category="safety", message="boom"))
    assert result.ok is False
    assert result.failures[0].category == "safety"


def test_secret_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    secrets.reset_cache()
    monkeypatch.setenv("IZUMI_TEST_SECRET", "value")
    assert secrets.require("IZUMI_TEST_SECRET") == "value"


def test_required_secret_missing_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    secrets.reset_cache()
    monkeypatch.delenv("IZUMI_TEST_SECRET", raising=False)
    monkeypatch.setattr(secrets, "_ENV_FILE", tmp_path / "missing.env")
    with pytest.raises(errors.SecretError):
        secrets.require("IZUMI_TEST_SECRET")


def test_optional_secret_returns_default(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    secrets.reset_cache()
    monkeypatch.delenv("IZUMI_TEST_SECRET", raising=False)
    monkeypatch.setattr(secrets, "_ENV_FILE", tmp_path / "missing.env")
    assert secrets.get_secret("IZUMI_TEST_SECRET", default="fallback", required=False) == "fallback"


def test_secret_from_dotenv_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    secrets.reset_cache()
    monkeypatch.delenv("IZUMI_TEST_SECRET", raising=False)
    env = tmp_path / ".env"
    env.write_text(
        'IZUMI_TEST_SECRET="from-file"\n# a comment\nNOT_AN_ASSIGNMENT\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(secrets, "_ENV_FILE", env)
    assert secrets.require("IZUMI_TEST_SECRET") == "from-file"
    secrets.reset_cache()
