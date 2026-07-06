"""Tests for modules.ops.netdoctor (DNS-isolation detector, read-only)."""

from __future__ import annotations

import json
from pathlib import Path

from adapters.docker import ContainerInfo
from modules.ops import netdoctor
from tests.fakes import make_context


def _ct(name: str, networks: list[str]) -> ContainerInfo:
    return ContainerInfo(
        name=name, image="img", state="running", ports=[], networks=networks, mounts=[]
    )


def _read_plan(tmp_path: Path) -> dict[str, object]:
    path = tmp_path / "reports" / "netdoctor" / "plan.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


# --- is_isolated (pure) --------------------------------------------------------

_SYS = frozenset({"bridge", "host", "none"})


def test_isolated_when_only_system_networks() -> None:
    assert netdoctor.is_isolated(["bridge"], _SYS) is True
    assert netdoctor.is_isolated([], _SYS) is True
    assert netdoctor.is_isolated(["Bridge", "none"], _SYS) is True  # case-insensitive


def test_not_isolated_with_user_network() -> None:
    assert netdoctor.is_isolated(["bridge", "proxynet"], _SYS) is False
    assert netdoctor.is_isolated(["proxynet"], _SYS) is False


# --- evaluate ------------------------------------------------------------------


def test_evaluate_flags_isolated_with_suggestion() -> None:
    settings = netdoctor._Settings(
        shared_network="proxynet", ignore_containers=frozenset(), system_networks=_SYS
    )
    containers = [
        _ct("radarr", ["bridge"]),  # isolated
        _ct("plex", ["proxynet"]),  # fine
    ]
    findings = netdoctor.evaluate(containers, settings)
    assert [f.container for f in findings] == ["radarr"]
    assert findings[0].suggestion == "docker network connect proxynet radarr"


def test_evaluate_respects_ignore_and_missing_shared() -> None:
    settings = netdoctor._Settings(
        shared_network="", ignore_containers=frozenset({"watchtower"}), system_networks=_SYS
    )
    containers = [_ct("watchtower", ["bridge"]), _ct("sonarr", [])]
    findings = netdoctor.evaluate(containers, settings)
    assert [f.container for f in findings] == ["sonarr"]  # watchtower ignored
    assert findings[0].suggestion == ""  # no shared_network configured


# --- run -----------------------------------------------------------------------


def test_run_reports_isolated(tmp_path: Path) -> None:
    ctx = make_context(
        tmp_path,
        integrations={"netdoctor": {"shared_network": "proxynet"}},
    )
    containers = [_ct("radarr", ["bridge"]), _ct("sonarr", ["bridge"]), _ct("plex", ["proxynet"])]
    result = netdoctor.run(ctx, list_fn=lambda: containers)

    assert result.actions == 0
    assert result.metrics["containers"] == 3.0
    assert result.metrics["isolated"] == 2.0
    plan = _read_plan(tmp_path)
    assert plan["isolated"] == 2
    suggestions = {f["container"]: f["suggestion"] for f in plan["findings"]}  # type: ignore[union-attr]
    assert suggestions["radarr"] == "docker network connect proxynet radarr"


def test_run_no_docker_note(tmp_path: Path) -> None:
    ctx = make_context(tmp_path, integrations={"netdoctor": {"shared_network": "x"}})
    result = netdoctor.run(ctx, list_fn=lambda: [])
    assert result.ok
    assert "No se pudo listar" in _read_plan(tmp_path)["note"]  # type: ignore[operator]


def test_run_note_when_no_shared_network(tmp_path: Path) -> None:
    ctx = make_context(tmp_path)
    netdoctor.run(ctx, list_fn=lambda: [_ct("radarr", ["bridge"])])
    assert "shared_network" in _read_plan(tmp_path)["note"]  # type: ignore[operator]
