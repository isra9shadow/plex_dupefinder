"""Certificate doctor — warn before an SSL/TLS certificate expires (read-only).

A lapsed cert takes services (traefik, *arr behind HTTPS, overseerr…) offline; this
probes each configured endpoint's TLS certificate and flags any that is expired or
expiring within ``warn_days``. Nothing to install: stdlib ``ssl`` does the handshake.

Config (config.json), under ``integrations.certdoctor``:
  endpoints : list of {name, host, port(default 443)} to probe
  warn_days : warn when a cert expires within this many days (default 21)

Strictly read-only (INVARIANT I1): opens TLS connections and writes only its report.
An unreachable host / unverifiable cert is a FailureRecord, never a crash.

Metrics: ``checked``, ``expiring`` (expired + within warn_days).
"""

from __future__ import annotations

import json
import socket
import ssl
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from core.registry import register
from core.types import FailureRecord, ModuleResult, RunContext

# (host, port, timeout) -> certificate notAfter as epoch seconds. Raises on any
# failure (unreachable / unverifiable). Injected so tests never open a socket.
ExpiryFn = Callable[[str, int, float], float]

_DEFAULT_WARN_DAYS = 21.0
_DEFAULT_TIMEOUT = 8.0


@dataclass(frozen=True)
class Endpoint:
    name: str
    host: str
    port: int


@dataclass(frozen=True)
class CertFinding:
    name: str
    host: str
    port: int
    status: str  # ok | expiring | expired | error
    days_left: float  # -1 on error
    detail: str = ""


def default_expiry(host: str, port: int, timeout: float) -> float:
    """Real TLS probe: connect, read the peer cert's notAfter (epoch seconds).

    Uses a verifying context (works for public/Let's Encrypt chains as traefik
    serves); a self-signed/unverifiable cert raises, which the caller records.
    """
    context = ssl.create_default_context()
    with socket.create_connection((host, port), timeout=timeout) as sock:
        with context.wrap_socket(sock, server_hostname=host) as tls:
            cert = tls.getpeercert()
    not_after = cert.get("notAfter") if isinstance(cert, dict) else None
    if not isinstance(not_after, str) or not not_after:
        raise ValueError("no notAfter in peer certificate")
    return float(ssl.cert_time_to_seconds(not_after))


def _endpoints(ctx: RunContext) -> list[Endpoint]:
    cfg = ctx.config.integrations.get("certdoctor", {})
    raw = cfg.get("endpoints")
    if not isinstance(raw, list):
        return []
    out: list[Endpoint] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        host = entry.get("host")
        if not isinstance(host, str) or not host.strip():
            continue
        port = entry.get("port")
        port_i = port if isinstance(port, int) and not isinstance(port, bool) and port > 0 else 443
        name = entry.get("name")
        label = name if isinstance(name, str) and name else host
        out.append(Endpoint(name=label, host=host.strip(), port=port_i))
    return out


def evaluate(
    endpoint: Endpoint, expiry_epoch: float | None, *, now: float, warn_days: float
) -> CertFinding:
    """Classify a cert as ok / expiring / expired / error (pure)."""
    if expiry_epoch is None:
        return CertFinding(
            endpoint.name,
            endpoint.host,
            endpoint.port,
            "error",
            -1.0,
            "no se pudo leer el certificado",
        )
    days_left = (expiry_epoch - now) / 86400.0
    if days_left < 0:
        status = "expired"
    elif days_left <= warn_days:
        status = "expiring"
    else:
        status = "ok"
    return CertFinding(endpoint.name, endpoint.host, endpoint.port, status, round(days_left, 1))


def _write_report(
    ctx: RunContext, findings: list[CertFinding], warn_days: float, note: str
) -> None:
    out_dir = ctx.config.reporting.dir / "certdoctor"
    out_dir.mkdir(parents=True, exist_ok=True)
    bad = [f for f in findings if f.status in ("expiring", "expired")]
    (out_dir / "plan.json").write_text(
        json.dumps(
            {
                "checked": len(findings),
                "expiring": len(bad),
                "warn_days": warn_days,
                "note": note,
                "certs": [
                    {
                        "name": f.name,
                        "host": f.host,
                        "port": f.port,
                        "status": f.status,
                        "days_left": f.days_left,
                        "detail": f.detail,
                    }
                    for f in findings
                ],
            },
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    lines = [
        "# Certdoctor — caducidad de certificados TLS (solo lectura)",
        "",
        f"Comprobados: {len(findings)} · a caducar/caducados: {len(bad)} (<{warn_days:.0f}d)",
        "",
    ]
    for f in sorted(findings, key=lambda x: x.days_left):
        left = "caducado" if f.status == "expired" else f"{f.days_left:g}d"
        suffix = f" — {f.detail}" if f.detail else ""
        lines.append(f"- [{f.status}] {f.name} ({f.host}:{f.port}) · {left}{suffix}")
    if not findings:
        lines.append("(sin endpoints configurados — integrations.certdoctor.endpoints)")
    if note:
        lines += ["", f"> {note}"]
    (out_dir / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


@register("certdoctor")
def run(
    ctx: RunContext, *, expiry: ExpiryFn = default_expiry, now: float | None = None
) -> ModuleResult:
    """Probe configured TLS endpoints and flag expiring/expired certificates."""
    result = ModuleResult(module="certdoctor", run_id=ctx.run_id, mode=ctx.mode)
    cfg = ctx.config.integrations.get("certdoctor", {})
    warn_days = cfg.get("warn_days")
    warn = (
        float(warn_days)
        if isinstance(warn_days, int | float) and not isinstance(warn_days, bool) and warn_days > 0
        else _DEFAULT_WARN_DAYS
    )
    when = now if now is not None else datetime.now(UTC).timestamp()

    findings: list[CertFinding] = []
    for endpoint in _endpoints(ctx):
        try:
            epoch: float | None = expiry(endpoint.host, endpoint.port, _DEFAULT_TIMEOUT)
        except Exception as exc:  # unreachable / unverifiable cert
            findings.append(evaluate(endpoint, None, now=when, warn_days=warn))
            result.add_failure(
                FailureRecord(
                    category="integration", message=f"{endpoint.name}: {exc}", src=endpoint.host
                )
            )
            continue
        finding = evaluate(endpoint, epoch, now=when, warn_days=warn)
        findings.append(finding)
        if finding.status in ("expiring", "expired"):
            result.add_failure(
                FailureRecord(
                    category="integration",
                    message=f"{endpoint.name}: certificado {finding.status} ({finding.days_left}d)",
                    src=endpoint.host,
                )
            )

    note = ""
    if not findings:
        note = (
            "No endpoints configured — set integrations.certdoctor.endpoints ({name, host, port})."
        )
    _write_report(ctx, findings, warn, note)
    expiring = sum(1 for f in findings if f.status in ("expiring", "expired"))
    ctx.logger.info("certdoctor done", checked=len(findings), expiring=expiring)
    result.metrics["checked"] = float(len(findings))
    result.metrics["expiring"] = float(expiring)
    result.actions = 0
    return result
