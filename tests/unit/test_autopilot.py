"""Tests for modules.ops.autopilot (policy self-healing with cooldown/anti-flap)."""

from __future__ import annotations

import json
from pathlib import Path

from aictx.apply import ApplyAction
from modules.ops import autopilot
from modules.ops.autopilot import _Policy
from tests.fakes import make_context


def _action(command: str, category: str, fp: str = "fp1") -> ApplyAction:
    return ApplyAction(
        command=command, category=category, finding_title="t", fingerprint=fp, severity="warning"
    )


def _policy(**kw: object) -> _Policy:
    base = dict(
        categories=frozenset({"docker-lifecycle"}),
        sources=("autoheal",),
        cooldown_s=3600.0,
        max_flaps=3,
        flap_window_s=86400.0,
    )
    base.update(kw)
    return _Policy(**base)  # type: ignore[arg-type]


# --- plan (pure) ---------------------------------------------------------------


def test_plan_applies_in_policy_and_fresh() -> None:
    d = autopilot.plan(
        [_action("docker restart x", "docker-lifecycle")], policy=_policy(), ledger={}, now=1000.0
    )
    assert [x.verdict for x in d] == ["apply"]


def test_plan_skips_not_in_policy() -> None:
    d = autopilot.plan([_action("chmod 0755 /x", "chmod")], policy=_policy(), ledger={}, now=1000.0)
    assert d[0].verdict == "not_in_policy"


def test_plan_cooldown() -> None:
    ledger = {"fp1": [1000.0 - 600]}  # applied 10 min ago; cooldown 60 min
    d = autopilot.plan(
        [_action("docker restart x", "docker-lifecycle")],
        policy=_policy(),
        ledger=ledger,
        now=1000.0,
    )
    assert d[0].verdict == "cooldown"


def test_plan_flapping() -> None:
    # 3 applies inside the window, cooldown already passed -> flapping (>= max_flaps).
    ledger = {"fp1": [1000.0 - 7200, 1000.0 - 10800, 1000.0 - 14400]}
    d = autopilot.plan(
        [_action("docker restart x", "docker-lifecycle")],
        policy=_policy(),
        ledger=ledger,
        now=1000.0,
    )
    assert d[0].verdict == "flapping"


def test_plan_old_stamps_fall_out_of_window() -> None:
    # Stamps older than the flap window don't count -> apply again.
    ledger = {"fp1": [1000.0 - 90000, 1000.0 - 100000]}  # > 24h ago
    d = autopilot.plan(
        [_action("docker restart x", "docker-lifecycle")],
        policy=_policy(),
        ledger=ledger,
        now=1000.0,
    )
    assert d[0].verdict == "apply"


# --- run -----------------------------------------------------------------------


def _write_autoheal_plan(tmp_path: Path, commands: list[str]) -> None:
    d = tmp_path / "reports" / "autoheal"
    d.mkdir(parents=True, exist_ok=True)
    actions = [
        {
            "command": c,
            "category": "docker-lifecycle",
            "finding_title": f"container down: {c}",
            "severity": "warning",
        }
        for c in commands
    ]
    (d / "plan.json").write_text(json.dumps({"actions": actions}), encoding="utf-8")


def _read_plan(tmp_path: Path) -> dict:
    return json.loads(
        (tmp_path / "reports" / "autopilot" / "plan.json").read_text(encoding="utf-8")
    )


def test_run_dry_run_plans_only(tmp_path: Path) -> None:
    _write_autoheal_plan(tmp_path, ["docker restart sonarr"])
    calls: list[str] = []
    ctx = make_context(
        tmp_path,
        integrations={
            "autopilot": {"policy_categories": ["docker-lifecycle"], "sources": ["autoheal"]}
        },
    )
    result = autopilot.run(ctx, runner=lambda c: calls.append(c) or (0, "ok"), now=1000.0)
    assert calls == []  # dry-run applies nothing
    assert result.metrics["applied"] == 0.0
    plan = _read_plan(tmp_path)
    assert plan["dry_run"] is True
    assert any(s["verdict"] == "would_apply" for s in plan["skipped"])


def test_run_live_applies_and_records_ledger(tmp_path: Path) -> None:
    from core.types import SafetyMode

    _write_autoheal_plan(tmp_path, ["docker restart sonarr"])
    calls: list[str] = []
    ctx = make_context(
        tmp_path,
        mode=SafetyMode.LIVE,
        integrations={
            "autopilot": {"policy_categories": ["docker-lifecycle"], "sources": ["autoheal"]}
        },
    )
    result = autopilot.run(ctx, runner=lambda c: calls.append(c) or (0, "ok"), now=1000.0)
    assert calls == ["docker restart sonarr"]
    assert result.metrics["applied"] == 1.0
    # Ledger persisted for cooldown next time.
    ledger = json.loads(
        (tmp_path / "reports" / "cache" / "autopilot.json").read_text(encoding="utf-8")
    )
    assert ledger["applied_ts"]  # non-empty


def test_run_no_policy_note(tmp_path: Path) -> None:
    _write_autoheal_plan(tmp_path, ["docker restart sonarr"])
    from core.types import SafetyMode

    ctx = make_context(
        tmp_path, mode=SafetyMode.LIVE, integrations={"autopilot": {"sources": ["autoheal"]}}
    )
    result = autopilot.run(ctx, runner=lambda c: (0, "ok"), now=1000.0)
    assert result.metrics["applied"] == 0.0
    assert "policy_categories vacío" in _read_plan(tmp_path)["note"]
