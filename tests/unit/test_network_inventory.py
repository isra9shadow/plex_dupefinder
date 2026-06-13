"""Tests for modules.inventory.network_inventory."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from adapters.docker import ContainerInfo
from modules.inventory import network_inventory as ni
from tests.fakes import make_context


def test_run_aggregates_networks_and_ports(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        ni.docker,
        "list_containers",
        lambda: [
            ContainerInfo("Plex", "img", "running", ["32400->32400/tcp"], ["proxy", "backend"], []),
            ContainerInfo("Sonarr", "img", "running", ["8989->8989/tcp"], ["backend"], []),
        ],
    )
    result = ni.run(make_context(tmp_path))

    assert result.actions == 2  # proxy, backend
    data = json.loads(
        (tmp_path / "reports" / "inventory" / "network_inventory.json").read_text(encoding="utf-8")
    )
    assert data["networks"]["backend"] == ["Plex", "Sonarr"]
    assert len(data["exposed_ports"]) == 2
