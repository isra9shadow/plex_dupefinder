"""Web dashboard — one clear HTML page with the whole homelab's health.

Read-only module. Renders ``reports/index.html`` from:
  * the metrics store — per-module ok/failures status + a sparkline trend,
  * each module's latest ``summary.md`` (markdown-rendered, colour-coded by status),
  * an optional AI **executive summary** (Ollama) in plain Spanish at the top.

Plus a KPI row and a client-side filter box. ``webui.py`` serves it; the page
auto-refreshes. Design follows the dataviz skill (reserved status colours + icon +
label, thin single-series sparklines, light/dark surfaces, accessible table).

Strictly read-only (INVARIANT I1): writes only ``index.html`` + its own report.
"""

from __future__ import annotations

import html
import json
from collections.abc import Callable
from pathlib import Path

from core.metrics import MetricsStore
from core.registry import register
from core.types import ModuleResult, RunContext

# (prompt) -> answer. Injected so tests never call the LLM.
LLM = Callable[[str], str]

_REFRESH_SECONDS = 60
_SKIP_DIRS = {"cache", "webdashboard", "inventory"}

_CSS = """
:root{
  --surface:#fcfcfb; --plane:#f9f9f7; --ink:#0b0b0b; --ink2:#52514e; --muted:#898781;
  --grid:#e1e0d9; --ring:rgba(11,11,11,.10);
  --good:#0ca30c; --critical:#d03b3b; --warn:#fab219; --spark:#2a78d6;
}
@media (prefers-color-scheme:dark){:root{
  --surface:#1a1a19; --plane:#0d0d0d; --ink:#fff; --ink2:#c3c2b7; --muted:#898781;
  --grid:#2c2c2a; --ring:rgba(255,255,255,.10);
  --good:#0ca30c; --critical:#d03b3b; --warn:#fab219; --spark:#3987e5;
}}
*{box-sizing:border-box}
body{margin:0;background:var(--plane);color:var(--ink);
  font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
.wrap{max-width:1120px;margin:0 auto;padding:24px}
h1{font-size:22px;margin:0 0 2px} .sub{color:var(--muted);margin:0 0 16px;font-size:13px}
h2{font-size:13px;color:var(--muted);margin:26px 0 10px;text-transform:uppercase}
h2{letter-spacing:.05em}
.ai{background:var(--surface);border:1px solid var(--ring);border-left:4px solid var(--spark);
  border-radius:10px;padding:12px 16px;margin:0 0 8px;color:var(--ink2)}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:12px;margin:8px 0}
.kpi{background:var(--surface);border:1px solid var(--ring);border-radius:10px;padding:12px 16px}
.kpi .n{font-size:26px;font-weight:700;font-variant-numeric:tabular-nums}
.kpi .l{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.04em}
.kpi.good .n{color:var(--good)} .kpi.bad .n{color:var(--critical)}
.tools{display:flex;gap:8px;margin:6px 0 2px}
.tools input{flex:1;max-width:320px;padding:8px 10px;border:1px solid var(--ring);border-radius:8px;
  background:var(--surface);color:var(--ink);font:14px system-ui}
.tiles{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:12px}
.tile{background:var(--surface);border:1px solid var(--ring);border-radius:10px;padding:14px 16px}
.tile .top{display:flex;justify-content:space-between;align-items:center;gap:8px}
.tile .name{font-weight:600} .tile .st{font-size:13px;font-weight:600}
.st.good{color:var(--good)} .st.bad{color:var(--critical)}
.tile .m{color:var(--ink2);font-size:12px;margin-top:6px;font-variant-numeric:tabular-nums;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.spark{margin-top:8px}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(330px,1fr));gap:12px}
.card{background:var(--surface);border:1px solid var(--ring);border-left:4px solid var(--muted);
  border-radius:10px;padding:12px 16px}
.card.good{border-left-color:var(--good)} .card.bad{border-left-color:var(--critical)}
.card h3{font-size:14px;margin:0 0 6px;display:flex;justify-content:space-between}
.card h3{align-items:baseline}
.card h3 a{color:var(--muted);font-weight:400;font-size:12px;text-decoration:none}
.card .b{font-size:12.5px;line-height:1.5;color:var(--ink2);max-height:260px;overflow:auto}
.card .b strong{color:var(--ink)} .card .b ul{margin:4px 0;padding-left:18px}
table{width:100%;border-collapse:collapse;font-size:13px;font-variant-numeric:tabular-nums}
th,td{text-align:left;padding:6px 10px;border-bottom:1px solid var(--grid)}
th{color:var(--muted);font-weight:600}
"""

_FILTER_JS = """
<script>
const q=document.getElementById('q');
if(q){q.addEventListener('input',()=>{const v=q.value.toLowerCase();
  document.querySelectorAll('.card').forEach(c=>{
    c.style.display=c.dataset.name.includes(v)?'':'none';});});}
</script>
"""


def _as_int(value: object) -> int:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


def md_lite(text: str) -> str:
    """Tiny, safe markdown → HTML for a report summary (escaped first).

    Handles headings (``#``/``##``/``###`` → bold line), ``- ``/``* `` bullets
    (wrapped in a ``<ul>``), ``> `` quotes, and blank lines. Everything else is a
    plain escaped line. No raw HTML from the source is ever emitted.
    """
    out: list[str] = []
    in_list = False
    for raw in (text or "").splitlines():
        line = raw.rstrip()
        stripped = line.lstrip()
        is_bullet = stripped.startswith(("- ", "* "))
        if in_list and not is_bullet:
            out.append("</ul>")
            in_list = False
        if not stripped:
            out.append("<br>")
        elif stripped.startswith("#"):
            out.append(f"<strong>{html.escape(stripped.lstrip('# ').strip())}</strong><br>")
        elif is_bullet:
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{html.escape(stripped[2:])}</li>")
        elif stripped.startswith(">"):
            out.append(f"<em>{html.escape(stripped.lstrip('> ').strip())}</em><br>")
        else:
            out.append(f"{html.escape(line)}<br>")
    if in_list:
        out.append("</ul>")
    return "".join(out)


def sparkline_svg(points: list[float], *, width: int = 200, height: int = 30) -> str:
    """Thin single-series sparkline (SVG polyline). Empty/one point → flat baseline."""
    pad = 3.0
    w, h = float(width), float(height)
    if len(points) < 2:
        y = h / 2
        return (
            f'<svg class="spark" width="{width}" height="{height}" '
            f'viewBox="0 0 {width} {height}" role="img" aria-label="sin serie">'
            f'<line x1="{pad}" y1="{y}" x2="{w - pad}" y2="{y}" '
            f'stroke="var(--spark)" stroke-width="2" opacity=".5"/></svg>'
        )
    lo, hi = min(points), max(points)
    span = (hi - lo) or 1.0
    n = len(points)
    coords = [
        f"{pad + (w - 2 * pad) * (i / (n - 1)):.1f},"
        f"{pad + (h - 2 * pad) * (1 - (v - lo) / span):.1f}"
        for i, v in enumerate(points)
    ]
    return (
        f'<svg class="spark" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" aria-label="tendencia">'
        f'<polyline fill="none" stroke="var(--spark)" stroke-width="2" '
        f'stroke-linejoin="round" stroke-linecap="round" points="{" ".join(coords)}"/></svg>'
    )


def _severity_by_module(status: list[dict[str, object]]) -> dict[str, str]:
    """good/bad per module from the metrics store status (unknown → '')."""
    out: dict[str, str] = {}
    for s in status:
        out[str(s.get("module"))] = "good" if s.get("ok") else "bad"
    return out


def _kpis(status: list[dict[str, object]]) -> str:
    total = len(status)
    ok = sum(1 for s in status if s.get("ok"))
    bad = total - ok
    fails = sum(_as_int(s.get("failures")) for s in status)
    cells = [
        ("", str(total), "módulos"),
        ("good", str(ok), "OK"),
        ("bad" if bad else "", str(bad), "con fallos"),
        ("bad" if fails else "", str(fails), "fallos"),
    ]
    return (
        '<div class="kpis">'
        + "".join(
            f'<div class="kpi {cls}"><div class="n">{n}</div><div class="l">{lbl}</div></div>'
            for cls, n, lbl in cells
        )
        + "</div>"
    )


def _status_tiles(
    status: list[dict[str, object]], sparks: dict[str, tuple[str, list[float]]]
) -> str:
    tiles: list[str] = []
    for s in sorted(status, key=lambda x: str(x.get("module"))):
        module = html.escape(str(s.get("module", "")))
        ok = bool(s.get("ok"))
        failures = _as_int(s.get("failures"))
        st_cls, icon, word = ("good", "✓", "OK") if ok else ("bad", "✕", "FALLO")
        spark = sparks.get(str(s.get("module")))
        spark_html = metric_line = ""
        if spark is not None:
            key, points = spark
            spark_html = sparkline_svg(points)
            if points:
                metric_line = f'<div class="m">{html.escape(key)}: {points[-1]:g}</div>'
        fail_txt = f" · {failures} fallo(s)" if failures else ""
        tiles.append(
            f'<div class="tile"><div class="top"><span class="name">{module}</span>'
            f'<span class="st {st_cls}">{icon} {word}{fail_txt}</span></div>'
            f"{metric_line}{spark_html}</div>"
        )
    return "".join(tiles)


def _module_cards(cards: list[tuple[str, str]], severity: dict[str, str]) -> str:
    out: list[str] = []
    for module, summary in cards:
        m = html.escape(module)
        cls = severity.get(module, "")
        out.append(
            f'<div class="card {cls}" data-name="{m.lower()}"><h3>{m}'
            f'<a href="{m}/summary.md">ver informe →</a></h3>'
            f'<div class="b">{md_lite(summary)}</div></div>'
        )
    return "".join(out)


def render_html(
    status: list[dict[str, object]],
    sparks: dict[str, tuple[str, list[float]]],
    cards: list[tuple[str, str]],
    *,
    ai_summary: str = "",
    generated: str,
) -> str:
    """Render the dashboard page (pure): AI summary + KPIs + tiles + cards + table."""
    severity = _severity_by_module(status)
    table_rows = "".join(
        f"<tr><td>{html.escape(str(s.get('module', '')))}</td>"
        f"<td>{'OK' if s.get('ok') else 'FALLO'}</td>"
        f"<td>{_as_int(s.get('failures'))}</td>"
        f"<td>{html.escape(str(s.get('ts', '')))}</td></tr>"
        for s in sorted(status, key=lambda x: str(x.get("module")))
    )
    sections: list[str] = [
        "<!doctype html><html lang=es><head><meta charset=utf-8>"
        '<meta name=viewport content="width=device-width,initial-scale=1">'
        f"<meta http-equiv=refresh content={_REFRESH_SECONDS}>"
        f"<title>izumi · panel</title><style>{_CSS}</style></head><body><div class=wrap>",
        "<h1>izumi · panel de salud</h1>",
        f'<p class="sub">generado {html.escape(generated)} · se actualiza cada '
        f"{_REFRESH_SECONDS}s</p>",
    ]
    if ai_summary.strip():
        sections.append(f'<div class="ai">🧠 {md_lite(ai_summary)}</div>')
    if status:
        sections.append(_kpis(status))
        sections.append(f'<h2>Estado</h2><div class="tiles">{_status_tiles(status, sparks)}</div>')
    if cards:
        sections.append('<div class="tools"><input id=q placeholder="filtrar módulos…"></div>')
        sections.append(
            f'<h2>Detalle por módulo</h2><div class="cards">{_module_cards(cards, severity)}</div>'
        )
    if not status and not cards:
        sections.append(
            '<p class="sub">Sin informes todavía — ejecuta el chequeo de salud '
            "(<code>run.py health</code>) primero.</p>"
        )
    if table_rows:
        sections.append(
            "<h2>Tabla (accesible)</h2><table><thead><tr><th>Módulo</th><th>Estado</th>"
            "<th>Fallos</th><th>Última ejecución</th></tr></thead>"
            f"<tbody>{table_rows}</tbody></table>"
        )
    sections.append(_FILTER_JS)
    sections.append("</div></body></html>")
    return "".join(sections)


def _collect_cards(reports: Path) -> list[tuple[str, str]]:
    if not reports.is_dir():
        return []
    out: list[tuple[str, str]] = []
    for sub in sorted(p for p in reports.iterdir() if p.is_dir()):
        if sub.name in _SKIP_DIRS:
            continue
        summary = sub / "summary.md"
        if summary.is_file():
            try:
                out.append((sub.name, summary.read_text(encoding="utf-8")))
            except OSError:
                continue
    return out


def build_exec_prompt(cards: list[tuple[str, str]]) -> str:
    """Prompt for a 2-3 sentence Spanish executive summary of the homelab state."""
    parts = [
        "Resume en 2-3 frases, en español claro y directo, el estado GENERAL del "
        "homelab a partir de estos informes. Destaca solo lo importante (fallos, "
        "riesgos) y si todo está bien, dilo. No inventes nada fuera de los informes.",
        "",
    ]
    for module, summary in cards:
        parts.append(f"### {module}\n{summary.strip()[:600]}")
    parts.append("\nRESUMEN:")
    return "\n".join(parts)


def _make_llm(ctx: RunContext) -> LLM:
    def _call(prompt: str) -> str:
        from integrations.ollama import OllamaClient

        cfg = ctx.config.integrations.get("ollama", {})
        kwargs: dict[str, object] = {}
        base, model = cfg.get("base_url"), cfg.get("model")
        if isinstance(base, str) and base:
            kwargs["base_url"] = base
        if isinstance(model, str) and model:
            kwargs["model"] = model
        return OllamaClient(**kwargs).complete(prompt)  # type: ignore[arg-type]

    return _call


def _clock() -> str:
    from datetime import datetime

    return datetime.now().strftime("%Y-%m-%d %H:%M")


@register("webdashboard")
def run(ctx: RunContext, *, llm: LLM | None = None, now: str | None = None) -> ModuleResult:
    """Generate ``reports/index.html`` from metrics + summaries (+ optional AI summary)."""
    result = ModuleResult(module="webdashboard", run_id=ctx.run_id, mode=ctx.mode)
    reports = ctx.config.reporting.dir
    db = reports / "cache" / "metrics.db"

    status: list[dict[str, object]] = []
    sparks: dict[str, tuple[str, list[float]]] = {}
    if db.is_file():
        with MetricsStore(db) as store:
            status = store.latest_status()
            for m in store.latest_metrics():
                module = str(m.get("module"))
                if module in sparks:
                    continue
                key = str(m.get("key"))
                sparks[module] = (key, [v for _ts, v in store.series(module, key, days=30)])

    cards = _collect_cards(reports)

    ai_summary = ""
    want_ai = bool(ctx.config.integrations.get("webdashboard", {}).get("ai_summary", True))
    if cards and want_ai:
        caller = llm if llm is not None else _make_llm(ctx)
        try:
            ai_summary = caller(build_exec_prompt(cards))
        except Exception:  # Ollama down / not configured → omit the summary, no failure
            ai_summary = ""

    page = render_html(status, sparks, cards, ai_summary=ai_summary, generated=now or _clock())
    reports.mkdir(parents=True, exist_ok=True)
    (reports / "index.html").write_text(page, encoding="utf-8")

    out_dir = reports / "webdashboard"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "plan.json").write_text(
        json.dumps(
            {"modules": len(status), "cards": len(cards), "ai_summary": bool(ai_summary)},
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (out_dir / "summary.md").write_text(
        f"# Web dashboard\n\nMódulos: {len(status)} · tarjetas: {len(cards)} · "
        f"IA: {'sí' if ai_summary else 'no'}\nPágina: {reports / 'index.html'}\n",
        encoding="utf-8",
    )
    ctx.logger.info("webdashboard done", modules=len(status), cards=len(cards), ai=bool(ai_summary))
    result.metrics["modules"] = float(len(status))
    result.metrics["cards"] = float(len(cards))
    result.actions = 0
    return result
