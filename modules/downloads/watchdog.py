"""Downloads watchdog (report-only): detect stalled / inactive torrents.

Parity target: legacy ``MonitorizarDescargasAtascadas.sh``. **Report-only** — lists
stalled torrents; ARR blocklist / qBit ``failed`` tag actions are opt-in only after
a parity window (MIGRATION_PLAN §9).
"""

from __future__ import annotations

import json

from core import secrets
from core.errors import ConfigError, IntegrationError, SecretError
from core.registry import register
from core.types import FailureRecord, ModuleResult, RunContext
from integrations.qbittorrent import QbitClient

_STALLED_STATE = "stalledDL"
_DEFAULT_STALLED_HOURS = 48.0


def _num(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    return float(value) if isinstance(value, int | float) else None


def _str(torrent: dict[str, object], key: str) -> str:
    value = torrent.get(key)
    return value if isinstance(value, str) else ""


def is_stalled(torrent: dict[str, object], min_seconds: float) -> bool:
    if torrent.get("state") == _STALLED_STATE:
        return True
    active = _num(torrent.get("time_active"))
    return (
        active is not None
        and active >= min_seconds
        and _num(torrent.get("dlspeed")) == 0
        and _num(torrent.get("upspeed")) == 0
    )


def category_of(value: object) -> str:
    category = value if isinstance(value, str) else ""
    if category in ("tv-sonarr", "sonarr"):
        return "sonarr"
    if category == "radarr":
        return "radarr"
    return "unknown"


def _qbit(ctx: RunContext) -> QbitClient:
    settings = ctx.config.integrations.get("qbittorrent", {})
    url = settings.get("url")
    user_ref = settings.get("username_ref")
    pass_ref = settings.get("password_ref")
    if not (isinstance(url, str) and isinstance(user_ref, str) and isinstance(pass_ref, str)):
        raise ConfigError("qbittorrent needs url, username_ref, password_ref")
    return QbitClient(url, secrets.require(user_ref), secrets.require(pass_ref))


def _stalled_hours(ctx: RunContext) -> float:
    value = ctx.config.integrations.get("qbittorrent", {}).get("stalled_hours")
    if isinstance(value, int | float) and not isinstance(value, bool):
        return float(value)
    return _DEFAULT_STALLED_HOURS


def _write_report(ctx: RunContext, stalled: list[dict[str, str]]) -> None:
    out_dir = ctx.config.reporting.dir / "downloads"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "stalled.json").write_text(
        json.dumps(stalled, indent=2, sort_keys=True), encoding="utf-8"
    )
    lines = [
        "# Stalled downloads (report-only)",
        "",
        f"_{len(stalled)} stalled_",
        "",
        *[f"- [{t['category']}] {t['state']} — {t['name']}" for t in stalled],
        "",
    ]
    (out_dir / "stalled.md").write_text("\n".join(lines), encoding="utf-8")


@register("downloads_watchdog")
def run(ctx: RunContext) -> ModuleResult:
    result = ModuleResult(module="downloads_watchdog", run_id=ctx.run_id, mode=ctx.mode)
    try:
        torrents = _qbit(ctx).torrents()
    except (ConfigError, SecretError) as exc:
        result.add_failure(FailureRecord(category="config", message=str(exc)))
        return result
    except IntegrationError as exc:
        result.add_failure(FailureRecord(category="integration", message=str(exc)))
        return result

    min_seconds = _stalled_hours(ctx) * 3600.0
    stalled = [
        {
            "name": _str(torrent, "name"),
            "hash": _str(torrent, "hash"),
            "state": _str(torrent, "state"),
            "category": category_of(torrent.get("category")),
        }
        for torrent in torrents
        if is_stalled(torrent, min_seconds)
    ]
    _write_report(ctx, stalled)
    ctx.logger.info("downloads watchdog (report-only)", stalled=len(stalled), total=len(torrents))
    result.actions = len(stalled)
    return result
