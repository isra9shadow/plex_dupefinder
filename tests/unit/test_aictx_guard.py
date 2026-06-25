"""Tests for the aictx Unraid-compatibility guard."""

from __future__ import annotations

from typing import Any

from aictx.guard import sanitize_payload, vet_command


def test_vetoes_non_unraid_commands() -> None:
    assert vet_command("systemctl restart docker") is False
    assert vet_command("service nginx start") is False
    assert vet_command("apt install foo") is False
    assert vet_command("apt-get update") is False
    assert vet_command("yum install bar") is False
    assert vet_command("dnf upgrade") is False
    assert vet_command("snap install baz") is False
    assert vet_command("docker-compose up -d") is False
    assert vet_command("docker compose up -d") is False
    assert vet_command("sudo systemctl daemon-reload") is False


def test_allows_unraid_safe_commands() -> None:
    assert vet_command("docker restart traefik") is True
    assert vet_command("chmod 600 acme.json") is True
    assert vet_command("docker logs plex") is True
    assert vet_command("ls -la /mnt/cache") is True


def test_sanitize_filters_and_reports_removed() -> None:
    payload: dict[str, Any] = {
        "summary": "x",
        "findings": [
            {
                "title": "f1",
                "unraid_commands": [
                    "docker restart traefik",
                    "systemctl restart docker",
                    "chmod 600 acme.json",
                    "docker-compose up -d",
                ],
            }
        ],
    }
    clean, removed = sanitize_payload(payload)
    assert clean["findings"][0]["unraid_commands"] == [
        "docker restart traefik",
        "chmod 600 acme.json",
    ]
    assert removed == ["systemctl restart docker", "docker-compose up -d"]
    # Original payload is not mutated.
    assert len(payload["findings"][0]["unraid_commands"]) == 4


def test_sanitize_defensive_about_malformed_shapes() -> None:
    clean, removed = sanitize_payload({})
    assert clean == {}
    assert removed == []

    weird: dict[str, Any] = {"findings": "not a list"}
    clean, removed = sanitize_payload(weird)
    assert removed == []

    weird2: dict[str, Any] = {"findings": [None, {"unraid_commands": "nope"}, 7]}
    clean, removed = sanitize_payload(weird2)
    assert removed == []
