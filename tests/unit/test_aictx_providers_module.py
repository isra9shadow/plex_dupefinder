"""Tests for aictx.providers.module.ModuleContextProvider."""

from __future__ import annotations

from aictx.provider import ContextProvider, Tier
from aictx.providers.module import ModuleContextProvider


def test_module_provider_wraps_body() -> None:
    provider = ModuleContextProvider("Datos", "linea de datos")
    assert isinstance(provider, ContextProvider)
    block = provider.block()
    assert block is not None
    assert block.name == "module"
    assert block.title == "Datos"
    assert block.tier == Tier.HIGH
    assert "linea de datos" in block.body


def test_module_provider_empty_body_is_none() -> None:
    assert ModuleContextProvider("Datos", "   ").block() is None
