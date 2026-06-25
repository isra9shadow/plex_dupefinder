"""ModuleContextProvider — wraps a module's own precomputed task data as a block.

Each module (analyst, logwatch, organizer…) shapes its domain data into a compact
markdown body and hands it here, so the PromptBuilder treats it like any other
context source (``name="module"``, HIGH tier). Returns None for an empty body.
"""

from __future__ import annotations

from aictx.provider import ContextBlock, Tier


class ModuleContextProvider:
    """A ready-made context block contributed by the calling module."""

    name = "module"

    def __init__(self, title: str, body: str, *, tier: Tier = Tier.HIGH) -> None:
        self._title = title
        self._body = body
        self._tier = tier

    def block(self) -> ContextBlock | None:
        if not self._body.strip():
            return None
        return ContextBlock(name="module", title=self._title, body=self._body, tier=self._tier)
