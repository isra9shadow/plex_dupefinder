"""Network/DNS doctor — detect the root cause of ``getaddrinfo ENOTFOUND`` (read-only).

Symptom (from the real 39-container logwatch run): overseerr/Configarr/*arr spam
``getaddrinfo ENOTFOUND radarr/sonarr`` because they are NOT on a shared
user-defined docker network, so Docker's embedded DNS cannot resolve peers by
container name. This module surfaces that root cause safely:

  * A container attached ONLY to Docker's built-in networks (``bridge`` / ``host`` /
    ``none``) — i.e. on NO user-defined network — cannot resolve other containers
    by name. Those are the "isolated" containers this doctor flags.
  * If the operator configures ``shared_network``, the doctor emits the ADVISORY
    ``docker network connect <shared_network> <container>`` for each isolated
    container. This is intentionally NOT an auto-appliable action: ``docker network
    connect`` is not on the :func:`aictx.apply.classify` allow-list (it changes
    topology and needs the operator to pick the right network), so it is reported
    as text for the operator to run/verify — never executed here.

Strictly READ-ONLY (INVARIANT I1): it only lists containers (via the read-only
docker adapter) and writes its own report. It designs no change beyond the generic
"join the shared network" suggestion, so it never guesses a wrong topology.

Config (config.json):
  integrations.netdoctor :
    shared_network   : name of the user-defined network peers should share
                       (e.g. "proxynet"); when set, drives the connect suggestion
    ignore_containers: container names to never flag (case-insensitive)
    system_networks  : names that DON'T provide DNS (default bridge/host/none)

Metrics: ``containers`` (seen), ``isolated`` (flagged).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass

from adapters import docker as docker_adapter
from adapters.docker import ContainerInfo
from core.registry import register
from core.types import ModuleResult, RunContext

ListContainersFn = Callable[[], list[ContainerInfo]]

_DEFAULT_SYSTEM_NETWORKS = ("bridge", "host", "none")


@dataclass(frozen=True)
class _Settings:
    shared_network: str
    ignore_containers: frozenset[str]
    system_networks: frozenset[str]


@dataclass(frozen=True)
class NetFinding:
    container: str
    networks: tuple[str, ...]
    suggestion: str  # advisory command, or "" when no shared_network configured


def _str(raw: object) -> str:
    return raw.strip() if isinstance(raw, str) and raw.strip() else ""


def _str_set(raw: object) -> set[str]:
    if not isinstance(raw, list):
        return set()
    return {s.strip() for s in raw if isinstance(s, str) and s.strip()}


def _settings(ctx: RunContext) -> _Settings:
    cfg = ctx.config.integrations.get("netdoctor", {})
    system = _str_set(cfg.get("system_networks")) or set(_DEFAULT_SYSTEM_NETWORKS)
    return _Settings(
        shared_network=_str(cfg.get("shared_network")),
        ignore_containers=frozenset(c.lower() for c in _str_set(cfg.get("ignore_containers"))),
        system_networks=frozenset(n.lower() for n in system),
    )


def is_isolated(networks: list[str], system_networks: frozenset[str]) -> bool:
    """True iff the container is on NO user-defined network (only system ones).

    A container with an empty network list is isolated too. Comparison is
    case-insensitive against the configured system-network names.
    """
    user_nets = [n for n in networks if n.strip() and n.strip().lower() not in system_networks]
    return not user_nets


def evaluate(containers: list[ContainerInfo], settings: _Settings) -> list[NetFinding]:
    """Return one finding per isolated, non-ignored container (pure)."""
    out: list[NetFinding] = []
    for info in containers:
        if info.name.lower() in settings.ignore_containers:
            continue
        if not is_isolated(info.networks, settings.system_networks):
            continue
        suggestion = (
            f"docker network connect {settings.shared_network} {info.name}"
            if settings.shared_network
            else ""
        )
        out.append(NetFinding(info.name, tuple(info.networks), suggestion))
    return out


def _write_report(
    ctx: RunContext, findings: list[NetFinding], settings: _Settings, note: str
) -> None:
    out_dir = ctx.config.reporting.dir / "netdoctor"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "plan.json").write_text(
        json.dumps(
            {
                "isolated": len(findings),
                "shared_network": settings.shared_network,
                "note": note,
                "findings": [
                    {
                        "container": f.container,
                        "networks": list(f.networks),
                        "suggestion": f.suggestion,
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
        "# Netdoctor — contenedores sin DNS de Docker (solo lectura)",
        "",
        f"Aislados (solo en redes de sistema): {len(findings)}",
        "",
        "> Causa de 'getaddrinfo ENOTFOUND': no comparten una red de usuario, así que",
        "> no se resuelven por nombre. Sugerencia ADVISORY (no se auto-aplica):",
        "",
        "## Contenedores aislados",
    ]
    for f in findings:
        nets = ", ".join(f.networks) or "(ninguna)"
        lines.append(f"- {f.container} [redes: {nets}]")
        if f.suggestion:
            lines.append(f"    → `{f.suggestion}`")
    if not findings:
        lines.append("(ninguno — todos comparten una red de usuario, o docker no accesible)")
    if note:
        lines += ["", f"> {note}"]
    (out_dir / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


@register("netdoctor")
def run(
    ctx: RunContext, *, list_fn: ListContainersFn = docker_adapter.list_containers
) -> ModuleResult:
    """Flag containers isolated from user-defined networks (the DNS root cause).

    ``list_fn`` is injected so tests run offline. Strictly read-only; the connect
    suggestion is advisory text, never executed (topology changes stay manual).
    """
    result = ModuleResult(module="netdoctor", run_id=ctx.run_id, mode=ctx.mode)
    settings = _settings(ctx)

    try:
        containers = list_fn()
    except Exception:  # pragma: no cover - list_containers already fails soft
        containers = []

    findings = evaluate(containers, settings)
    note = ""
    if not containers:
        note = "No se pudo listar contenedores (¿docker.sock montado?) — nada que analizar."
    elif not settings.shared_network:
        note = (
            "Configura integrations.netdoctor.shared_network con el nombre de la red de "
            "usuario compartida para obtener el comando 'docker network connect' sugerido."
        )

    _write_report(ctx, findings, settings, note)
    ctx.logger.info(
        "netdoctor done",
        containers=len(containers),
        isolated=len(findings),
    )
    result.metrics["containers"] = float(len(containers))
    result.metrics["isolated"] = float(len(findings))
    result.actions = 0  # read-only; the connect suggestion is advisory only
    return result
