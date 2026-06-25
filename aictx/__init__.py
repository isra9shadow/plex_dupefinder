"""aictx — the AI context layer.

Builds high-signal, low-token, homelab-specific prompts for the local Ollama
model and validates its structured output. Composition:

  provider.py   frozen contracts: ContextBlock / ContextProvider / PromptTemplate
                / BuiltPrompt / Tier (everything else is built against these)
  schema.py     JSON Schemas for the model's structured output + validation
  budget.py     token-budget allocator (keep CRITICAL, compact the rest)
  builder.py    PromptBuilder — assembles blocks under budget into a BuiltPrompt
  guard.py      deterministic post-guard (vetoes non-Unraid commands)
  render.py     validated JSON -> human markdown
  providers/    ContextProviders (system, capabilities, inventory, runtime, history)
  templates/    one declarative PromptTemplate per task

Pure library: modules import aictx; aictx never imports a module.
"""

from __future__ import annotations
