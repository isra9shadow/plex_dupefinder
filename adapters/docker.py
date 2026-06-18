"""Read-only Docker introspection via the command adapter (no direct subprocess)."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from adapters import command
from adapters.command import CommandResult


@dataclass(frozen=True)
class ContainerInfo:
    name: str
    image: str
    state: str
    ports: list[str]
    networks: list[str]
    mounts: list[str]


def _as_dict(value: object) -> dict[str, object]:
    return {str(k): v for k, v in value.items()} if isinstance(value, dict) else {}


def _get_str(data: Mapping[str, object], key: str) -> str:
    value = data.get(key)
    return value if isinstance(value, str) else ""


def _parse_ports(netset: Mapping[str, object]) -> list[str]:
    out: set[str] = set()
    for container_port, bindings in _as_dict(netset.get("Ports")).items():
        if isinstance(bindings, list):
            for binding in bindings:
                host_port = _get_str(_as_dict(binding), "HostPort")
                if host_port:
                    out.add(f"{host_port}->{container_port}")
    return sorted(out)


def _parse_mounts(value: object) -> list[str]:
    out: list[str] = []
    if isinstance(value, list):
        for mount in value:
            md = _as_dict(mount)
            src, dst = _get_str(md, "Source"), _get_str(md, "Destination")
            if src or dst:
                out.append(f"{src}:{dst}")
    return out


def _parse_container(raw: object) -> ContainerInfo:
    data = _as_dict(raw)
    netset = _as_dict(data.get("NetworkSettings"))
    return ContainerInfo(
        name=_get_str(data, "Name").lstrip("/"),
        image=_get_str(_as_dict(data.get("Config")), "Image"),
        state=_get_str(_as_dict(data.get("State")), "Status"),
        ports=_parse_ports(netset),
        networks=sorted(_as_dict(netset.get("Networks"))),
        mounts=_parse_mounts(data.get("Mounts")),
    )


def list_containers(
    runner: Callable[[Sequence[str]], CommandResult] = command.run,
) -> list[ContainerInfo]:
    """Return every container (running or not). Empty list on any docker failure."""
    listing = runner(["docker", "ps", "-a", "--format", "{{.Names}}"])
    if not listing.ok:
        return []
    names = [n for n in listing.stdout.splitlines() if n.strip()]
    if not names:
        return []
    inspected = runner(["docker", "inspect", *names])
    if not inspected.ok or not inspected.stdout.strip():
        return []
    parsed: object = json.loads(inspected.stdout)
    if not isinstance(parsed, list):
        return []
    return [_parse_container(item) for item in parsed]
