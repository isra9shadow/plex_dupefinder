"""Tests for core.config (layered, typed configuration)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from core import config as cfgmod
from core.errors import ConfigError
from core.types import NotifyLevel, SafetyMode


def _write(tmp: Path, data: dict[str, object]) -> Path:
    f = tmp / "config.json"
    f.write_text(json.dumps(data), encoding="utf-8")
    return f


def test_defaults_when_no_file(tmp_path: Path) -> None:
    cfg = cfgmod.load(tmp_path / "missing.json", env={})
    assert cfg.mode is SafetyMode.DRY_RUN
    assert cfg.safety.retention_days == 7.0
    assert cfg.notify.level is NotifyLevel.FAIL


def test_json_overrides_defaults(tmp_path: Path) -> None:
    f = _write(
        tmp_path,
        {
            "safety": {"mode": "live", "retention_days": 2, "auto_purge": True},
            "logging": {"level": "DEBUG", "rotate_mb": 20},
            "reporting": {"metrics": False},
            "notify": {"enabled": True, "level": "all"},
            "pipelines": {"daily": ["a", "b"]},
            "paths": {"media": "/mnt/user/media"},
        },
    )
    cfg = cfgmod.load(f, env={})
    assert cfg.mode is SafetyMode.LIVE
    assert cfg.safety.retention_days == 2.0
    assert cfg.safety.auto_purge is True
    assert cfg.logging.rotate_mb == 20
    assert cfg.reporting.metrics is False
    assert cfg.notify.level is NotifyLevel.ALL
    assert cfg.pipelines["daily"] == ["a", "b"]
    assert cfg.paths["media"] == "/mnt/user/media"


def test_env_overrides_json(tmp_path: Path) -> None:
    f = _write(tmp_path, {"safety": {"mode": "live"}})
    cfg = cfgmod.load(f, env={"IZUMI_MODE": "audit"})
    assert cfg.mode is SafetyMode.AUDIT


def test_with_mode_does_not_mutate(tmp_path: Path) -> None:
    cfg = cfgmod.load(tmp_path / "x.json", env={})
    forced = cfgmod.with_mode(cfg, SafetyMode.LIVE)
    assert forced.mode is SafetyMode.LIVE
    assert cfg.mode is SafetyMode.DRY_RUN


def test_invalid_mode_raises(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        cfgmod.load(_write(tmp_path, {"safety": {"mode": "nope"}}), env={})


def test_invalid_env_mode_raises(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        cfgmod.load(tmp_path / "x.json", env={"IZUMI_MODE": "bogus"})


def test_invalid_level_raises(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        cfgmod.load(_write(tmp_path, {"notify": {"level": "loud"}}), env={})


@pytest.mark.parametrize(
    "data",
    [
        {"safety": {"retention_days": "lots"}},
        {"safety": {"mode": 5}},
        {"safety": {"auto_purge": "yes"}},
        {"logging": {"rotate_mb": "big"}},
        {"logging": {"dir": 5}},
        {"pipelines": {"daily": "notalist"}},
        {"pipelines": {"daily": [1, 2]}},
        {"paths": {"media": 5}},
        {"safety": "notobject"},
    ],
)
def test_bad_types_raise(tmp_path: Path, data: dict[str, object]) -> None:
    with pytest.raises(ConfigError):
        cfgmod.load(_write(tmp_path, data), env={})


def test_non_object_root_raises(tmp_path: Path) -> None:
    f = tmp_path / "c.json"
    f.write_text("[]", encoding="utf-8")
    with pytest.raises(ConfigError):
        cfgmod.load(f, env={})


def test_invalid_json_raises(tmp_path: Path) -> None:
    f = tmp_path / "c.json"
    f.write_text("{bad", encoding="utf-8")
    with pytest.raises(ConfigError):
        cfgmod.load(f, env={})
