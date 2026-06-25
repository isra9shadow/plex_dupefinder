"""Tests for the aictx PromptBuilder."""

from __future__ import annotations

from aictx.builder import PromptBuilder
from aictx.provider import ContextBlock, PromptTemplate, Tier


class _FakeProvider:
    def __init__(self, block: ContextBlock | None) -> None:
        self.name = block.name if block is not None else "none"
        self._block = block

    def block(self) -> ContextBlock | None:
        return self._block


def _template(provider_order: tuple[str, ...] = ()) -> PromptTemplate:
    return PromptTemplate(
        task="diagnose",
        system_role="Eres un asistente de homelab.",
        instructions="Produce un diagnóstico.",
        schema={"type": "object"},
        provider_order=provider_order,
    )


def test_critical_goes_to_system_rest_to_prompt() -> None:
    crit = ContextBlock("facts", "Hechos", "unraid host", Tier.CRITICAL, stable=True)
    high = ContextBlock("inv", "Inventario", "docker ps", Tier.HIGH)
    builder = PromptBuilder()
    built = builder.build(_template(), [_FakeProvider(crit), _FakeProvider(high)])

    assert "Eres un asistente de homelab." in built.system
    assert "## Hechos" in built.system
    assert "unraid host" in built.system
    # CRITICAL must NOT leak into the prompt body.
    assert "## Hechos" not in built.prompt
    assert "## Inventario" in built.prompt
    assert "Produce un diagnóstico." in built.prompt


def test_skips_none_blocks() -> None:
    builder = PromptBuilder()
    built = builder.build(_template(), [_FakeProvider(None)])
    assert built.prompt.strip().endswith("Produce un diagnóstico.")
    assert "##" not in built.prompt


def test_provider_order_honored() -> None:
    a = ContextBlock("alpha", "Alpha", "a", Tier.HIGH)
    b = ContextBlock("beta", "Beta", "b", Tier.HIGH)
    builder = PromptBuilder()
    tmpl = _template(provider_order=("beta", "alpha"))
    built = builder.build(tmpl, [_FakeProvider(a), _FakeProvider(b)])
    assert built.prompt.index("## Beta") < built.prompt.index("## Alpha")


def test_tier_order_when_no_provider_order() -> None:
    low = ContextBlock("low", "Low", "l", Tier.LOW)
    high = ContextBlock("high", "High", "h", Tier.HIGH)
    builder = PromptBuilder()
    built = builder.build(_template(), [_FakeProvider(low), _FakeProvider(high)])
    assert built.prompt.index("## High") < built.prompt.index("## Low")


def test_budget_drops_low_priority_blocks() -> None:
    big = ContextBlock("big", "Big", "x", Tier.LOW, token_hint=10_000)
    keep = ContextBlock("keep", "Keep", "y", Tier.HIGH, token_hint=5)
    builder = PromptBuilder(num_ctx=200, response_reserve=10)
    built = builder.build(_template(), [_FakeProvider(big), _FakeProvider(keep)])
    assert "## Keep" in built.prompt
    assert "## Big" not in built.prompt


def test_schema_and_tokens_set() -> None:
    builder = PromptBuilder()
    built = builder.build(_template(), [])
    assert built.schema == {"type": "object"}
    assert built.tokens > 0
