"""Tests for core.registry (self-registration + discovery)."""

from __future__ import annotations

from core import registry
from core.types import ModuleResult, RunContext, SafetyMode


def _dummy(ctx: RunContext) -> ModuleResult:
    return ModuleResult(module="dummy", run_id="r", mode=SafetyMode.DRY_RUN)


def test_register_and_get() -> None:
    registry.register("dummy_test_module")(_dummy)
    assert registry.get("dummy_test_module") is _dummy
    assert "dummy_test_module" in registry.names()


def test_get_unknown_returns_none() -> None:
    assert registry.get("module_that_does_not_exist") is None


def test_discover_imports_and_registers_modules() -> None:
    registry.discover()
    found = registry.names()
    assert "noop" in found
    assert "docker_inventory" in found
