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

import hashlib
import html
import json
import re
import time
from collections.abc import Callable
from pathlib import Path

from core.cache import Cache
from core.metrics import MetricsStore
from core.registry import register
from core.types import ModuleResult, RunContext

# (prompt) -> answer. Injected so tests never call the LLM.
LLM = Callable[[str], str]

_REFRESH_SECONDS = 60
_SKIP_DIRS = {"cache", "webdashboard", "inventory"}

# Cards are grouped under these collapsible sections (first match wins; the rest
# fall into "Otros"). Purely presentational.
_SECTIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "Salud",
        (
            "uptime",
            "dbcheck",
            "diskwatch",
            "permsdoctor",
            "backupaudit",
            "netdoctor",
            "certdoctor",
            "capacitydoctor",
            "status",
        ),
    ),
    ("Media", ("extractor", "organizer", "plexrefresh", "shadowcheck", "dbrepair")),
    ("IA & avisos", ("analyst", "logwatch", "autoheal", "autopilot", "notifypush", "configcheck")),
)

# A word anywhere in a summary that means "something is wrong" → red card.
_BAD_WORDS = (
    "corrupt",
    "fallo",
    "expired",
    "caducado",
    "over_threshold",
    "full_soon",
    "rolled_back",
    "missing:",
    "invalid:",
)
# "<label> N" counts that mean trouble when N>0 → amber card.
_COUNT_RE = re.compile(
    r"(errores?|errors?|corrupt|missing|invalid|en riesgo|at.?risk|a caducar|caducados|"
    r"drift|deriva|aislados|isolated|expiring|degrad)\D{0,8}(\d+)",
    re.IGNORECASE,
)


def card_severity(summary: str, metric_status: str) -> str:
    """Colour a card 'good'/'warn'/'bad'/'' from the metrics status + the summary text.

    The metrics status (from a run.py run) is authoritative for a hard failure; on top
    of that the summary content can raise a warning (e.g. logwatch '17 errors' even when
    the run itself succeeded), so a problem is visible before it becomes a failure.
    """
    if metric_status == "bad":
        return "bad"
    low = summary.lower()
    if any(w in low for w in _BAD_WORDS):
        return "bad"
    worst = max((int(m.group(2)) for m in _COUNT_RE.finditer(summary)), default=0)
    if worst > 0:
        return "warn"
    return metric_status  # 'good' or '' (unknown)


_CSS = """
:root{
  --surface:#fcfcfb; --plane:#f9f9f7; --ink:#0b0b0b; --ink2:#52514e; --muted:#898781;
  --grid:#e1e0d9; --ring:rgba(11,11,11,.10);
  --good:#0ca30c; --critical:#d03b3b; --warn:#fab219; --spark:#2a78d6; --accent:__ACCENT__;
}
@media (prefers-color-scheme:dark){:root{
  --surface:#1a1a19; --plane:#0d0d0d; --ink:#fff; --ink2:#c3c2b7; --muted:#898781;
  --grid:#2c2c2a; --ring:rgba(255,255,255,.10);
  --good:#0ca30c; --critical:#d03b3b; --warn:#fab219; --spark:#3987e5; --accent:__ACCENT__;
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
.tools button{padding:8px 12px;border:1px solid var(--ring);border-radius:8px;cursor:pointer;
  background:var(--surface);color:var(--ink);font:13px system-ui}
.tools button:disabled{opacity:.5;cursor:progress}
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
.card.warn{border-left-color:var(--warn)}
details{margin:0 0 6px} details>summary{cursor:pointer;color:var(--ink2);font-size:13px;
  text-transform:uppercase;letter-spacing:.05em;padding:6px 0;list-style:none}
details>summary::-webkit-details-marker{display:none}
details>summary::before{content:"▸ "} details[open]>summary::before{content:"▾ "}
.card h3{font-size:14px;margin:0 0 6px;display:flex;justify-content:space-between}
.card h3{align-items:baseline}
.card h3 a{color:var(--muted);font-weight:400;font-size:12px;text-decoration:none}
.card .b{font-size:12.5px;line-height:1.5;color:var(--ink2);max-height:260px;overflow:auto}
.card .b strong{color:var(--ink)} .card .b ul{margin:4px 0;padding-left:18px}
table{width:100%;border-collapse:collapse;font-size:13px;font-variant-numeric:tabular-nums}
th,td{text-align:left;padding:6px 10px;border-bottom:1px solid var(--grid)}
th{color:var(--muted);font-weight:600}
.figs{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:12px}
.fig{background:var(--surface);border:1px solid var(--ring);border-radius:10px;padding:10px 12px}
.figh{font-size:12px;color:var(--ink2);margin-bottom:2px}
.chart .pt{fill:var(--accent);opacity:.85;cursor:pointer}
.chart .pt:hover{r:5}
#tip{position:fixed;pointer-events:none;background:var(--ink);color:var(--plane);
  font:12px system-ui;padding:3px 7px;border-radius:6px;opacity:0;transition:opacity .1s;z-index:9}
.tl{list-style:none;padding:0;margin:0}
.tl li{padding:6px 0;border-bottom:1px solid var(--grid);font-size:13px}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:8px}
.dot.good{background:var(--good)} .dot.bad{background:var(--critical)}
a{color:var(--accent)}
.chat{display:flex;gap:8px;margin:8px 0}
.chat input{flex:1;padding:9px 12px;border:1px solid var(--ring);border-radius:8px;
  background:var(--surface);color:var(--ink);font:14px system-ui}
.chat button,.btn{padding:9px 14px;border:1px solid var(--ring);border-radius:8px;cursor:pointer;
  background:var(--accent);color:#fff;font:13px system-ui;text-decoration:none}
.fx{list-style:none;padding:0;margin:0}
.fx li{display:flex;gap:8px;align-items:center;padding:6px 0;border-bottom:1px solid var(--grid);
  font-size:13px}
.fx code{background:var(--plane);padding:2px 6px;border-radius:5px;font:12px ui-monospace,monospace}
.fx button{margin-left:auto;padding:5px 10px;border:1px solid var(--ring);border-radius:7px;
  cursor:pointer;background:var(--surface);color:var(--ink);font:12px system-ui}
"""

_JS = """
<script>
const q=document.getElementById('q');
if(q){q.addEventListener('input',()=>{const v=q.value.toLowerCase();
  document.querySelectorAll('.card').forEach(c=>{
    c.style.display=c.dataset.name.includes(v)?'':'none';});});}
document.querySelectorAll('[data-act]').forEach(b=>b.addEventListener('click',async()=>{
  let t=localStorage.getItem('izumi_tok')||prompt('Token (IZUMI_WEB_TOKEN):');
  if(!t)return; localStorage.setItem('izumi_tok',t);
  const o=b.textContent; b.disabled=true; b.textContent='ejecutando…';
  try{const r=await fetch('/api/run',{method:'POST',
      headers:{'content-type':'application/json'},
      body:JSON.stringify({action:b.dataset.act,token:t})});
    const j=await r.json();
    if(!j.ok && /token/.test(j.message||'')) localStorage.removeItem('izumi_tok');
    b.textContent=o; b.disabled=false; if(j.ok){location.reload();}else{alert(j.message||'error');}
  }catch(e){b.textContent=o; b.disabled=false; alert('error de red');}
});});
const tip=document.getElementById('tip');
if(tip){document.querySelectorAll('.chart .pt').forEach(p=>{
  p.addEventListener('mousemove',e=>{tip.textContent=p.dataset.v;
    tip.style.left=(e.clientX+10)+'px';tip.style.top=(e.clientY-10)+'px';tip.style.opacity=1;});
  p.addEventListener('mouseleave',()=>{tip.style.opacity=0;});});}
const gen=parseFloat(document.body.dataset.gen||'0');
const age=document.getElementById('age');
function fa(s){s=Math.max(0,s|0);
  if(s<90)return 'hace segundos';
  if(s<5400)return 'hace '+(s/60|0)+' min';
  return 'hace '+(s/3600|0)+' h';}
if(age){const u=()=>{age.textContent='actualizado '+fa(Date.now()/1000-gen);};
  u(); setInterval(u,1000);}
let lm=null;
setInterval(async()=>{try{
  const r=await fetch('/',{method:'HEAD',cache:'no-store'});
  const m=r.headers.get('Last-Modified');
  if(lm&&m&&m!==lm){location.reload();} lm=m;
}catch(e){}},30000);
function tok(){let t=localStorage.getItem('izumi_tok')||prompt('Token (IZUMI_WEB_TOKEN):');
  if(t)localStorage.setItem('izumi_tok',t); return t;}
document.querySelectorAll('[data-cmd]').forEach(b=>b.addEventListener('click',async()=>{
  if(!confirm('Aplicar: '+b.dataset.cmd+' ?'))return; const t=tok(); if(!t)return; b.disabled=true;
  try{const r=await fetch('/api/apply',{method:'POST',
    headers:{'content-type':'application/json'},
    body:JSON.stringify({command:b.dataset.cmd,token:t})});
    const j=await r.json(); alert(j.message||'');
    if(j.ok){location.reload();}else{b.disabled=false;}
  }catch(e){alert('error de red'); b.disabled=false;}
}));
const asb=document.getElementById('asb');
if(asb){asb.addEventListener('click',async()=>{
  const ask=document.getElementById('ask'), ans=document.getElementById('ans');
  const qq=(ask.value||'').trim(); if(!qq)return; const t=tok(); if(!t)return;
  asb.disabled=true; ans.style.display='block'; ans.textContent='pensando…';
  try{const r=await fetch('/api/ask',{method:'POST',
    headers:{'content-type':'application/json'},
    body:JSON.stringify({question:qq,token:t})});
    const j=await r.json(); ans.textContent=j.message||'(sin respuesta)';
  }catch(e){ans.textContent='error de red';} asb.disabled=false;
});}
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


def line_chart_svg(points: list[float], *, width: int = 260, height: int = 64) -> str:
    """A small line chart with hoverable points (data-v carries the value)."""
    pad = 6.0
    w, h = float(width), float(height)
    if len(points) < 2:
        return '<div class="sub">sin serie suficiente</div>'
    lo, hi = min(points), max(points)
    span = (hi - lo) or 1.0
    n = len(points)
    xs = [pad + (w - 2 * pad) * (i / (n - 1)) for i in range(n)]
    ys = [pad + (h - 2 * pad) * (1 - (v - lo) / span) for v in points]
    poly = " ".join(f"{x:.1f},{y:.1f}" for x, y in zip(xs, ys, strict=True))
    dots = "".join(
        f'<circle class="pt" cx="{x:.1f}" cy="{y:.1f}" r="3" data-v="{v:g}"/>'
        for x, y, v in zip(xs, ys, points, strict=True)
    )
    return (
        f'<svg class="chart" width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
        f'role="img" aria-label="tendencia {lo:g}→{hi:g}">'
        f'<line x1="{pad}" y1="{h - pad}" x2="{w - pad}" y2="{h - pad}" '
        f'stroke="var(--grid)" stroke-width="1"/>'
        f'<polyline fill="none" stroke="var(--accent)" stroke-width="2" '
        f'stroke-linejoin="round" points="{poly}"/>{dots}</svg>'
    )


def _trends(sparks: dict[str, tuple[str, list[float]]]) -> str:
    figs = []
    for module in sorted(sparks):
        key, points = sparks[module]
        if len(points) < 2:
            continue
        figs.append(
            f'<div class="fig"><div class="figh">{html.escape(module)} · '
            f"{html.escape(key)}</div>{line_chart_svg(points)}</div>"
        )
    if not figs:
        return ""
    return f'<h2>Tendencias</h2><div class="figs">{"".join(figs)}</div>'


def _flt(value: object) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def _rel_time(ts: float, nowsec: float) -> str:
    d = max(0.0, nowsec - ts)
    if d < 90:
        return "hace segundos"
    if d < 5400:
        return f"hace {int(d / 60)} min"
    if d < 129600:
        return f"hace {int(d / 3600)} h"
    return f"hace {int(d / 86400)} d"


def render_incidents(incidents: list[dict[str, object]], *, nowsec: float) -> str:
    """Timeline of recent incidents (open first), from the incident cache."""
    if not incidents:
        return ""
    rows = []
    for inc in incidents[:40]:
        resolved = str(inc.get("status")) == "resolved"
        dot = "good" if resolved else "bad"
        module = html.escape(str(inc.get("module", "")))
        title = html.escape(str(inc.get("title", "")))
        when = _rel_time(_flt(inc.get("last_seen")), nowsec)
        tag = "resuelto" if resolved else "abierto"
        rows.append(
            f'<li><span class="dot {dot}"></span><b>{module}</b> · {title} '
            f'<span class="sub">— {tag}, {when}</span></li>'
        )
    return f'<h2>Incidencias</h2><ul class="tl">{"".join(rows)}</ul>'


def _fixes_section(fixes: list[tuple[str, str, str]]) -> str:
    """Guard-vetted proposed actions, each with an 'aplicar' button (token-gated)."""
    if not fixes:
        return ""
    rows = "".join(
        f"<li><code>{html.escape(cmd)}</code> "
        f'<span class="sub">{html.escape(title)}</span>'
        f'<button data-cmd="{html.escape(cmd)}">aplicar</button></li>'
        for cmd, title, _sev in fixes
    )
    return f'<h2>Arreglos sugeridos</h2><ul class="fx">{rows}</ul>'


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


def _one_card(module: str, summary: str, status: dict[str, str]) -> str:
    m = html.escape(module)
    cls = card_severity(summary, status.get(module, ""))
    return (
        f'<div class="card {cls}" data-name="{m.lower()}"><h3>{m}'
        f'<a href="{m}/">ver informe →</a></h3>'
        f'<div class="b">{md_lite(summary)}</div></div>'
    )


def render_module_page(module: str, summary: str, plan_json: str, *, generated: str) -> str:
    """A per-module detail page (rendered summary + raw plan.json), served at /<module>/."""
    m = html.escape(module)
    plan_block = (
        f'<h2>plan.json</h2><div class="card"><div class="b"><pre style="white-space:pre-wrap">'
        f"{html.escape(plan_json)}</pre></div></div>"
        if plan_json
        else ""
    )
    return (
        "<!doctype html><html lang=es><head><meta charset=utf-8>"
        '<meta name=viewport content="width=device-width,initial-scale=1">'
        f"<title>izumi · {m}</title><style>{_CSS}</style></head><body><div class=wrap>"
        f"<h1>izumi · {m}</h1>"
        f'<p class="sub">generado {html.escape(generated)} · '
        '<a href="../">← volver al panel</a></p>'
        f'<div class="card"><div class="b">{md_lite(summary)}</div></div>'
        f"{plan_block}</div></body></html>"
    )


def _section_for(module: str) -> str:
    for name, members in _SECTIONS:
        if module in members:
            return name
    return "Otros"


def _grouped_cards(cards: list[tuple[str, str]], status: dict[str, str]) -> str:
    """Group cards into collapsible sections, in the fixed section order."""
    by_section: dict[str, list[str]] = {}
    for module, summary in cards:
        by_section.setdefault(_section_for(module), []).append(_one_card(module, summary, status))
    order = [name for name, _ in _SECTIONS] + ["Otros"]
    blocks: list[str] = []
    for name in order:
        items = by_section.get(name)
        if not items:
            continue
        blocks.append(
            f"<details open><summary>{html.escape(name)} ({len(items)})</summary>"
            f'<div class="cards">{"".join(items)}</div></details>'
        )
    return "".join(blocks)


_FAVICON = (
    "data:image/svg+xml,"
    "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'>"
    "<circle cx='16' cy='16' r='14' fill='%232a78d6'/></svg>"
)


def render_html(
    status: list[dict[str, object]],
    sparks: dict[str, tuple[str, list[float]]],
    cards: list[tuple[str, str]],
    incidents: list[dict[str, object]] | None = None,
    fixes: list[tuple[str, str, str]] | None = None,
    *,
    ai_summary: str = "",
    title: str = "izumi · panel de salud",
    accent: str = "#2a78d6",
    nowsec: float = 0.0,
    generated: str,
) -> str:
    """Render the dashboard page (pure): AI + KPIs + tiles + trends + cards + incidents."""
    severity = _severity_by_module(status)
    css = _CSS.replace("__ACCENT__", accent)
    t = html.escape(title)
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
        f'<link rel=icon href="{_FAVICON}">'
        f"<title>{t}</title><style>{css}</style></head>"
        f'<body data-gen="{nowsec:.0f}"><div id=tip></div><div class=wrap>',
        f"<h1>{t}</h1>",
        f'<p class="sub">generado {html.escape(generated)} · <span id=age></span></p>',
    ]
    if ai_summary.strip():
        sections.append(f'<div class="ai">🧠 {md_lite(ai_summary)}</div>')
    sections.append(
        '<div class="chat"><input id=ask placeholder="Pregunta al asistente IA…">'
        "<button id=asb>Preguntar</button>"
        '<a class="btn" href="/api/export">⬇ Exportar</a></div>'
        '<div id=ans class="ai" style="display:none"></div>'
    )
    if status:
        sections.append(_kpis(status))
        sections.append(f'<h2>Estado</h2><div class="tiles">{_status_tiles(status, sparks)}</div>')
    trends = _trends(sparks)
    if trends:
        sections.append(trends)
    if incidents:
        sections.append(render_incidents(incidents, nowsec=nowsec))
    if cards:
        sections.append(
            '<div class="tools"><input id=q placeholder="filtrar módulos…">'
            '<button data-act="health">▶ Ejecutar salud</button>'
            '<button data-act="webdashboard">↻ Refrescar panel</button></div>'
        )
        sections.append(f"<h2>Detalle por módulo</h2>{_grouped_cards(cards, severity)}")
    if fixes:
        sections.append(_fixes_section(fixes))
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
    sections.append(_JS)
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

    # Per-module detail pages (served at /<module>/), so "ver informe" opens a
    # rendered page instead of raw markdown.
    when = now or _clock()
    for module, summary in cards:
        plan_path = reports / module / "plan.json"
        try:
            plan_json = plan_path.read_text(encoding="utf-8") if plan_path.is_file() else ""
        except OSError:
            plan_json = ""
        (reports / module / "index.html").write_text(
            render_module_page(module, summary, plan_json, generated=when), encoding="utf-8"
        )

    ai_summary = ""
    want_ai = bool(ctx.config.integrations.get("webdashboard", {}).get("ai_summary", True))
    if cards and want_ai:
        # Cache the AI summary keyed on a hash of the summaries — only call Ollama when
        # the content actually changed (avoids an LLM call on every render).
        content_hash = hashlib.sha256("".join(s for _, s in cards).encode("utf-8")).hexdigest()
        ai_cache = Cache(reports / "cache" / "ai_summary.json")
        cached = ai_cache.get("entry")
        if isinstance(cached, dict) and cached.get("hash") == content_hash and cached.get("text"):
            ai_summary = str(cached["text"])
        else:
            caller = llm if llm is not None else _make_llm(ctx)
            try:
                ai_summary = caller(build_exec_prompt(cards))
                ai_cache.set("entry", {"hash": content_hash, "text": ai_summary})
                ai_cache.save()
            except Exception:  # Ollama down / not configured → omit the summary, no failure
                ai_summary = ""

    # Incident timeline (best-effort read of the shared incident cache).
    incidents: list[dict[str, object]] = []
    inc_db = reports / "cache" / "incidents.db"
    if inc_db.is_file():
        try:
            from core.cache import SqliteCache

            with SqliteCache(inc_db) as sc:
                incidents = [
                    {
                        "module": i.module,
                        "title": i.title,
                        "status": i.status,
                        "last_seen": i.last_seen,
                    }
                    for i in sc.recent_all(limit=40)
                ]
        except Exception:  # pragma: no cover - defensive
            incidents = []

    # Guard-vetted proposed fixes (from the modules that already produce them).
    from aictx.apply import collect_actions

    fix_paths = [
        reports / s / "plan.json" for s in ("logwatch", "analyst", "autoheal", "permsdoctor")
    ]
    fixes = [(a.command, a.finding_title, a.severity) for a in collect_actions(fix_paths)]

    wcfg = ctx.config.integrations.get("webdashboard", {})
    title = wcfg.get("title")
    accent = wcfg.get("accent")
    page = render_html(
        status,
        sparks,
        cards,
        incidents,
        fixes,
        ai_summary=ai_summary,
        title=title if isinstance(title, str) and title.strip() else "izumi · panel de salud",
        accent=accent if isinstance(accent, str) and accent.strip() else "#2a78d6",
        nowsec=time.time(),
        generated=when,
    )
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
