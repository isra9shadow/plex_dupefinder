"""Share inventory (read-only): /boot/config/shares/*.cfg → cache/allocation/disks."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from core.registry import register
from core.types import ModuleResult, RunContext

_CACHE_LABEL = {
    "no": "array-only",
    "yes": "cache->array (mover)",
    "only": "cache-only",
    "prefer": "prefer-cache",
}


@dataclass(frozen=True)
class ShareInfo:
    name: str
    cache: str
    allocator: str
    include: list[str]
    exclude: list[str]
    comment: str


def parse_share_cfg(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip().strip('"')
    return out


def _split(value: str) -> list[str]:
    return [item for item in value.split(",") if item]


def _to_share(name: str, cfg: dict[str, str]) -> ShareInfo:
    use_cache = cfg.get("shareUseCache", "")
    return ShareInfo(
        name=name,
        cache=_CACHE_LABEL.get(use_cache, use_cache or "unknown"),
        allocator=cfg.get("shareAllocator", "") or "default",
        include=_split(cfg.get("shareInclude", "")),
        exclude=_split(cfg.get("shareExclude", "")),
        comment=cfg.get("shareComment", ""),
    )


def _shares_dir(ctx: RunContext) -> Path:
    return Path(ctx.config.paths.get("shares_cfg_dir", "/boot/config/shares"))


def _write_report(ctx: RunContext, shares: list[ShareInfo]) -> None:
    out_dir = ctx.config.reporting.dir / "inventory"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "share_inventory.json").write_text(
        json.dumps([asdict(s) for s in shares], indent=2, sort_keys=True), encoding="utf-8"
    )
    lines = ["# Share Inventory", "", f"## Shares ({len(shares)})", ""]
    for share in shares:
        lines.append(
            f"- {share.name} — cache: {share.cache} · "
            f"include: {', '.join(share.include) or 'all'} · "
            f"exclude: {', '.join(share.exclude) or 'none'}"
        )
    (out_dir / "share_inventory.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


@register("share_inventory")
def run(ctx: RunContext) -> ModuleResult:
    result = ModuleResult(module="share_inventory", run_id=ctx.run_id, mode=ctx.mode)
    shares = [
        _to_share(cfg.stem, parse_share_cfg(cfg.read_text(encoding="utf-8", errors="ignore")))
        for cfg in sorted(_shares_dir(ctx).glob("*.cfg"))
    ]
    _write_report(ctx, shares)
    ctx.logger.info("share inventory", shares=len(shares))
    result.actions = len(shares)
    return result
