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


_LOGS_TIMEOUT = 300.0


def _run_logs(argv: Sequence[str]) -> CommandResult:  # pragma: no cover - thin default
    """Default ``logs`` runner: ``command.run`` with a generous timeout (logs are big)."""
    return command.run(argv, timeout=_LOGS_TIMEOUT)


def container_names(
    *,
    include_stopped: bool = False,
    runner: Callable[[Sequence[str]], CommandResult] = command.run,
) -> list[str]:
    """Container names. ``include_stopped`` adds ``-a`` (running + exited/crashed —
    where errors often live). Empty list on any docker failure (never raises)."""
    argv = ["docker", "ps", "--format", "{{.Names}}"]
    if include_stopped:
        argv.insert(2, "-a")
    listing = runner(argv)
    if not listing.ok:
        return []
    return [n.strip() for n in listing.stdout.splitlines() if n.strip()]


def logs(
    name: str,
    *,
    since_days: float,
    tail: int = 0,
    runner: Callable[[Sequence[str]], CommandResult] = _run_logs,
) -> str:
    """Combined stdout+stderr of ``name``'s logs since ``since_days`` ago.

    Docker writes most application output to stderr, so both streams are merged.
    Returns "" on any docker failure; never raises. ``since_days`` is converted to
    whole hours for ``docker logs --since`` (clamped to at least 1h). ``tail`` > 0
    also caps the output to the last N lines (bounds a week of huge logs).
    """
    hours = max(1, int(since_days * 24))
    argv = ["docker", "logs", "--since", f"{hours}h", "--timestamps"]
    if tail > 0:
        argv += ["--tail", str(tail)]
    argv.append(name)
    result = runner(argv)
    if not result.ok and not result.stdout and not result.stderr:
        return ""
    return result.stdout + result.stderr
