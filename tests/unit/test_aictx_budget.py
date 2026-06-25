"""Tests for the aictx token-budget allocator."""

from __future__ import annotations

from aictx.budget import fit
from aictx.provider import ContextBlock, Tier


def _block(name: str, tier: Tier, tokens: int) -> ContextBlock:
    return ContextBlock(name=name, title=name, body="", tier=tier, token_hint=tokens)


def test_critical_always_kept_even_over_budget() -> None:
    blocks = [_block("c", Tier.CRITICAL, 1000)]
    kept = fit(blocks, budget_tokens=10)
    assert kept == blocks


def test_respects_budget_and_tier_priority() -> None:
    blocks = [
        _block("crit", Tier.CRITICAL, 10),
        _block("high", Tier.HIGH, 30),
        _block("med", Tier.MEDIUM, 30),
        _block("low", Tier.LOW, 30),
    ]
    # 10 (critical) + 30 (high) = 40; med/low would overflow a 50 budget.
    kept = fit(blocks, budget_tokens=50)
    names = [b.name for b in kept]
    assert names == ["crit", "high"]


def test_tier_order_high_then_medium_then_low() -> None:
    blocks = [
        _block("low", Tier.LOW, 10),
        _block("med", Tier.MEDIUM, 10),
        _block("high", Tier.HIGH, 10),
    ]
    kept = fit(blocks, budget_tokens=1000)
    assert [b.name for b in kept] == ["high", "med", "low"]


def test_preserves_input_order_within_tier() -> None:
    blocks = [
        _block("h1", Tier.HIGH, 5),
        _block("h2", Tier.HIGH, 5),
        _block("h3", Tier.HIGH, 5),
    ]
    kept = fit(blocks, budget_tokens=1000)
    assert [b.name for b in kept] == ["h1", "h2", "h3"]


def test_smaller_later_block_still_fits() -> None:
    blocks = [
        _block("big", Tier.HIGH, 100),
        _block("small", Tier.HIGH, 5),
    ]
    kept = fit(blocks, budget_tokens=10)
    assert [b.name for b in kept] == ["small"]


def test_empty_input() -> None:
    assert fit([], budget_tokens=100) == []
