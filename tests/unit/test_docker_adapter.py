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


# --- logwatch surface: container_names + logs (string list, merged streams) ----


def test_container_names_parses_and_trims() -> None:
    runner = _runner({"ps": CommandResult(("docker", "ps"), 0, "Plex\n  Sonarr \n\n", "")})
    assert docker.container_names(runner=runner) == ["Plex", "Sonarr"]


def test_container_names_empty_on_failure() -> None:
    runner = _runner({"ps": CommandResult(("docker", "ps"), 1, "", "boom")})
    assert docker.container_names(runner=runner) == []


def test_logs_merges_stdout_and_stderr() -> None:
    captured: list[Sequence[str]] = []

    def runner(argv: Sequence[str]) -> CommandResult:
        captured.append(argv)
        return CommandResult(tuple(argv), 0, "out-line\n", "err-line\n")

    out = docker.logs("Plex", since_days=3.0, runner=runner)
    assert out == "out-line\nerr-line\n"  # both streams combined
    # days -> hours (3*24=72), timestamps requested, name passed through.
    assert captured[0] == ["docker", "logs", "--since", "72h", "--timestamps", "Plex"]


def test_logs_since_days_clamped_to_one_hour() -> None:
    captured: list[Sequence[str]] = []

    def runner(argv: Sequence[str]) -> CommandResult:
        captured.append(argv)
        return CommandResult(tuple(argv), 0, "", "")

    docker.logs("Plex", since_days=0.0, runner=runner)
    assert "1h" in list(captured[0])  # never asks docker for 0h


def test_logs_empty_on_total_failure() -> None:
    def runner(argv: Sequence[str]) -> CommandResult:
        return CommandResult(tuple(argv), 1, "", "")

    assert docker.logs("Nope", since_days=1.0, runner=runner) == ""


def test_logs_returns_partial_output_even_when_nonzero() -> None:
    # docker may exit non-zero yet still have emitted useful log lines.
    def runner(argv: Sequence[str]) -> CommandResult:
        return CommandResult(tuple(argv), 1, "", "partial-error\n")

    assert docker.logs("Plex", since_days=1.0, runner=runner) == "partial-error\n"
