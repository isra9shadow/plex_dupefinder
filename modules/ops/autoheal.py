"""Autoheal — propose restarts for down services (semi-supervised, READ-ONLY).

This module reads the latest **uptime** report and turns every down container into
a *proposed* ``docker restart <name>`` action. It is strictly a PROPOSAL module
(INVARIANT I1 + DRY_RUN-safe): it NEVER executes anything itself — it only writes
its own plan under ``reporting.dir / "autoheal"`` (plan.json + summary.md). The
actual execution happens later through the operator-confirmed apply path
(menu ``y/N`` or the Telegram inline button), reusing :mod:`aictx.apply`.

What it does:

  1. READ — load ``reporting.dir / "uptime" / plan.json`` (best-effort; a missing
     or malformed report yields zero proposals and a note, never an exception).
  2. COLLECT down containers — the union of:
       * ``expected_not_running`` (container names uptime flagged as not running),
       * down TCP ``targets`` whose ``name`` maps to a known container (a target
         is only mapped when its name also appears in ``expected_not_running``
         OR in ``containers_running`` — i.e. it is a real container, not just an
         arbitrary host:port endpoint). Running containers are never restarted.
  3. PROPOSE — for each down container build a ``docker restart <name>`` command,
     classify it through :func:`aictx.apply.classify` (POSITIVE allow-list — only
     allow-listed, metachar-free, guard-passing commands survive) and wrap the
     survivors as :class:`aictx.apply.ApplyAction` objects. Names that do not
     produce an allow-listed command (e.g. illegal characters) are skipped and
     recorded as a failure, never aborting the run.

Config (config.json):
  integrations.autoheal :
    ignore_containers : container names that must NOT be proposed for restart
                        (batch/one-shot dockers that exit normally, or services
                        you prefer to heal by hand). Case-insensitive.

Metrics: ``proposed_count`` — number of restart actions proposed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from aictx.apply import ApplyAction, classify, finding_fingerprint
from core.registry import register
from core.types import FailureRecord, ModuleResult, RunContext


@dataclass(frozen=True)
class _Settings:
    ignore_containers: set[str]


def _str_list(raw: object) -> list[str]:
    """Coerce a config value into a clean list of non-empty strings."""
    if not isinstance(raw, list):
        return []
    return [item.strip() for item in raw if isinstance(item, str) and item.strip()]


def _settings(ctx: RunContext) -> _Settings:
    """Read ``integrations.autoheal`` into a typed settings object."""
    cfg = ctx.config.integrations.get("autoheal", {})
    return _Settings(
        ignore_containers={c.lower() for c in _str_list(cfg.get("ignore_containers"))},
    )


def load_uptime_plan(path: Path) -> dict[str, object] | None:
    """Read the uptime ``plan.json`` (None if missing/malformed — never raises)."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def down_containers(plan: dict[str, object], ignore: set[str]) -> list[str]:
    """Container names to propose a restart for, derived from an uptime plan.

    The union of ``expected_not_running`` and down TCP ``targets`` whose name
    maps to a real container (it also appears in ``expected_not_running`` or in
    ``containers_running``). Comparison is case-insensitive; ``ignore`` names are
    dropped; order follows first appearance and duplicates collapse.
    """
    expected_not_running = _str_list(plan.get("expected_not_running"))
    running = _str_list(plan.get("containers_running"))
    # A name is a known container iff uptime saw it (running or expected-not-running).
    known = {n.lower() for n in expected_not_running} | {n.lower() for n in running}

    out: list[str] = []
    seen: set[str] = set()

    def _consider(name: str) -> None:
        key = name.lower()
        if key in ignore or key in seen:
            return
        seen.add(key)
        out.append(name)

    # 1) Containers uptime explicitly flagged as expected-but-not-running.
    for name in expected_not_running:
        _consider(name)

    # 2) Down TCP targets whose name maps to a known container.
    raw_targets = plan.get("targets")
    if isinstance(raw_targets, list):
        for entry in raw_targets:
            if not isinstance(entry, dict):
                continue
            if entry.get("status") != "down":
                continue
            raw_name = entry.get("name")
            if not isinstance(raw_name, str) or not raw_name.strip():
                continue
            clean = raw_name.strip()
            if clean.lower() in known:
                _consider(clean)

    return out


def propose_actions(
    names: list[str],
) -> tuple[list[ApplyAction], list[str]]:
    """Build allow-listed ``docker restart`` actions for ``names``.

    Returns ``(actions, rejected)``: every name whose ``docker restart <name>``
    command passes :func:`aictx.apply.classify` becomes an :class:`ApplyAction`;
    names that do not produce an allow-listed command are returned in ``rejected``
    so the caller can record them (defense in depth — odd names never slip through).
    """
    actions: list[ApplyAction] = []
    rejected: list[str] = []
    for name in names:
        command = f"docker restart {name}"
        if not classify(command).allowed:
            rejected.append(name)
            continue
        title = f"container down: {name}"
        actions.append(
            ApplyAction(
                command=command,
                category="docker-lifecycle",
                finding_title=title,
                fingerprint=finding_fingerprint(title),
                severity="warning",
            )
        )
    return actions, rejected


def _write_report(
    ctx: RunContext,
    actions: list[ApplyAction],
    note: str,
) -> None:
    out_dir = ctx.config.reporting.dir / "autoheal"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "plan.json").write_text(
        json.dumps(
            {
                "proposed_count": len(actions),
                "note": note,
                "actions": [
                    {
                        "command": a.command,
                        "category": a.category,
                        "finding_title": a.finding_title,
                        "fingerprint": a.fingerprint,
                        "severity": a.severity,
                    }
                    for a in actions
                ],
            },
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    lines = [
        "# Autoheal summary",
        "",
        f"Proposed restarts: {len(actions)}",
        "",
        "> Proposals only — nothing was executed. Confirm each action through the",
        "> operator-confirmed apply path (menu y/N or Telegram inline button).",
        "",
        f"## Proposed actions ({len(actions)})",
        *(
            [f"- `{a.command}`  ({a.finding_title})" for a in actions]
            or ["(no down containers — nothing to propose)"]
        ),
    ]
    if note:
        lines += ["", f"> {note}"]
    (out_dir / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


@register("autoheal")
def run(ctx: RunContext) -> ModuleResult:
    result = ModuleResult(module="autoheal", run_id=ctx.run_id, mode=ctx.mode)
    settings = _settings(ctx)

    uptime_plan_path = ctx.config.reporting.dir / "uptime" / "plan.json"
    plan = load_uptime_plan(uptime_plan_path)

    note = ""
    actions: list[ApplyAction] = []
    if plan is None:
        note = (
            "No readable uptime report at " f"{uptime_plan_path} — run the 'uptime' module first."
        )
        result.add_failure(FailureRecord(category="integration", message=note))
    else:
        names = down_containers(plan, settings.ignore_containers)
        actions, rejected = propose_actions(names)
        for name in rejected:
            result.add_failure(
                FailureRecord(
                    category="validation",
                    message=f"container name not safe to auto-restart, skipped: {name}",
                )
            )
        if not names:
            note = "No down containers in the latest uptime report — nothing to propose."

    _write_report(ctx, actions, note)
    ctx.logger.info(
        "autoheal done",
        proposed=len(actions),
        uptime_plan=str(uptime_plan_path),
    )
    result.metrics["proposed_count"] = float(len(actions))
    result.actions = 0  # proposal-only; execution happens via the apply path
    return result
