"""PromptBuilder — assembles context blocks under a token budget.

The builder gathers blocks from the configured providers, pins the stable
CRITICAL blocks (+ the template role) into a cacheable ``system`` prefix, budgets
the remaining blocks against the leftover context window and renders them, with
the task instructions, into the ``prompt``. The result is a ``BuiltPrompt`` ready
for ``OllamaClient.generate_json``.
"""

from __future__ import annotations

from collections.abc import Sequence

from aictx import budget
from aictx.provider import (
    BuiltPrompt,
    ContextBlock,
    ContextProvider,
    PromptTemplate,
    Tier,
    estimate_tokens,
)


class PromptBuilder:
    """Compose providers + a template into a budgeted, cache-friendly prompt."""

    def __init__(self, *, num_ctx: int = 8192, response_reserve: int = 1024) -> None:
        self.num_ctx = num_ctx
        self.response_reserve = response_reserve

    def build(self, template: PromptTemplate, providers: Sequence[ContextProvider]) -> BuiltPrompt:
        """Build a ``BuiltPrompt`` from ``template`` and ``providers``."""
        blocks: list[ContextBlock] = []
        for provider in providers:
            block = provider.block()
            if block is not None:
                blocks.append(block)

        critical = [b for b in blocks if b.tier == Tier.CRITICAL]
        rest = [b for b in blocks if b.tier != Tier.CRITICAL]

        system = self._render_system(template, critical)
        system_tokens = estimate_tokens(system)

        remaining_budget = self.num_ctx - self.response_reserve - system_tokens
        kept = budget.fit(rest, remaining_budget)
        ordered = self._order(kept, template.provider_order)

        prompt = self._render_prompt(template, ordered)

        total_tokens = estimate_tokens(system) + estimate_tokens(prompt)
        return BuiltPrompt(
            system=system,
            prompt=prompt,
            schema=template.schema,
            tokens=total_tokens,
        )

    @staticmethod
    def _render_system(template: PromptTemplate, critical: list[ContextBlock]) -> str:
        parts = [template.system_role.strip()]
        parts.extend(block.render() for block in critical)
        return "\n\n".join(part for part in parts if part).strip()

    @staticmethod
    def _render_prompt(template: PromptTemplate, blocks: list[ContextBlock]) -> str:
        parts = [block.render() for block in blocks]
        instructions = template.instructions.strip()
        if instructions:
            parts.append(instructions)
        return "\n\n".join(part for part in parts if part).strip()

    @staticmethod
    def _order(blocks: list[ContextBlock], provider_order: tuple[str, ...]) -> list[ContextBlock]:
        order_index = {name: i for i, name in enumerate(provider_order)}
        fallback = len(provider_order)

        def key(item: tuple[int, ContextBlock]) -> tuple[int, int, int]:
            idx, block = item
            return (order_index.get(block.name, fallback), int(block.tier), idx)

        return [block for _, block in sorted(enumerate(blocks), key=key)]
