"""Proactive push — consolidate the latest module reports into a Telegram digest.

This is the "segunda persona te avisa" piece: run the read-only health/AI modules
(uptime, diskwatch, dbcheck, status, autoheal, analyst, logwatch, …) on a nightly
cron, then run THIS module to gather each one's freshest ``summary.md`` into a
single digest and push it to Telegram — so the operator gets a daily health report
(and anomaly alerts) without opening the SSH menu.

Boundaries & safety:

  * It never moves/deletes anything (INVARIANT I1 trivially satisfied): the only
    side effect is sending a Telegram message, which is idempotent and reversible.
  * DRY_RUN default (I2): in DRY_RUN it COMPOSES the digest and writes it to its own
    report, but never sends. LIVE actually pushes.
  * Secrets ONLY via ``core/secrets`` — the bot token / chat id are resolved from
    ``config.notify.token_ref`` / ``chat_id_ref`` (default ``IZUMI_TELEGRAM_*``),
    never hardcoded or logged.
  * The Telegram HTTP call is the ONE shared send-only client
    (:mod:`integrations.telegram`); the poster is injected so tests stay offline.

Config (config.json):
  notify : {enabled, token_ref, chat_id_ref}   # reused; enabled gates sending
  integrations.notifypush :
    sources    : list of report subdirs to include, in order
                 (default: uptime, diskwatch, dbcheck, status, autoheal,
                  analyst, logwatch)
    title      : header line for the digest (default "izumi · informe")
    max_section_chars : truncate each section's body to this many chars (default 1200)

Metrics: ``sections`` (included), ``sent`` (1 if pushed, 0 otherwise).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from core.registry import register
from core.secrets import get_secret
from core.types import FailureRecord, ModuleResult, RunContext, SafetyMode
from integrations import telegram

# (token, chat_id, text) -> delivered? Injected so tests never touch the network.
Sender = Callable[[str, str, str], bool]

_DEFAULT_SOURCES = (
    "uptime",
    "diskwatch",
    "dbcheck",
    "status",
    "permsdoctor",
    "backupaudit",
    "autoheal",
    "analyst",
    "logwatch",
)
_DEFAULT_TITLE = "izumi · informe"
_DEFAULT_MAX_SECTION = 1200


@dataclass(frozen=True)
class _Settings:
    sources: tuple[str, ...]
    title: str
    max_section_chars: int


def _str_list(raw: object) -> list[str]:
    if not isinstance(raw, list):
        return []
    return [item.strip() for item in raw if isinstance(item, str) and item.strip()]


def _settings(ctx: RunContext) -> _Settings:
    cfg = ctx.config.integrations.get("notifypush", {})
    sources = tuple(_str_list(cfg.get("sources"))) or _DEFAULT_SOURCES
    title = cfg.get("title")
    max_chars = cfg.get("max_section_chars")
    return _Settings(
        sources=sources,
        title=title if isinstance(title, str) and title.strip() else _DEFAULT_TITLE,
        max_section_chars=(
            max_chars
            if isinstance(max_chars, int) and not isinstance(max_chars, bool) and max_chars > 0
            else _DEFAULT_MAX_SECTION
        ),
    )


def _read_summary(reports_dir: Path, subdir: str) -> str:
    """Return a module's latest ``summary.md`` text (stripped), or '' if missing."""
    path = reports_dir / subdir / "summary.md"
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _truncate(text: str, limit: int) -> str:
    """Clip ``text`` to ``limit`` chars, marking the cut (keeps the digest bounded)."""
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n… (truncado)"


def collect_sections(
    reports_dir: Path, sources: tuple[str, ...], max_chars: int
) -> list[tuple[str, str]]:
    """Return ``(source, body)`` for every source that has a non-empty summary."""
    out: list[tuple[str, str]] = []
    for source in sources:
        body = _read_summary(reports_dir, source)
        if body:
            out.append((source, _truncate(body, max_chars)))
    return out


def build_digest(sections: list[tuple[str, str]], *, title: str, when: str) -> str:
    """Compose the human digest text (pure; unit-tested)."""
    lines = [f"{title} — {when}", ""]
    if not sections:
        lines.append("(sin informes recientes — ejecuta los chequeos primero)")
        return "\n".join(lines)
    for source, body in sections:
        lines.append(f"═══ {source} ═══")
        lines.append(body)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _default_sender(token: str, chat_id: str, text: str) -> bool:
    return telegram.send_message(token, chat_id, text)


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def _write_report(ctx: RunContext, digest: str, sent: bool, dry_run: bool, note: str) -> None:
    out_dir = ctx.config.reporting.dir / "notifypush"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "plan.json").write_text(
        json.dumps(
            {"dry_run": dry_run, "sent": sent, "note": note, "chars": len(digest)},
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (out_dir / "summary.md").write_text(digest, encoding="utf-8")


@register("notifypush")
def run(
    ctx: RunContext,
    *,
    sender: Sender = _default_sender,
    now: str | None = None,
) -> ModuleResult:
    """Compose the digest from the latest module summaries and (in LIVE) push it.

    ``sender`` and ``now`` are injected so tests run offline and deterministically.
    In DRY_RUN (default) the digest is written to this module's report but NOT sent.
    """
    result = ModuleResult(module="notifypush", run_id=ctx.run_id, mode=ctx.mode)
    settings = _settings(ctx)
    dry_run = ctx.mode != SafetyMode.LIVE
    when = now if now is not None else _now()

    sections = collect_sections(
        ctx.config.reporting.dir, settings.sources, settings.max_section_chars
    )
    digest = build_digest(sections, title=settings.title, when=when)

    sent = False
    note = ""
    if dry_run:
        note = "DRY-RUN: digest compuesto pero NO enviado (usa modo live para enviar)."
    elif not ctx.config.notify.enabled:
        note = "notify.enabled=false — no se envía; activa notify para el push."
    else:
        token = get_secret(ctx.config.notify.token_ref, required=False)
        chat_id = get_secret(ctx.config.notify.chat_id_ref, required=False)
        if not token or not chat_id:
            note = (
                f"faltan secretos {ctx.config.notify.token_ref} / "
                f"{ctx.config.notify.chat_id_ref} (.env) — no se envía."
            )
            result.add_failure(FailureRecord(category="config", message=note))
        else:
            sent = sender(token, chat_id, digest)
            if not sent:
                result.add_failure(
                    FailureRecord(
                        category="integration",
                        message="Telegram no confirmó el envío del informe.",
                    )
                )

    _write_report(ctx, digest, sent, dry_run, note)
    ctx.logger.info(
        "notifypush done",
        sections=len(sections),
        sent=sent,
        dry_run=dry_run,
    )
    result.metrics["sections"] = float(len(sections))
    result.metrics["sent"] = 1.0 if sent else 0.0
    result.actions = 1 if sent else 0
    return result
