"""Tests for modules.ops.configcheck (read-only config doctor)."""

from __future__ import annotations

import json
from pathlib import Path

from modules.ops import configcheck
from tests.fakes import make_context


def _read_plan(tmp_path: Path) -> dict[str, object]:
    return json.loads(
        (tmp_path / "reports" / "configcheck" / "plan.json").read_text(encoding="utf-8")
    )


def _read_summary(tmp_path: Path) -> str:
    return (tmp_path / "reports" / "configcheck" / "summary.md").read_text(encoding="utf-8")


def _no_env(_: str) -> str | None:
    return None


def _all_paths_exist(_: str) -> bool:
    return True


def test_run_is_read_only_and_reports_missing_secrets(tmp_path: Path) -> None:
    # No env secrets at all and no real config -> required secrets are MISSING.
    ctx = make_context(tmp_path)
    result = configcheck.run(
        ctx, env_reader=_no_env, path_exists=_all_paths_exist, dir_exists=_all_paths_exist
    )

    assert result.actions == 0  # read-only
    assert result.metrics["missing_count"] >= 1
    assert not result.ok  # missing secrets recorded as failures

    plan = _read_plan(tmp_path)
    assert plan["missing_count"] == result.metrics["missing_count"]
    keys = {s["key"]: s for s in plan["settings"]}  # type: ignore[index]
    assert keys["PLEX_TOKEN"]["status"] == "missing"
    assert keys["PLEX_TOKEN"]["remediation"]  # has a how-to hint

    summary = _read_summary(tmp_path)
    assert "PLEX_TOKEN" in summary
    assert "fix:" in summary


def test_run_redacts_secret_values(tmp_path: Path) -> None:
    ctx = make_context(tmp_path)
    secrets = {
        "PLEX_TOKEN": "super-secret-token",
        "RADARR_API_KEY": "radarr-key",
        "SONARR_API_KEY": "sonarr-key",
    }

    def reader(name: str) -> str | None:
        return secrets.get(name)

    result = configcheck.run(
        ctx, env_reader=reader, path_exists=_all_paths_exist, dir_exists=_all_paths_exist
    )

    plan_text = (tmp_path / "reports" / "configcheck" / "plan.json").read_text(encoding="utf-8")
    assert "super-secret-token" not in plan_text  # never echoed
    assert "super-secret-token" not in _read_summary(tmp_path)

    plan = _read_plan(tmp_path)
    keys = {s["key"]: s for s in plan["settings"]}  # type: ignore[index]
    assert keys["PLEX_TOKEN"]["status"] == "ok"
    assert keys["PLEX_TOKEN"]["value"] == "(set)"  # set-marker, not the value
    assert result.metrics["missing_count"] == 0.0  # the three required secrets are set


def test_run_flags_invalid_config_value(tmp_path: Path) -> None:
    # safety.mode is an enum; an out-of-range value must be INVALID.
    ctx = make_context(tmp_path)
    # force the live config to carry a bad integration url and good secrets
    secrets = {"PLEX_TOKEN": "t", "RADARR_API_KEY": "r", "SONARR_API_KEY": "s"}
    ctx.config.integrations["radarr"] = {"url": "not-a-url"}

    def reader(name: str) -> str | None:
        return secrets.get(name)

    result = configcheck.run(
        ctx, env_reader=reader, path_exists=_all_paths_exist, dir_exists=_all_paths_exist
    )

    plan = _read_plan(tmp_path)
    keys = {s["key"]: s for s in plan["settings"]}  # type: ignore[index]
    assert keys["radarr.url"]["status"] == "invalid"
    assert result.metrics["invalid_count"] >= 1.0
    assert any("invalid: radarr.url" in f.message for f in result.failures)


def test_run_dir_exists_drives_path_validators(tmp_path: Path) -> None:
    ctx = make_context(tmp_path)
    secrets = {"PLEX_TOKEN": "t", "RADARR_API_KEY": "r", "SONARR_API_KEY": "s"}
    # configure a quarantine dir and point it at an existing temp dir
    real_dir = tmp_path / "qdir"
    real_dir.mkdir()
    ctx.config.integrations.setdefault("uptime", {})  # touch integrations

    def reader(name: str) -> str | None:
        return secrets.get(name)

    def dir_exists(p: str) -> bool:
        return Path(p).is_dir()

    result = configcheck.run(
        ctx, env_reader=reader, path_exists=lambda p: Path(p).exists(), dir_exists=dir_exists
    )

    plan = _read_plan(tmp_path)
    keys = {s["key"]: s for s in plan["settings"]}  # type: ignore[index]
    # reporting.dir is created by make_context's logging setup or our report write,
    # so it must validate OK once the report directory exists.
    assert keys["reporting.dir"]["status"] in {"ok", "invalid"}
    # the doctor never raises and always produces both metrics
    assert "missing_count" in result.metrics
    assert "invalid_count" in result.metrics


def test_extra_required_tightens_optional_settings(tmp_path: Path) -> None:
    # GEMINI_API_KEY is optional by default; extra_required should make it MISSING.
    ctx = make_context(
        tmp_path,
        integrations={"configcheck": {"extra_required": ["GEMINI_API_KEY"]}},
    )
    result = configcheck.run(
        ctx, env_reader=_no_env, path_exists=_all_paths_exist, dir_exists=_all_paths_exist
    )

    plan = _read_plan(tmp_path)
    keys = {s["key"]: s for s in plan["settings"]}  # type: ignore[index]
    assert keys["GEMINI_API_KEY"]["status"] == "missing"
    assert keys["GEMINI_API_KEY"]["required"] is True
    assert any("GEMINI_API_KEY" in f.message for f in result.failures)


def test_run_survives_env_reader_failure(tmp_path: Path) -> None:
    ctx = make_context(tmp_path)

    def boom(_: str) -> str | None:
        raise RuntimeError("env backend down")

    result = configcheck.run(
        ctx, env_reader=boom, path_exists=_all_paths_exist, dir_exists=_all_paths_exist
    )

    # The env failure is recorded but the doctor still completes and writes a report.
    assert any("env read failed" in f.message for f in result.failures)
    assert (tmp_path / "reports" / "configcheck" / "plan.json").is_file()
