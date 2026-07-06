"""Tests for modules.ops.notifypush (proactive Telegram digest of module reports)."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from core.types import RunContext, SafetyMode
from modules.ops import notifypush
from tests.fakes import make_context


def _write_summary(tmp_path: Path, subdir: str, text: str) -> None:
    d = tmp_path / "reports" / subdir
    d.mkdir(parents=True, exist_ok=True)
    (d / "summary.md").write_text(text, encoding="utf-8")


def _read_plan(tmp_path: Path) -> dict[str, object]:
    data = json.loads(
        (tmp_path / "reports" / "notifypush" / "plan.json").read_text(encoding="utf-8")
    )
    assert isinstance(data, dict)
    return data


def _enable_notify(ctx: RunContext) -> RunContext:
    cfg = replace(ctx.config, notify=replace(ctx.config.notify, enabled=True))
    return replace(ctx, config=cfg)


# --- pure logic ----------------------------------------------------------------


def test_build_digest_with_sections() -> None:
    text = notifypush.build_digest(
        [("uptime", "todo arriba"), ("dbcheck", "1 corrupta")],
        title="izumi",
        when="2026-07-06 03:00",
    )
    assert "izumi — 2026-07-06 03:00" in text
    assert "═══ uptime ═══" in text
    assert "1 corrupta" in text


def test_build_digest_empty() -> None:
    text = notifypush.build_digest([], title="izumi", when="now")
    assert "sin informes recientes" in text


def test_truncate_marks_the_cut() -> None:
    assert notifypush._truncate("abcdef", 10) == "abcdef"
    clipped = notifypush._truncate("x" * 50, 10)
    assert clipped.startswith("x" * 10)
    assert "truncado" in clipped


def test_collect_sections_skips_missing_and_truncates(tmp_path: Path) -> None:
    _write_summary(tmp_path, "uptime", "servicios ok")
    _write_summary(tmp_path, "dbcheck", "y" * 100)
    sections = notifypush.collect_sections(
        tmp_path / "reports", ("uptime", "diskwatch", "dbcheck"), max_chars=20
    )
    names = [s for s, _ in sections]
    assert names == ["uptime", "dbcheck"]  # diskwatch missing → skipped
    assert "truncado" in dict(sections)["dbcheck"]


# --- run: dry-run --------------------------------------------------------------


def test_dry_run_composes_but_never_sends(tmp_path: Path) -> None:
    _write_summary(tmp_path, "uptime", "servicios ok")
    calls: list[tuple[str, str, str]] = []

    ctx = make_context(tmp_path)  # DRY_RUN default
    result = notifypush.run(
        ctx,
        sender=lambda t, c, x: calls.append((t, c, x)) or True,
        now="2026-07-06 03:00",
    )

    assert result.actions == 0
    assert result.metrics["sent"] == 0.0
    assert result.metrics["sections"] == 1.0
    assert calls == []  # never sent
    plan = _read_plan(tmp_path)
    assert plan["sent"] is False
    assert plan["dry_run"] is True


# --- run: live -----------------------------------------------------------------


def test_live_disabled_notify_does_not_send(tmp_path: Path) -> None:
    _write_summary(tmp_path, "uptime", "ok")
    calls: list[str] = []
    ctx = make_context(tmp_path, mode=SafetyMode.LIVE)  # notify.enabled False by default
    result = notifypush.run(ctx, sender=lambda t, c, x: calls.append(x) or True)

    assert result.metrics["sent"] == 0.0
    assert calls == []
    assert "notify.enabled=false" in _read_plan(tmp_path)["note"]  # type: ignore[operator]


def test_live_missing_secrets_records_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_summary(tmp_path, "uptime", "ok")
    monkeypatch.delenv("IZUMI_TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("IZUMI_TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.setenv("IZUMI_ENV_FILE", str(tmp_path / "no.env"))  # no .env fallback
    import core.secrets as secrets

    secrets.reset_cache()

    ctx = _enable_notify(make_context(tmp_path, mode=SafetyMode.LIVE))
    result = notifypush.run(ctx, sender=lambda t, c, x: True)

    assert not result.ok
    assert result.metrics["sent"] == 0.0
    assert any("faltan secretos" in f.message for f in result.failures)


def test_live_sends_when_enabled_and_secrets_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_summary(tmp_path, "uptime", "servicios ok")
    _write_summary(tmp_path, "dbcheck", "todo sano")
    monkeypatch.setenv("IZUMI_TELEGRAM_BOT_TOKEN", "T0KEN")
    monkeypatch.setenv("IZUMI_TELEGRAM_CHAT_ID", "12345")
    import core.secrets as secrets

    secrets.reset_cache()

    sent: list[tuple[str, str, str]] = []

    def sender(token: str, chat_id: str, text: str) -> bool:
        sent.append((token, chat_id, text))
        return True

    ctx = _enable_notify(make_context(tmp_path, mode=SafetyMode.LIVE))
    result = notifypush.run(ctx, sender=sender, now="2026-07-06 03:00")

    assert result.ok
    assert result.actions == 1
    assert result.metrics["sent"] == 1.0
    assert len(sent) == 1
    token, chat_id, text = sent[0]
    assert (token, chat_id) == ("T0KEN", "12345")
    assert "servicios ok" in text and "todo sano" in text
    assert _read_plan(tmp_path)["sent"] is True


def test_live_send_failure_is_recorded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_summary(tmp_path, "uptime", "ok")
    monkeypatch.setenv("IZUMI_TELEGRAM_BOT_TOKEN", "T0KEN")
    monkeypatch.setenv("IZUMI_TELEGRAM_CHAT_ID", "12345")
    import core.secrets as secrets

    secrets.reset_cache()

    ctx = _enable_notify(make_context(tmp_path, mode=SafetyMode.LIVE))
    result = notifypush.run(ctx, sender=lambda t, c, x: False)

    assert not result.ok
    assert result.metrics["sent"] == 0.0
    assert any("no confirmó" in f.message for f in result.failures)
