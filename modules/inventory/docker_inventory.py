"""Docker inventory: containers, images, ports, networks, mounts.

Read-only (via the docker adapter). Emits `reports/inventory/docker_inventory.{json,md}`
— living documentation that replaces the hand-maintained homelab-infra docker docs.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from adapters import docker
from adapters.docker import ContainerInfo
from core.registry import register
from core.types import ModuleResult, RunContext


def _render_md(containers: list[ContainerInfo]) -> str:
    lines = ["# Docker Inventory", "", f"_{len(containers)} containers_", ""]
    for c in containers:
        lines += [
            f"## {c.name}",
            "",
            f"- Image: `{c.image}`",
            f"- State: {c.state}",
            f"- Ports: {', '.join(c.ports) or '—'}",
            f"- Networks: {', '.join(c.networks) or '—'}",
            f"- Mounts: {len(c.mounts)}",
            "",
        ]
    return "\n".join(lines)


def _write(directory: Path, containers: list[ContainerInfo]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    payload = [asdict(c) for c in containers]
    (directory / "docker_inventory.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8"
    )
    (directory / "docker_inventory.md").write_text(_render_md(containers), encoding="utf-8")


@register("docker_inventory")
def run(ctx: RunContext) -> ModuleResult:
    containers = sorted(docker.list_containers(), key=lambda c: c.name.lower())
    out_dir = ctx.config.reporting.dir / "inventory"
    _write(out_dir, containers)
    ctx.logger.info("docker inventory written", containers=len(containers), dir=str(out_dir))
    return ModuleResult(
        module="docker_inventory",
        run_id=ctx.run_id,
        mode=ctx.mode,
        actions=len(containers),
    )
