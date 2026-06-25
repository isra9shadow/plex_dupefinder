"""Unit tests for the fixed (system + capabilities) aictx providers."""

from __future__ import annotations

import json
from pathlib import Path

from aictx.provider import ContextProvider, Tier
from aictx.providers.capabilities import CapabilitiesContextProvider
from aictx.providers.system import SystemContextProvider


def _write_facts(tmp_path: Path) -> Path:
    facts = {
        "os": "Unraid 7",
        "package_management": "none (no apt/yum/dnf/snap)",
        "hardware": {"gpu": "NVIDIA RTX 4060 (8 GB VRAM)", "ram_gb": 32, "role": "home server"},
        "paths": {"media": "/mnt/user/media", "cache": "/mnt/cache"},
        "services": [{"name": "Plex", "role": "media server"}],
        "forbidden_commands": ["systemctl", "apt", "docker-compose"],
        "constraints": ["Never recommend commands incompatible with Unraid"],
    }
    path = tmp_path / "homelab_facts.json"
    path.write_text(json.dumps(facts), encoding="utf-8")
    return path


def test_system_provider_satisfies_protocol() -> None:
    assert isinstance(SystemContextProvider(), ContextProvider)


def test_system_block_with_tmp_facts(tmp_path: Path) -> None:
    provider = SystemContextProvider(facts_path=_write_facts(tmp_path))
    block = provider.block()
    assert block is not None
    assert block.name == "system"
    assert block.tier is Tier.CRITICAL
    assert block.stable is True
    assert "Unraid" in block.body
    # A forbidden command must be named in the body.
    assert "docker-compose" in block.body
    assert "systemctl" in block.body


def test_system_block_missing_file_does_not_crash(tmp_path: Path) -> None:
    provider = SystemContextProvider(facts_path=tmp_path / "does_not_exist.json")
    block = provider.block()
    assert block is not None
    assert block.tier is Tier.CRITICAL
    assert "Unraid" in block.body
    assert "systemctl" in block.body


def test_system_block_bad_json_does_not_crash(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{ not valid json", encoding="utf-8")
    provider = SystemContextProvider(facts_path=bad)
    block = provider.block()
    assert block is not None
    assert "Unraid" in block.body


def test_capabilities_provider_satisfies_protocol() -> None:
    assert isinstance(CapabilitiesContextProvider(), ContextProvider)


def test_capabilities_block_states_limits() -> None:
    block = CapabilitiesContextProvider().block()
    assert block is not None
    assert block.name == "capabilities"
    assert block.tier is Tier.CRITICAL
    assert block.stable is True
    lowered = block.body.lower()
    assert "no puedes" in lowered
    assert "ejecutar" in lowered
    assert "borrar" in lowered
    assert "fact" in lowered
    assert "inference" in lowered
    assert "hypothesis" in lowered
