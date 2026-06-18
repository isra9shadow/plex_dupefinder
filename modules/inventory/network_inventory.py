"""Network inventory (read-only): Docker networks + exposed ports (from inspect)."""

from __future__ import annotations

import json

from adapters import docker
from adapters.docker import ContainerInfo
from core.registry import register
from core.types import ModuleResult, RunContext


def _aggregate(
    containers: list[ContainerInfo],
) -> tuple[dict[str, list[str]], list[dict[str, str]]]:
    networks: dict[str, list[str]] = {}
    ports: list[dict[str, str]] = []
    for container in containers:
        for net in container.networks:
            networks.setdefault(net, []).append(container.name)
        for port in container.ports:
            ports.append({"container": container.name, "port": port})
    sorted_networks = {net: sorted(names) for net, names in sorted(networks.items())}
    return sorted_networks, sorted(ports, key=lambda p: (p["container"], p["port"]))


def _write_report(
    ctx: RunContext, networks: dict[str, list[str]], ports: list[dict[str, str]]
) -> None:
    out_dir = ctx.config.reporting.dir / "inventory"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "network_inventory.json").write_text(
        json.dumps({"networks": networks, "exposed_ports": ports}, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    lines = ["# Network Inventory", "", f"## Networks ({len(networks)})", ""]
    lines += [f"- {net}: {', '.join(names)}" for net, names in networks.items()]
    lines += ["", f"## Exposed ports ({len(ports)})", ""]
    lines += [f"- {p['port']} -> {p['container']}" for p in ports]
    (out_dir / "network_inventory.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


@register("network_inventory")
def run(ctx: RunContext) -> ModuleResult:
    result = ModuleResult(module="network_inventory", run_id=ctx.run_id, mode=ctx.mode)
    networks, ports = _aggregate(docker.list_containers())
    _write_report(ctx, networks, ports)
    ctx.logger.info("network inventory", networks=len(networks), ports=len(ports))
    result.actions = len(networks)
    return result
