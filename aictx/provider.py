"""Frozen contracts for the AI context layer (FREEZE — built against in parallel).

Pure types only; importing this module never performs IO. The PromptBuilder, the
ContextProviders and the per-task templates all agree on the shapes here, so they
can be developed independently without drifting.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import Protocol, runtime_checkable


class Tier(IntEnum):
    """Inclusion priority when the token budget is tight (lower = kept first)."""

    CRITICAL = 0  # system facts + capabilities — always sent
    HIGH = 1  # inventory, runtime, module task data
    MEDIUM = 2  # history / recurrence
    LOW = 3  # optional extras


def estimate_tokens(text: str) -> int:
    """Cheap token estimate (~4 chars/token) — no tokenizer dependency."""
    return (len(text) + 3) // 4


@dataclass(frozen=True)
class ContextBlock:
    """One self-contained chunk of context contributed by a provider.

    ``stable`` marks a block that does not change between calls (system facts,
    inventory) so the builder can keep it as a reusable prefix (KV-cache friendly).
    ``token_hint`` overrides the estimate when the provider already knows the cost.
    """

    name: str
    title: str
    body: str
    tier: Tier
    stable: bool = False
    token_hint: int = 0

    @property
    def tokens(self) -> int:
        return self.token_hint or estimate_tokens(self.title) + estimate_tokens(self.body)

    def render(self) -> str:
        return f"## {self.title}\n{self.body}".strip()


@runtime_checkable
class ContextProvider(Protocol):
    """Produces one ContextBlock, or None when it has nothing to contribute.

    Providers are constructed with whatever inputs they need (RunContext, parsed
    data, ...) and expose a no-arg ``block()`` so the builder stays decoupled.
    """

    name: str

    def block(self) -> ContextBlock | None: ...


@dataclass(frozen=True)
class PromptTemplate:
    """Declarative per-task prompt spec — no giant inline prompts in modules."""

    task: str
    system_role: str  # short persona/role line (goes in the system message)
    instructions: str  # what to produce for THIS task
    schema: dict[str, object]  # JSON schema the model's output must satisfy
    provider_order: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class BuiltPrompt:
    """Result of ``PromptBuilder.build`` — ready for ``OllamaClient.generate_json``."""

    system: str
    prompt: str
    schema: dict[str, object]
    tokens: int
