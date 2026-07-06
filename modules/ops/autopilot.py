"""Autopilot — policy-based self-healing (auto-apply SAFE fixes, no operator prompt).

Reads the guard-vetted proposals that other modules already produce (``autoheal``'s
``docker restart``, ``permsdoctor``'s ``chmod``/``chown``) and, for a configured
policy allow-list, applies them AUTOMATICALLY — with two brakes so it can't run wild:

  * **Cooldown**: never re-apply the same finding within ``cooldown_minutes``.
  * **Anti-flapping**: if a finding has been auto-applied more than ``max_flaps``
    times within ``flap_window_hours``, stop auto-applying it (a restart loop is a
    real problem, not something to paper over) and surface it for a human.

Safety:
  * DRY_RUN by default (I2). In DRY_RUN it PLANS (reports what it *would* auto-heal);
    only in LIVE does it apply.
  * It applies ONLY through :func:`aictx.apply.apply_action` (re-classified allow-list,
    the single audited boundary) and ONLY for categories the operator put in
    ``policy_categories``. Risky fixes (e.g. dbrepair) are never in that list, so
    they stay operator-confirmed.
  * A tiny JSON ledger (``core.cache.Cache``) records apply timestamps per finding
    for the cooldown/flap logic.

Config (config.json), under ``integrations.autopilot``:
  policy_categories : apply categories allowed to auto-apply
                      (subset of docker-lifecycle | chmod | chown | mkdir; default [])
  sources           : plan.json subdirs to read (default: autoheal, permsdoctor)
  cooldown_minutes  : min minutes between auto-applies of the same finding (default 60)
  max_flaps         : max auto-applies of a finding within the window (default 3)
  flap_window_hours : the flap window (default 24)

Metrics: ``applied`` (auto-healed), ``skipped`` (cooldown/flap/not-in-policy), ``candidates``.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from aictx.apply import (
    ApplyAction,
    ApplyOutcome,
    Runner,
    apply_action,
    collect_actions,
    default_runner,
)
from core.cache import Cache
from core.registry import register
from core.types import FailureRecord, ModuleResult, RunContext, SafetyMode

_DEFAULT_SOURCES = ("autoheal", "permsdoctor")
_DEFAULT_COOLDOWN_MIN = 60.0
_DEFAULT_MAX_FLAPS = 3
_DEFAULT_FLAP_WINDOW_H = 24.0
_LEDGER_KEY = "applied_ts"  # cache key -> {fingerprint: [epoch_seconds, ...]}


@dataclass(frozen=True)
class _Policy:
    categories: frozenset[str]
    sources: tuple[str, ...]
    cooldown_s: float
    max_flaps: int
    flap_window_s: float


@dataclass
class Decision:
    action: ApplyAction
    verdict: str  # apply | cooldown | flapping | not_in_policy
    detail: str = ""


@dataclass
class Outcome:
    applied: list[str] = field(default_factory=list)
    skipped: list[tuple[str, str, str]] = field(default_factory=list)  # (command, verdict, detail)
    failed: list[tuple[str, str]] = field(default_factory=list)  # (command, error)


def _str_set(raw: object) -> set[str]:
    if not isinstance(raw, list):
        return set()
    return {s.strip() for s in raw if isinstance(s, str) and s.strip()}


def _pos_float(raw: object, default: float) -> float:
    return (
        float(raw)
        if isinstance(raw, int | float) and not isinstance(raw, bool) and raw > 0
        else default
    )


def _pos_int(raw: object, default: int) -> int:
    return raw if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0 else default


def _policy(ctx: RunContext) -> _Policy:
    cfg = ctx.config.integrations.get("autopilot", {})
    sources = tuple(_str_set(cfg.get("sources"))) or _DEFAULT_SOURCES
    return _Policy(
        categories=frozenset(_str_set(cfg.get("policy_categories"))),
        sources=sources,
        cooldown_s=_pos_float(cfg.get("cooldown_minutes"), _DEFAULT_COOLDOWN_MIN) * 60.0,
        max_flaps=_pos_int(cfg.get("max_flaps"), _DEFAULT_MAX_FLAPS),
        flap_window_s=_pos_float(cfg.get("flap_window_hours"), _DEFAULT_FLAP_WINDOW_H) * 3600.0,
    )


def plan(
    actions: list[ApplyAction],
    *,
    policy: _Policy,
    ledger: dict[str, list[float]],
    now: float,
) -> list[Decision]:
    """Decide, per action, whether policy auto-heal applies (pure, using the ledger).

    An action is applied only if its category is in the policy AND it is not in
    cooldown AND it is not flapping (too many recent auto-applies of the same
    finding). Everything else is skipped with a reason.
    """
    out: list[Decision] = []
    for action in actions:
        if action.category not in policy.categories:
            out.append(Decision(action, "not_in_policy", action.category))
            continue
        stamps = [t for t in ledger.get(action.fingerprint, []) if now - t <= policy.flap_window_s]
        if any(now - t < policy.cooldown_s for t in stamps):
            out.append(
                Decision(action, "cooldown", f"aplicado hace <{policy.cooldown_s / 60:.0f}m")
            )
            continue
        if len(stamps) >= policy.max_flaps:
            out.append(
                Decision(action, "flapping", f"{len(stamps)} auto-aplicaciones en la ventana")
            )
            continue
        out.append(Decision(action, "apply"))
    return out


def _plan_paths(ctx: RunContext, sources: tuple[str, ...]) -> list[Path]:
    reports = ctx.config.reporting.dir
    return [reports / sub / "plan.json" for sub in sources]


def _write_report(ctx: RunContext, outcome: Outcome, dry_run: bool, note: str) -> None:
    out_dir = ctx.config.reporting.dir / "autopilot"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "plan.json").write_text(
        json.dumps(
            {
                "dry_run": dry_run,
                "applied": outcome.applied,
                "skipped": [
                    {"command": c, "verdict": v, "detail": d} for c, v, d in outcome.skipped
                ],
                "failed": [{"command": c, "error": e} for c, e in outcome.failed],
                "note": note,
            },
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    verb = "auto-aplicaría" if dry_run else "auto-aplicado"
    lines = [
        "# Autopilot — self-healing por políticas",
        "",
        f"Modo: {'DRY-RUN (solo plan)' if dry_run else 'LIVE'}",
        f"{verb}: {len(outcome.applied)} · omitido: {len(outcome.skipped)} · "
        f"fallado: {len(outcome.failed)}",
        "",
    ]
    for cmd in outcome.applied:
        lines.append(f"- ✅ `{cmd}`")
    for cmd, verdict, detail in outcome.skipped:
        lines.append(f"- ⏭️ `{cmd}` ({verdict}{': ' + detail if detail else ''})")
    for cmd, err in outcome.failed:
        lines.append(f"- ❌ `{cmd}` — {err}")
    if note:
        lines += ["", f"> {note}"]
    (out_dir / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


@register("autopilot")
def run(
    ctx: RunContext, *, runner: Runner = default_runner, now: float | None = None
) -> ModuleResult:
    """Auto-apply policy-allowed guard-vetted fixes with cooldown + anti-flapping.

    ``runner`` (the apply boundary) and ``now`` are injected for offline tests.
    DRY_RUN plans only; LIVE applies and records timestamps to the ledger.
    """
    result = ModuleResult(module="autopilot", run_id=ctx.run_id, mode=ctx.mode)
    policy = _policy(ctx)
    dry_run = ctx.mode != SafetyMode.LIVE
    when = now if now is not None else time.time()

    actions = collect_actions(_plan_paths(ctx, policy.sources))
    cache = Cache(ctx.config.reporting.dir / "cache" / "autopilot.json")
    raw_ledger = cache.get(_LEDGER_KEY)
    ledger: dict[str, list[float]] = {}
    if isinstance(raw_ledger, dict):
        for fp, stamps in raw_ledger.items():
            if isinstance(stamps, list):
                ledger[str(fp)] = [float(t) for t in stamps if isinstance(t, int | float)]

    decisions = plan(actions, policy=policy, ledger=ledger, now=when)
    outcome = Outcome()
    note = ""
    if not policy.categories:
        note = (
            "policy_categories vacío — autopilot no auto-aplicará nada. Añade categorías "
            "seguras (p.ej. docker-lifecycle, chmod, chown) para activarlo."
        )

    for d in decisions:
        if d.verdict != "apply":
            outcome.skipped.append((d.action.command, d.verdict, d.detail))
            continue
        if dry_run:
            outcome.skipped.append((d.action.command, "would_apply", "dry-run"))
            continue
        result_apply: ApplyOutcome = apply_action(d.action, runner=runner)
        if result_apply.ok:
            outcome.applied.append(d.action.command)
            ledger.setdefault(d.action.fingerprint, []).append(when)
        else:
            outcome.failed.append((d.action.command, result_apply.error or "rc!=0"))
            result.add_failure(
                FailureRecord(
                    category="integration",
                    message=f"autopilot no pudo aplicar: {d.action.command}",
                )
            )

    if not dry_run:
        cache.set(_LEDGER_KEY, ledger)
        cache.save()

    _write_report(ctx, outcome, dry_run, note)
    ctx.logger.info(
        "autopilot done",
        candidates=len(actions),
        applied=len(outcome.applied),
        skipped=len(outcome.skipped),
        dry_run=dry_run,
    )
    result.metrics["candidates"] = float(len(actions))
    result.metrics["applied"] = float(len(outcome.applied))
    result.metrics["skipped"] = float(len(outcome.skipped))
    result.actions = len(outcome.applied)
    return result
