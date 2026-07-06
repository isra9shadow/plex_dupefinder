"""Web dashboard — one HTML page with the whole homelab's health at a glance.

Read-only module: reads the metrics store (per-module ok/failures + metric trends)
and renders ``reports/index.html`` — status tiles + sparklines + a table view.
``webui.py`` serves the reports dir so you open it in a browser on the LAN.

Design (dataviz skill): status colors are the reserved good/critical palette paired
with an icon + label (never colour alone); sparklines are a single thin 2px blue
series with no legend; both light and dark modes are defined against their own
surfaces; a table view backs the tiles for accessibility.

Strictly read-only (INVARIANT I1): writes only ``index.html`` + its own report.
"""

from __future__ import annotations

import html
import json

from core.metrics import MetricsStore
from core.registry import register
from core.types import ModuleResult, RunContext

# dataviz reference palette (status + sequential blue + surfaces/ink), light | dark.
_CSS = """
:root{
  --surface:#fcfcfb; --plane:#f9f9f7; --ink:#0b0b0b; --ink2:#52514e; --muted:#898781;
  --grid:#e1e0d9; --ring:rgba(11,11,11,.10);
  --good:#0ca30c; --critical:#d03b3b; --spark:#2a78d6;
}
@media (prefers-color-scheme:dark){:root{
  --surface:#1a1a19; --plane:#0d0d0d; --ink:#fff; --ink2:#c3c2b7; --muted:#898781;
  --grid:#2c2c2a; --ring:rgba(255,255,255,.10);
  --good:#0ca30c; --critical:#d03b3b; --spark:#3987e5;
}}
*{box-sizing:border-box}
body{margin:0;background:var(--plane);color:var(--ink);
  font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
.wrap{max-width:1040px;margin:0 auto;padding:24px}
h1{font-size:20px;margin:0 0 2px} .sub{color:var(--muted);margin:0 0 20px;font-size:13px}
.tiles{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:12px}
.tile{background:var(--surface);border:1px solid var(--ring);border-radius:10px;padding:14px 16px}
.tile .top{display:flex;justify-content:space-between;align-items:center;gap:8px}
.tile .name{font-weight:600} .tile .st{font-size:13px;font-weight:600}
.st.good{color:var(--good)} .st.bad{color:var(--critical)}
.tile .m{color:var(--ink2);font-size:12px;margin-top:6px;
  font-variant-numeric:tabular-nums;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.spark{margin-top:8px}
h2{font-size:14px;color:var(--ink2);margin:28px 0 8px}
table{width:100%;border-collapse:collapse;font-size:13px;font-variant-numeric:tabular-nums}
th,td{text-align:left;padding:6px 10px;border-bottom:1px solid var(--grid)}
th{color:var(--muted);font-weight:600}
"""


def _as_int(value: object) -> int:
    """Coerce a store value to int (0 for anything non-numeric)."""
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


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
    coords = []
    for i, v in enumerate(points):
        x = pad + (w - 2 * pad) * (i / (n - 1))
        y = pad + (h - 2 * pad) * (1 - (v - lo) / span)
        coords.append(f"{x:.1f},{y:.1f}")
    label = f"tendencia {lo:g}→{hi:g}"
    return (
        f'<svg class="spark" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" aria-label="{html.escape(label)}">'
        f'<polyline fill="none" stroke="var(--spark)" stroke-width="2" '
        f'stroke-linejoin="round" stroke-linecap="round" points="{" ".join(coords)}"/></svg>'
    )


def render_html(
    status: list[dict[str, object]],
    sparks: dict[str, tuple[str, list[float]]],
    *,
    generated: str,
) -> str:
    """Render the dashboard page (pure). ``sparks[module] = (metric_key, points)``."""
    tiles: list[str] = []
    for s in sorted(status, key=lambda x: str(x.get("module"))):
        module = html.escape(str(s.get("module", "")))
        ok = bool(s.get("ok"))
        failures = _as_int(s.get("failures"))
        st_cls, icon, word = ("good", "✓", "OK") if ok else ("bad", "✕", "FALLO")
        spark = sparks.get(str(s.get("module")))
        spark_html = ""
        metric_line = ""
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

    rows = "".join(
        f"<tr><td>{html.escape(str(s.get('module', '')))}</td>"
        f"<td>{'OK' if s.get('ok') else 'FALLO'}</td>"
        f"<td>{_as_int(s.get('failures'))}</td>"
        f"<td>{html.escape(str(s.get('ts', '')))}</td></tr>"
        for s in sorted(status, key=lambda x: str(x.get("module")))
    )
    body = (
        "".join(tiles)
        if tiles
        else '<p class="sub">Sin métricas todavía — ejecuta las pipelines primero.</p>'
    )
    return (
        "<!doctype html><html lang=es><head><meta charset=utf-8>"
        '<meta name=viewport content="width=device-width,initial-scale=1">'
        f"<title>izumi · panel</title><style>{_CSS}</style></head><body><div class=wrap>"
        "<h1>izumi · panel de salud</h1>"
        f'<p class="sub">generado {html.escape(generated)}</p>'
        f'<div class="tiles">{body}</div>'
        "<h2>Tabla (accesible)</h2><table><thead><tr><th>Módulo</th><th>Estado</th>"
        f"<th>Fallos</th><th>Última ejecución</th></tr></thead><tbody>{rows}</tbody></table>"
        "</div></body></html>"
    )


def _clock() -> str:
    from datetime import datetime

    return datetime.now().strftime("%Y-%m-%d %H:%M")


@register("webdashboard")
def run(ctx: RunContext, *, now: str | None = None) -> ModuleResult:
    """Generate ``reports/index.html`` from the metrics store (read-only)."""
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
                    continue  # first metric per module drives its sparkline
                key = str(m.get("key"))
                points = [v for _ts, v in store.series(module, key, days=30)]
                sparks[module] = (key, points)

    page = render_html(status, sparks, generated=now if now is not None else _clock())
    reports.mkdir(parents=True, exist_ok=True)
    (reports / "index.html").write_text(page, encoding="utf-8")

    out_dir = reports / "webdashboard"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "plan.json").write_text(
        json.dumps({"modules": len(status)}, indent=2, sort_keys=True, ensure_ascii=False),
        encoding="utf-8",
    )
    (out_dir / "summary.md").write_text(
        f"# Web dashboard\n\nMódulos en el panel: {len(status)}\n"
        f"Página: {reports / 'index.html'}\n",
        encoding="utf-8",
    )
    ctx.logger.info("webdashboard done", modules=len(status))
    result.metrics["modules"] = float(len(status))
    result.actions = 0
    return result
