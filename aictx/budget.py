"""Token-budget allocator for the AI context layer.

Decides which ContextBlocks survive when the prompt would otherwise overflow the
model's context window. CRITICAL blocks are non-negotiable (always kept, even if
they alone exceed the budget); the remaining tiers are added in priority order
(HIGH -> MEDIUM -> LOW) for as long as the running token total fits.
"""

from __future__ import annotations

from aictx.provider import ContextBlock, Tier


def fit(blocks: list[ContextBlock], budget_tokens: int) -> list[ContextBlock]:
    """Select blocks that fit within ``budget_tokens`` by tier priority.

    All ``Tier.CRITICAL`` blocks are kept unconditionally (even if they exceed the
    budget). Then HIGH, MEDIUM and LOW blocks are added in that order while the
    running total of ``block.tokens`` stays ``<= budget_tokens``. Input order is
    preserved within each tier. Blocks that would push the total over budget are
    skipped, but a later, smaller block in the same/next tier may still fit.
    """
    kept: list[ContextBlock] = []
    used = 0

    critical = [b for b in blocks if b.tier == Tier.CRITICAL]
    for block in critical:
        kept.append(block)
        used += block.tokens

    for tier in (Tier.HIGH, Tier.MEDIUM, Tier.LOW):
        for block in blocks:
            if block.tier != tier:
                continue
            if used + block.tokens <= budget_tokens:
                kept.append(block)
                used += block.tokens

    return kept
