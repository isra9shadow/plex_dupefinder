"""Tests for modules.inventory.docker_inventory (collect → emit json+md)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from adapters.docker import ContainerInfo
from modules.inventory import docker_inventory
from tests.fakes import make_context


def test_docker_inventory_writes_json_and_md(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fixtures = [
        ContainerInfo(
            "Plex",
            "linuxserver/plex",
            "running",
            ["32400->32400/tcp"],
            ["proxy"],
            ["/mnt/user/media:/data"],
        ),
        ContainerInfo("Sonarr", "linuxserver/sonarr", "running", [], ["backend"], []),
    ]
    monkeypatch.setattr(docker_inventory.docker, "list_containers", lambda: fixtures)

    ctx = make_context(tmp_path)
    result = docker_inventory.run(ctx)

    assert result.actions == 2
    assert result.ok
    out = tmp_path / "reports" / "inventory"
    data = json.loads((out / "docker_inventory.json").read_text(encoding="utf-8"))
    assert {c["name"] for c in data} == {"Plex", "Sonarr"}
    md = (out / "docker_inventory.md").read_text(encoding="utf-8")
    assert "## Plex" in md
    assert "32400->32400/tcp" in md
    assert "2 containers" in md


def test_docker_inventory_empty(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(docker_inventory.docker, "list_containers", lambda: [])
    ctx = make_context(tmp_path)
    result = docker_inventory.run(ctx)
    assert result.actions == 0
    assert (tmp_path / "reports" / "inventory" / "docker_inventory.json").exists()
