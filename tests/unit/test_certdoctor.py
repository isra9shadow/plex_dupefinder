"""Tests for modules.ops.certdoctor (TLS expiry doctor, read-only)."""

from __future__ import annotations

import json
from pathlib import Path

from modules.ops import certdoctor
from modules.ops.certdoctor import Endpoint
from tests.fakes import make_context

_DAY = 86400.0


def _read_plan(tmp_path: Path) -> dict:
    return json.loads(
        (tmp_path / "reports" / "certdoctor" / "plan.json").read_text(encoding="utf-8")
    )


# --- evaluate (pure) -----------------------------------------------------------


def test_evaluate_ok() -> None:
    f = certdoctor.evaluate(Endpoint("plex", "h", 443), 1000 * _DAY, now=900 * _DAY, warn_days=21)
    assert f.status == "ok" and f.days_left == 100.0


def test_evaluate_expiring() -> None:
    f = certdoctor.evaluate(Endpoint("plex", "h", 443), 910 * _DAY, now=900 * _DAY, warn_days=21)
    assert f.status == "expiring" and 0 < f.days_left <= 21


def test_evaluate_expired() -> None:
    f = certdoctor.evaluate(Endpoint("plex", "h", 443), 890 * _DAY, now=900 * _DAY, warn_days=21)
    assert f.status == "expired" and f.days_left < 0


def test_evaluate_error_when_no_expiry() -> None:
    f = certdoctor.evaluate(Endpoint("plex", "h", 443), None, now=900 * _DAY, warn_days=21)
    assert f.status == "error"


# --- run -----------------------------------------------------------------------


def test_run_flags_expiring_and_records_failure(tmp_path: Path) -> None:
    ctx = make_context(
        tmp_path,
        integrations={
            "certdoctor": {
                "warn_days": 21,
                "endpoints": [
                    {"name": "plex", "host": "plex.local", "port": 443},
                    {"name": "sonarr", "host": "sonarr.local"},  # default port 443
                ],
            }
        },
    )
    # plex expires in 10 days (expiring), sonarr in 100 (ok).
    expiries = {"plex.local": 910 * _DAY, "sonarr.local": 1000 * _DAY}
    result = certdoctor.run(ctx, expiry=lambda h, p, t: expiries[h], now=900 * _DAY)

    assert result.metrics["checked"] == 2.0
    assert result.metrics["expiring"] == 1.0
    assert not result.ok and any("plex" in f.message for f in result.failures)
    statuses = {c["name"]: c["status"] for c in _read_plan(tmp_path)["certs"]}
    assert statuses == {"plex": "expiring", "sonarr": "ok"}


def test_run_unreachable_is_error_not_crash(tmp_path: Path) -> None:
    ctx = make_context(
        tmp_path,
        integrations={"certdoctor": {"endpoints": [{"name": "down", "host": "no.host"}]}},
    )

    def boom(host: str, port: int, timeout: float) -> float:
        raise OSError("connection refused")

    result = certdoctor.run(ctx, expiry=boom, now=900 * _DAY)
    assert result.metrics["checked"] == 1.0
    assert any("down" in f.message for f in result.failures)
    assert _read_plan(tmp_path)["certs"][0]["status"] == "error"


def test_run_no_endpoints_note(tmp_path: Path) -> None:
    ctx = make_context(tmp_path)
    result = certdoctor.run(ctx, expiry=lambda h, p, t: 0.0)
    assert result.ok
    assert "No endpoints configured" in _read_plan(tmp_path)["note"]
