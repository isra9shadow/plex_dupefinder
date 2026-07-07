"""Tests for modules.ops.webdashboard (clear HTML health panel)."""

from __future__ import annotations

from pathlib import Path

from core.metrics import MetricsStore
from modules.ops import webdashboard
from tests.fakes import make_context


def test_sparkline_flat_and_series() -> None:
    assert "<line" in webdashboard.sparkline_svg([5.0])
    assert "<polyline" in webdashboard.sparkline_svg([1.0, 2.0, 3.0])


def test_md_lite_headings_bullets_and_escape() -> None:
    out = webdashboard.md_lite("# Título\n- uno\n- dos\ntexto <x>")
    assert "<strong>Título</strong>" in out
    assert "<ul><li>uno</li><li>dos</li></ul>" in out
    assert "&lt;x&gt;" in out and "<x>" not in out


def test_render_has_kpis_ai_filter_and_severity() -> None:
    status = [
        {"module": "dbcheck", "ok": False, "failures": 1, "ts": "t"},
        {"module": "uptime", "ok": True, "failures": 0, "ts": "t"},
    ]
    cards = [("dbcheck", "# Dbcheck\n- 1 corrupta"), ("uptime", "todo arriba")]
    out = webdashboard.render_html(
        status, {}, cards, ai_summary="Todo bien salvo dbcheck.", generated="now"
    )
    assert "con fallos" in out and 'class="kpi' in out  # KPI row
    assert "🧠" in out and "Todo bien salvo dbcheck" in out  # AI summary
    assert "id=q" in out  # filter box
    assert '<div class="card bad" data-name="dbcheck">' in out  # severity colour
    assert '<div class="card good" data-name="uptime">' in out
    assert "id=age" in out  # smart auto-refresh (no full meta-refresh flicker)


def test_render_bad_card_has_ai_fix_button() -> None:
    status = [{"module": "dbcheck", "ok": False, "failures": 1, "ts": "t"}]
    out = webdashboard.render_html(status, {}, [("dbcheck", "# Dbcheck\n- 1 corrupta")], generated="n")
    assert 'class="fixai" data-mod="dbcheck"' in out  # per-card AI fix button
    assert 'id="ai-dbcheck"' in out  # inline answer target
    assert "function fixcmd(" in out and "applyCmd(cmd,ab)" in out  # one-click apply of the fix


def test_render_good_card_has_no_ai_fix_button() -> None:
    status = [{"module": "uptime", "ok": True, "failures": 0, "ts": "t"}]
    out = webdashboard.render_html(status, {}, [("uptime", "todo arriba")], generated="n")
    assert 'data-mod="uptime"' not in out  # healthy cards don't offer a fix button


def test_render_empty() -> None:
    assert "Sin informes todavía" in webdashboard.render_html([], {}, [], generated="now")


def test_line_chart_has_hover_points() -> None:
    svg = webdashboard.line_chart_svg([1.0, 3.0, 2.0])
    assert svg.count('class="pt"') == 3  # one hoverable dot per point
    assert 'data-v="3"' in svg


def test_render_incidents_timeline() -> None:
    inc = [
        {"module": "dbcheck", "title": "DB corruption", "status": "open", "last_seen": 0.0},
        {"module": "uptime", "title": "svc down", "status": "resolved", "last_seen": 0.0},
    ]
    out = webdashboard.render_incidents(inc, nowsec=100.0)
    assert "Incidencias" in out
    assert '<span class="dot bad">' in out and "abierto" in out  # open incident
    assert '<span class="dot good">' in out and "resuelto" in out


def test_render_full_page_has_trends_incidents_branding() -> None:
    status = [{"module": "diskwatch", "ok": True, "failures": 0, "ts": "t"}]
    sparks = {"diskwatch": ("temp", [40.0, 42.0, 45.0])}
    inc = [{"module": "dbcheck", "title": "x", "status": "open", "last_seen": 0.0}]
    out = webdashboard.render_html(
        status,
        sparks,
        [("diskwatch", "ok")],
        inc,
        title="Homelab de Isra",
        accent="#e34948",
        nowsec=100.0,
        generated="now",
    )
    assert "Tendencias" in out and 'class="pt"' in out  # trend chart
    assert "Incidencias" in out
    assert "Homelab de Isra" in out and "#e34948" in out  # branding title + accent
    assert "rel=icon" in out and "id=age" in out  # favicon + smart refresh


def test_render_fixes_chat_and_export() -> None:
    out = webdashboard.render_html(
        [],
        {},
        [("uptime", "ok")],
        None,
        [("docker restart sonarr", "container down", "warning")],
        generated="now",
    )
    assert "Arreglos sugeridos" in out
    assert 'data-cmd="docker restart sonarr"' in out  # apply button
    assert "id=asb" in out and "id=ask" in out  # chat box
    assert "/api/export" in out  # export link


def test_card_severity_from_content_and_metrics() -> None:
    assert webdashboard.card_severity("todo ok", "good") == "good"
    assert webdashboard.card_severity("anything", "bad") == "bad"  # metrics authoritative
    assert webdashboard.card_severity("1 base corrupta", "") == "bad"  # bad word
    assert webdashboard.card_severity("Containers with errors: 17", "good") == "warn"  # count>0
    assert webdashboard.card_severity("errors: 0", "") == ""  # zero counts → neutral


def test_render_groups_cards_into_collapsible_sections() -> None:
    cards = [("uptime", "ok"), ("logwatch", "errors: 5"), ("organizer", "plan")]
    out = webdashboard.render_html([], {}, cards, generated="now")
    assert "<details open><summary>Salud (1)" in out  # uptime under Salud
    assert "IA &amp; avisos (1)" in out  # logwatch under IA
    assert "Media (1)" in out  # organizer under Media
    assert 'class="card warn"' in out  # logwatch errors:5 → amber


def test_run_writes_per_module_page(tmp_path: Path) -> None:
    reports = tmp_path / "reports"
    (reports / "dbcheck").mkdir(parents=True, exist_ok=True)
    (reports / "dbcheck" / "summary.md").write_text("# Dbcheck\n- 1 corrupta", encoding="utf-8")
    (reports / "dbcheck" / "plan.json").write_text('{"corrupt_count": 1}', encoding="utf-8")
    ctx = make_context(tmp_path)
    webdashboard.run(ctx, llm=lambda p: "", now="now")
    page = (reports / "dbcheck" / "index.html").read_text(encoding="utf-8")
    assert "volver al panel" in page and "corrupt_count" in page


def test_run_caches_ai_summary(tmp_path: Path) -> None:
    reports = tmp_path / "reports"
    (reports / "uptime").mkdir(parents=True, exist_ok=True)
    (reports / "uptime" / "summary.md").write_text("ok", encoding="utf-8")
    ctx = make_context(tmp_path)

    calls = {"n": 0}

    def llm(prompt: str) -> str:
        calls["n"] += 1
        return "resumen"

    webdashboard.run(ctx, llm=llm, now="a")
    webdashboard.run(ctx, llm=llm, now="b")  # same summaries → cached, no 2nd LLM call
    assert calls["n"] == 1


def test_run_writes_panel_with_ai_and_cards(tmp_path: Path) -> None:
    reports = tmp_path / "reports"
    with MetricsStore(reports / "cache" / "metrics.db") as store:
        store.record("r1", "uptime", {"down": 2.0}, ok=False, failures=1)
    (reports / "dbcheck").mkdir(parents=True, exist_ok=True)
    (reports / "dbcheck" / "summary.md").write_text("# Dbcheck\n- 1 corrupta", encoding="utf-8")

    ctx = make_context(tmp_path)
    result = webdashboard.run(ctx, llm=lambda prompt: "Resumen IA de prueba.", now="2026-07-07")

    page = (reports / "index.html").read_text(encoding="utf-8")
    assert "Resumen IA de prueba" in page  # injected LLM summary
    assert "1 corrupta" in page and "uptime" in page
    assert result.metrics["cards"] == 1.0


def test_run_ai_failure_is_graceful(tmp_path: Path) -> None:
    reports = tmp_path / "reports"
    (reports / "uptime").mkdir(parents=True, exist_ok=True)
    (reports / "uptime" / "summary.md").write_text("ok", encoding="utf-8")

    def boom(prompt: str) -> str:
        raise RuntimeError("ollama down")

    ctx = make_context(tmp_path)
    result = webdashboard.run(ctx, llm=boom, now="2026-07-07")
    # No AI summary, but the page + cards still render.
    page = (reports / "index.html").read_text(encoding="utf-8")
    assert "🧠" not in page
    assert result.ok and result.metrics["cards"] == 1.0


def test_run_skips_infra_dirs(tmp_path: Path) -> None:
    reports = tmp_path / "reports"
    reports.mkdir(parents=True)
    for skip in ("cache", "webdashboard", "inventory"):
        (reports / skip).mkdir()
        (reports / skip / "summary.md").write_text("x", encoding="utf-8")
    ctx = make_context(tmp_path)
    result = webdashboard.run(ctx, llm=lambda p: "", now="now")
    assert result.metrics["cards"] == 0.0
