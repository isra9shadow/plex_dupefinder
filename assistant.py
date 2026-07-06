#!/usr/bin/env python3
"""izumi assistant — answer an ops question in natural language (container entrypoint).

Routes the question to READ-ONLY modules, runs them, feeds their report summaries to
the local LLM (Ollama) and prints a Spanish answer. Runs in the container (which has
Ollama + jsonschema); the host bot invokes it via
``docker run … python assistant.py -q "<pregunta>"`` and relays stdout. If the LLM is
unreachable it prints the collected summaries instead (graceful degradation).

    python assistant.py -q "¿por qué falla mysql?" [--config PATH]
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from aictx import assistant as core
from core import config as config_mod
from core import registry
from core.types import RunContext
from run import build_context


def _read_summary(reports: Path, module: str) -> str:
    try:
        return (reports / module / "summary.md").read_text(encoding="utf-8")
    except OSError:
        return ""


def _run_and_read(ctx: RunContext, module: str) -> str:
    """Run a read-only module (best-effort) and return its freshest summary text."""
    fn = registry.get(module)
    if fn is not None:
        try:
            fn(ctx)
        except Exception as exc:  # a broken module must not sink the answer
            ctx.logger.warning("assistant module failed", module=module, error=str(exc))
    return _read_summary(ctx.config.reporting.dir, module)


def _llm_complete(ctx: RunContext, prompt: str) -> str:
    """Call Ollama with the assembled prompt (built from config.integrations.ollama)."""
    from integrations.ollama import OllamaClient

    cfg = ctx.config.integrations.get("ollama", {})
    kwargs: dict[str, object] = {}
    base = cfg.get("base_url")
    model = cfg.get("model")
    if isinstance(base, str) and base:
        kwargs["base_url"] = base
    if isinstance(model, str) and model:
        kwargs["model"] = model
    return OllamaClient(**kwargs).complete(prompt)  # type: ignore[arg-type]


def answer(question: str, *, config_path: str | None = None) -> str:
    """Full flow: route → run modules → LLM answer (fallback to raw summaries)."""
    cfg = config_mod.load(Path(config_path) if config_path else None)
    registry.discover()
    ctx = build_context(cfg)
    prompt, sections = core.assemble(question, lambda m: _run_and_read(ctx, m))
    try:
        return _llm_complete(ctx, prompt)
    except Exception as exc:  # Ollama down / not configured → degrade gracefully
        return core.fallback_answer(sections, str(exc))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="assistant.py", description="izumi NL ops assistant")
    parser.add_argument("-q", "--question", required=True, help="natural-language question")
    parser.add_argument("--config", default=None, help="path to config.json")
    args = parser.parse_args(argv)
    print(answer(args.question, config_path=args.config))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
