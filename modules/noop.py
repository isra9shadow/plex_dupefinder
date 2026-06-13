"""A no-op module: smoke-test target and wiring verification (self-registered)."""

from __future__ import annotations

from core.registry import register
from core.types import ModuleResult, RunContext


@register("noop")
def run(ctx: RunContext) -> ModuleResult:
    ctx.logger.info("noop module ran", mode=str(ctx.mode))
    return ModuleResult(module="noop", run_id=ctx.run_id, mode=ctx.mode)
