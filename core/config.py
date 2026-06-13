"""Layered, typed configuration: built-in defaults -> config.json -> ENV overrides.

Configuration is a tree of frozen dataclasses (the dataclasses ARE the schema, so
no runtime jsonschema). Parsing is explicit and fails closed with ``ConfigError``.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path

from core.errors import ConfigError
from core.types import NotifyLevel, SafetyMode

_DEFAULT_CONFIG_PATH = Path("config/config.json")


@dataclass(frozen=True)
class SafetyConfig:
    mode: SafetyMode = SafetyMode.DRY_RUN
    audit: bool = False
    min_file_age_hours: float = 24.0
    stability_check_seconds: float = 2.0
    max_size_ratio: float = 5.0
    quarantine_dir: Path = Path("/mnt/user/Temp/izumi_quarantine")
    retention_days: float = 7.0
    auto_purge: bool = False


@dataclass(frozen=True)
class LoggingConfig:
    level: str = "INFO"
    dir: Path = Path("/mnt/cache/appdata/izumi/logs")
    rotate_mb: int = 10
    backups: int = 5


@dataclass(frozen=True)
class ReportingConfig:
    dir: Path = Path("/mnt/cache/appdata/izumi/reports")
    metrics: bool = True


@dataclass(frozen=True)
class NotifyConfig:
    enabled: bool = False
    level: NotifyLevel = NotifyLevel.FAIL
    provider: str = "telegram"
    token_ref: str = "IZUMI_TELEGRAM_BOT_TOKEN"
    chat_id_ref: str = "IZUMI_TELEGRAM_CHAT_ID"


@dataclass(frozen=True)
class Config:
    safety: SafetyConfig = field(default_factory=SafetyConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    reporting: ReportingConfig = field(default_factory=ReportingConfig)
    notify: NotifyConfig = field(default_factory=NotifyConfig)
    paths: dict[str, str] = field(default_factory=dict)
    pipelines: dict[str, list[str]] = field(default_factory=dict)
    integrations: dict[str, dict[str, object]] = field(default_factory=dict)

    @property
    def mode(self) -> SafetyMode:
        return self.safety.mode


# --- typed parsing helpers (fail closed) ---------------------------------------

def _section(data: Mapping[str, object], key: str) -> Mapping[str, object]:
    value = data.get(key, {})
    if not isinstance(value, Mapping):
        raise ConfigError(f"config section '{key}' must be an object")
    return value


def _str(d: Mapping[str, object], key: str, default: str) -> str:
    value = d.get(key, default)
    if not isinstance(value, str):
        raise ConfigError(f"'{key}' must be a string")
    return value


def _bool(d: Mapping[str, object], key: str, default: bool) -> bool:
    value = d.get(key, default)
    if not isinstance(value, bool):
        raise ConfigError(f"'{key}' must be a boolean")
    return value


def _num(d: Mapping[str, object], key: str, default: float) -> float:
    value = d.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ConfigError(f"'{key}' must be a number")
    return float(value)


def _int(d: Mapping[str, object], key: str, default: int) -> int:
    value = d.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"'{key}' must be an integer")
    return value


def _path(d: Mapping[str, object], key: str, default: Path) -> Path:
    value = d.get(key)
    if value is None:
        return default
    if not isinstance(value, str):
        raise ConfigError(f"'{key}' must be a path string")
    return Path(value)


def _enum_mode(raw: str) -> SafetyMode:
    try:
        return SafetyMode(raw)
    except ValueError as exc:
        raise ConfigError(f"invalid safety.mode: {raw}") from exc


def _enum_level(raw: str) -> NotifyLevel:
    try:
        return NotifyLevel(raw)
    except ValueError as exc:
        raise ConfigError(f"invalid notify.level: {raw}") from exc


def _parse_safety(d: Mapping[str, object]) -> SafetyConfig:
    base = SafetyConfig()
    return SafetyConfig(
        mode=_enum_mode(_str(d, "mode", base.mode.value)),
        audit=_bool(d, "audit", base.audit),
        min_file_age_hours=_num(d, "min_file_age_hours", base.min_file_age_hours),
        stability_check_seconds=_num(d, "stability_check_seconds", base.stability_check_seconds),
        max_size_ratio=_num(d, "max_size_ratio", base.max_size_ratio),
        quarantine_dir=_path(d, "quarantine_dir", base.quarantine_dir),
        retention_days=_num(d, "retention_days", base.retention_days),
        auto_purge=_bool(d, "auto_purge", base.auto_purge),
    )


def _parse_logging(d: Mapping[str, object]) -> LoggingConfig:
    base = LoggingConfig()
    return LoggingConfig(
        level=_str(d, "level", base.level),
        dir=_path(d, "dir", base.dir),
        rotate_mb=_int(d, "rotate_mb", base.rotate_mb),
        backups=_int(d, "backups", base.backups),
    )


def _parse_reporting(d: Mapping[str, object]) -> ReportingConfig:
    base = ReportingConfig()
    return ReportingConfig(
        dir=_path(d, "dir", base.dir),
        metrics=_bool(d, "metrics", base.metrics),
    )


def _parse_notify(d: Mapping[str, object]) -> NotifyConfig:
    base = NotifyConfig()
    return NotifyConfig(
        enabled=_bool(d, "enabled", base.enabled),
        level=_enum_level(_str(d, "level", base.level.value)),
        provider=_str(d, "provider", base.provider),
        token_ref=_str(d, "token_ref", base.token_ref),
        chat_id_ref=_str(d, "chat_id_ref", base.chat_id_ref),
    )


def _parse_paths(d: Mapping[str, object]) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in d.items():
        if not isinstance(value, str):
            raise ConfigError(f"paths.{key} must be a string")
        out[str(key)] = value
    return out


def _parse_pipelines(d: Mapping[str, object]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for key, value in d.items():
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise ConfigError(f"pipelines.{key} must be a list of strings")
        out[str(key)] = [str(item) for item in value]
    return out


def _parse_integrations(d: Mapping[str, object]) -> dict[str, dict[str, object]]:
    out: dict[str, dict[str, object]] = {}
    for service, settings in d.items():
        if not isinstance(settings, Mapping):
            raise ConfigError(f"integrations.{service} must be an object")
        out[str(service)] = {str(k): v for k, v in settings.items()}
    return out


def _from_dict(data: Mapping[str, object]) -> Config:
    return Config(
        safety=_parse_safety(_section(data, "safety")),
        logging=_parse_logging(_section(data, "logging")),
        reporting=_parse_reporting(_section(data, "reporting")),
        notify=_parse_notify(_section(data, "notify")),
        paths=_parse_paths(_section(data, "paths")),
        pipelines=_parse_pipelines(_section(data, "pipelines")),
        integrations=_parse_integrations(_section(data, "integrations")),
    )


def _apply_env(cfg: Config, env: Mapping[str, str]) -> Config:
    mode_raw = env.get("IZUMI_MODE")
    if mode_raw:
        return with_mode(cfg, _enum_mode(mode_raw))
    return cfg


def with_mode(cfg: Config, mode: SafetyMode) -> Config:
    """Return a copy of ``cfg`` forced to ``mode`` (used by --dry-run/--audit)."""
    return replace(cfg, safety=replace(cfg.safety, mode=mode))


def load(path: Path | None = None, *, env: Mapping[str, str] | None = None) -> Config:
    """Load defaults, overlay ``config.json`` (if present), then ENV. Fails closed."""
    path = path or _DEFAULT_CONFIG_PATH
    data: dict[str, object] = {}
    if path.is_file():
        try:
            loaded: object = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ConfigError(f"invalid JSON in {path}: {exc}") from exc
        if not isinstance(loaded, dict):
            raise ConfigError("config root must be a JSON object")
        data = {str(k): v for k, v in loaded.items()}
    return _apply_env(_from_dict(data), env if env is not None else os.environ)
