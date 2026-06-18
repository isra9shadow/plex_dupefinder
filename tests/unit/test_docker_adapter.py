"""Tests for adapters.docker (read-only docker introspection, parsing)."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence

from adapters import docker
from adapters.command import CommandResult

_INSPECT = [
    {
        "Name": "/Plex",
        "Config": {"Image": "linuxserver/plex"},
        "State": {"Status": "running"},
        "Mounts": [{"Source": "/mnt/user/media", "Destination": "/data"}],
        "NetworkSettings": {
            "Ports": {"32400/tcp": [{"HostPort": "32400"}]},
            "Networks": {"proxy": {}},
        },
    }
]


def _runner(results: dict[str, CommandResult]) -> Callable[[Sequence[str]], CommandResult]:
    def run(argv: Sequence[str]) -> CommandResult:
        return results[argv[1]]

    return run


def test_list_containers_parses() -> None:
    runner = _runner(
        {
            "ps": CommandResult(("docker", "ps"), 0, "Plex\n", ""),
            "inspect": CommandResult(("docker", "inspect"), 0, json.dumps(_INSPECT), ""),
        }
    )
    containers = docker.list_containers(runner=runner)
    assert len(containers) == 1
    c = containers[0]
    assert c.name == "Plex"
    assert c.image == "linuxserver/plex"
    assert c.state == "running"
    assert c.ports == ["32400->32400/tcp"]
    assert c.networks == ["proxy"]
    assert c.mounts == ["/mnt/user/media:/data"]


def test_empty_when_ps_fails() -> None:
    runner = _runner({"ps": CommandResult(("docker", "ps"), 1, "", "boom")})
    assert docker.list_containers(runner=runner) == []


def test_empty_when_no_names() -> None:
    runner = _runner({"ps": CommandResult(("docker", "ps"), 0, "\n  \n", "")})
    assert docker.list_containers(runner=runner) == []


def test_empty_when_inspect_fails() -> None:
    runner = _runner(
        {
            "ps": CommandResult(("docker", "ps"), 0, "Plex\n", ""),
            "inspect": CommandResult(("docker", "inspect"), 1, "", "boom"),
        }
    )
    assert docker.list_containers(runner=runner) == []


def test_empty_when_inspect_not_list() -> None:
    runner = _runner(
        {
            "ps": CommandResult(("docker", "ps"), 0, "Plex\n", ""),
            "inspect": CommandResult(("docker", "inspect"), 0, "{}", ""),
        }
    )
    assert docker.list_containers(runner=runner) == []
