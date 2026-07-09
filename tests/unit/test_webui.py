"""Tests for webui.py (read-only reports server — pure bits only)."""

from __future__ import annotations

import json
from pathlib import Path

import webui


def _write_plan(reports: Path, action: str, payload: dict) -> None:
    d = reports / action
    d.mkdir(parents=True)
    (d / "plan.json").write_text(json.dumps(payload), encoding="utf-8")


def test_humanize_reads_plan_all_ok(tmp_path: Path) -> None:
    _write_plan(
        tmp_path,
        "configcheck",
        {"total": 30, "missing_count": 0, "invalid_count": 0, "settings": []},
    )
    msg = webui.humanize_action_result("configcheck", 0, str(tmp_path))
    assert "todo correcto" in msg and "30" in msg


def test_humanize_lists_missing_and_invalid_keys(tmp_path: Path) -> None:
    _write_plan(
        tmp_path,
        "configcheck",
        {
            "total": 3,
            "missing_count": 2,
            "invalid_count": 1,
            "settings": [
                {"key": "PLEX_TOKEN", "status": "missing"},
                {"key": "RADARR_API_KEY", "status": "missing"},
                {"key": "radarr.url", "status": "invalid"},
            ],
        },
    )
    msg = webui.humanize_action_result("configcheck", 1, str(tmp_path))
    assert "PLEX_TOKEN" in msg and "RADARR_API_KEY" in msg and "radarr.url" in msg
    assert "rc=" not in msg  # never leak the raw exit code to a human


def test_humanize_fallback_without_plan(tmp_path: Path) -> None:
    assert "completado" in webui.humanize_action_result("uptime", 0, str(tmp_path))
    assert "incidencias" in webui.humanize_action_result("uptime", 1, str(tmp_path))


def test_parse_defaults() -> None:
    ns = webui._parse([])
    assert ns.port == 8888
    assert ns.host == "0.0.0.0"  # noqa: S104 - asserting the documented default
    assert ns.dir is None


def test_parse_overrides() -> None:
    ns = webui._parse(["--dir", "/x", "--port", "9000", "--host", "127.0.0.1"])
    assert (ns.dir, ns.port, ns.host) == ("/x", 9000, "127.0.0.1")


def test_default_reports_dir_returns_a_path() -> None:
    # Either the configured reporting.dir or the ./reports fallback — always a str.
    d = webui.default_reports_dir()
    assert isinstance(d, str) and d


def test_api_disabled_without_token() -> None:
    ok, code, msg = webui.check_request("health", "x", expected_token="")
    assert not ok and code == 403 and "deshabilitada" in msg


def test_api_rejects_bad_token() -> None:
    ok, code, _ = webui.check_request("health", "wrong", expected_token="secret")
    assert not ok and code == 403


def test_api_rejects_non_whitelisted_action() -> None:
    ok, code, msg = webui.check_request("shutdown", "secret", expected_token="secret")
    assert not ok and code == 400 and "no permitida" in msg  # unknown action never runs


def test_jobs_registry_tracks_progress() -> None:
    webui._JOBS.clear()
    jid = webui._new_job("dbcheck", dry_run=False)
    assert webui._JOBS[jid]["state"] == "queued"
    webui._update_job(jid, state="running", started=100.0, step=2, total=3, current="netdoctor")
    snap = webui.jobs_snapshot()
    job = next(j for j in snap if j["id"] == jid)
    assert job["state"] == "running" and job["total"] == 3 and job["current"] == "netdoctor"


def test_job_log_handler_captures_lines_into_snapshot() -> None:
    import logging

    webui._JOBS.clear()
    jid = webui._new_job("dbcheck", dry_run=False)
    handler = webui._JobLogHandler(jid)
    rec = logging.LogRecord("izumi.dbcheck", logging.INFO, __file__, 1, "scanning", None, None)
    rec.fields = {"db": "radarr"}  # type: ignore[attr-defined]
    handler.emit(rec)
    snap = next(j for j in webui.jobs_snapshot() if j["id"] == jid)
    assert snap["log"] == ["I scanning db=radarr"]
    webui._JOBS.clear()


def test_jobs_snapshot_newest_first_and_capped() -> None:
    webui._JOBS.clear()
    for i in range(25):
        jid = webui._new_job(f"m{i}", dry_run=False)
        webui._update_job(jid, started=float(i))
    snap = webui.jobs_snapshot()
    assert len(snap) == 20  # capped
    assert float(snap[0]["started"]) >= float(snap[-1]["started"])  # newest first
    webui._JOBS.clear()


def test_render_report_shows_summary_plan_and_back_link() -> None:
    out = webui.render_report("radarr_tagger", "# Radarr tagger\n> ERROR: x", '{"error": "x"}')
    assert "radarr_tagger" in out
    assert "Radarr tagger" in out and "ERROR: x" in out  # summary rendered (escaped)
    assert "plan.json" in out and "&quot;error&quot;" in out  # plan shown, escaped
    assert 'href="/"' in out  # back-to-panel link


def test_git_pull_reports_update_and_asks_for_restart() -> None:
    heads = iter(["aaa1111", "bbb2222"])  # before, after

    def runner(args: list[str]) -> tuple[int, str] | None:
        if args[0] == "rev-parse":
            return 0, next(heads) + "\n"
        return 0, ""  # fetch / pull ok

    ok, msg = webui.git_pull("/app", runner=runner)
    assert ok and "aaa1111 → bbb2222" in msg and "reinicia izumi-webui" in msg


def test_git_pull_up_to_date() -> None:
    def runner(args: list[str]) -> tuple[int, str] | None:
        return (0, "same999\n") if args[0] == "rev-parse" else (0, "")

    ok, msg = webui.git_pull("/app", runner=runner)
    assert ok and "ya al día" in msg


def test_git_pull_offline_and_missing_git() -> None:
    def offline(args: list[str]) -> tuple[int, str] | None:
        return (1, "") if args[0] == "fetch" else (0, "")

    ok, msg = webui.git_pull("/app", runner=offline)
    assert not ok and "sin conexión" in msg

    ok2, msg2 = webui.git_pull("/app", runner=lambda a: None)  # git binary missing
    assert not ok2 and "git no está disponible" in msg2


def test_api_allows_acting_modules() -> None:
    # Acting modules are whitelisted (they run dry-run by default; live only when the
    # client asks after confirming). The apply allow-list still gates raw commands.
    for act in ("organizer", "extractor", "plex_dupefinder", "dbrepair", "plexrefresh"):
        ok, code, _ = webui.check_request(act, "secret", expected_token="secret")
        assert ok and code == 200, act


def test_api_allows_readonly_action_with_token() -> None:
    ok, code, _ = webui.check_request("health", "secret", expected_token="secret")
    assert ok and code == 200


def test_check_token_ask_apply() -> None:
    assert webui.check_token("s", expected_token="s")[0] is True
    assert webui.check_token("x", expected_token="s")[0] is False
    assert webui.check_token("s", expected_token="")[0] is False  # API off


def test_basic_auth_disabled_when_unset() -> None:
    assert webui.check_basic_auth("", "", "") is True  # no user/pass → auth off


def test_basic_auth_validates_credentials() -> None:
    import base64

    good = "Basic " + base64.b64encode(b"isra:secret").decode()
    assert webui.check_basic_auth(good, "isra", "secret") is True
    bad = "Basic " + base64.b64encode(b"isra:wrong").decode()
    assert webui.check_basic_auth(bad, "isra", "secret") is False
    assert webui.check_basic_auth("", "isra", "secret") is False  # missing header
    assert webui.check_basic_auth("Bearer x", "isra", "secret") is False  # wrong scheme


def test_status_snapshot_empty_when_no_db(tmp_path) -> None:
    snap = webui.status_snapshot(str(tmp_path))
    assert snap == {"ok": True, "modules": [], "failing": []}


def test_status_snapshot_reports_failing_modules(tmp_path) -> None:
    from core.metrics import MetricsStore

    db = tmp_path / "cache" / "metrics.db"
    with MetricsStore(db) as store:
        store.record("r1", "uptime", {"down": 1.0}, ok=False, failures=2)
        store.record("r1", "diskwatch", {"temp": 40.0}, ok=True, failures=0)
    snap = webui.status_snapshot(str(tmp_path))
    assert snap["ok"] is False
    assert snap["failing"] == ["uptime"]
    mods = {m["module"]: m for m in snap["modules"]}  # type: ignore[union-attr]
    assert mods["uptime"]["failures"] == 2 and mods["uptime"]["ok"] is False
    assert mods["diskwatch"]["ok"] is True


def test_export_markdown_concatenates_summaries(tmp_path) -> None:
    (tmp_path / "uptime").mkdir()
    (tmp_path / "uptime" / "summary.md").write_text("servicios ok", encoding="utf-8")
    (tmp_path / "cache").mkdir()  # infra dir, skipped
    (tmp_path / "cache" / "summary.md").write_text("x", encoding="utf-8")
    out = webui.export_markdown(str(tmp_path))
    assert out.startswith("# izumi")
    assert "## uptime" in out and "servicios ok" in out
    assert "## cache" not in out  # infra dir excluded
