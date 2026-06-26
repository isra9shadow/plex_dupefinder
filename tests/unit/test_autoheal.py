"""Tests for modules.ops.autoheal (proposal-only restart suggester).

All offline: we write a fake uptime ``plan.json`` under ``tmp_path`` and assert the
proposed actions are exactly the down containers' ``docker restart`` commands and
that every proposed command passes :func:`aictx.apply.classify` (allow-listed).
"""

from __future__ import annotations

import json
from pathlib import Path

from aictx.apply import classify
from modules.ops import autoheal
from tests.fakes import make_context


def _write_uptime_plan(tmp_path: Path, plan: dict[str, object]) -> None:
    out_dir = tmp_path / "reports" / "uptime"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "plan.json").write_text(json.dumps(plan, indent=2, sort_keys=True), encoding="utf-8")


def _read_plan(tmp_path: Path) -> dict[str, object]:
    return json.loads((tmp_path / "reports" / "autoheal" / "plan.json").read_text(encoding="utf-8"))


def _read_summary(tmp_path: Path) -> str:
    return (tmp_path / "reports" / "autoheal" / "summary.md").read_text(encoding="utf-8")


# --- pure: down_containers -----------------------------------------------------


def test_down_containers_from_expected_not_running() -> None:
    plan = {
        "expected_not_running": ["Sonarr", "Radarr"],
        "containers_running": ["Plex"],
        "targets": [],
    }
    assert autoheal.down_containers(plan, ignore=set()) == ["Sonarr", "Radarr"]


def test_down_containers_maps_down_targets_that_are_known_containers() -> None:
    # 'Plex' is a down TCP target AND a known (running) container -> proposed.
    # 'router' is a down TCP target but NOT a known container -> ignored.
    plan = {
        "expected_not_running": ["Sonarr"],
        "containers_running": ["Plex"],
        "targets": [
            {"name": "Plex", "host": "h", "port": 1, "status": "down"},
            {"name": "router", "host": "h", "port": 2, "status": "down"},
            {"name": "Sonarr", "host": "h", "port": 3, "status": "up"},
        ],
    }
    assert autoheal.down_containers(plan, ignore=set()) == ["Sonarr", "Plex"]


def test_down_containers_dedupes_case_insensitively() -> None:
    plan = {
        "expected_not_running": ["Sonarr"],
        "containers_running": [],
        "targets": [
            {"name": "sonarr", "host": "h", "port": 1, "status": "down"},
        ],
    }
    # sonarr target collapses into the expected-not-running Sonarr (case-insensitive).
    assert autoheal.down_containers(plan, ignore=set()) == ["Sonarr"]


def test_down_containers_honors_ignore() -> None:
    plan = {
        "expected_not_running": ["Sonarr", "watchtower"],
        "containers_running": [],
        "targets": [],
    }
    assert autoheal.down_containers(plan, ignore={"watchtower"}) == ["Sonarr"]


# --- pure: propose_actions -----------------------------------------------------


def test_propose_actions_are_allowlisted_restarts() -> None:
    actions, rejected = autoheal.propose_actions(["Sonarr", "Radarr"])
    assert rejected == []
    assert [a.command for a in actions] == [
        "docker restart Sonarr",
        "docker restart Radarr",
    ]
    for action in actions:
        verdict = classify(action.command)
        assert verdict.allowed
        assert verdict.category == "docker-lifecycle"
        assert action.category == "docker-lifecycle"


def test_propose_actions_rejects_unsafe_names() -> None:
    # A name with shell metacharacters can never be allow-listed.
    actions, rejected = autoheal.propose_actions(["Sonarr", "evil; rm -rf /"])
    assert [a.command for a in actions] == ["docker restart Sonarr"]
    assert rejected == ["evil; rm -rf /"]


# --- run integration -----------------------------------------------------------


def test_run_proposes_exactly_down_containers(tmp_path: Path) -> None:
    _write_uptime_plan(
        tmp_path,
        {
            "down_count": 2,
            "expected_not_running": ["Sonarr", "Radarr"],
            "containers_running": ["Plex"],
            "targets": [
                {"name": "Plex", "host": "h", "port": 1, "status": "up"},
            ],
        },
    )
    ctx = make_context(tmp_path)
    result = autoheal.run(ctx)

    assert result.ok
    assert result.actions == 0  # proposal-only
    assert result.metrics["proposed_count"] == 2.0

    plan = _read_plan(tmp_path)
    assert plan["proposed_count"] == 2
    commands = [a["command"] for a in plan["actions"]]  # type: ignore[index]
    assert commands == ["docker restart Sonarr", "docker restart Radarr"]
    # Every proposed command is allow-listed.
    for cmd in commands:
        assert classify(cmd).allowed

    summary = _read_summary(tmp_path)
    assert "docker restart Sonarr" in summary
    assert "docker restart Radarr" in summary
    assert "nothing was executed" in summary


def test_run_no_down_containers_is_clean(tmp_path: Path) -> None:
    _write_uptime_plan(
        tmp_path,
        {
            "down_count": 0,
            "expected_not_running": [],
            "containers_running": ["Plex", "Sonarr"],
            "targets": [{"name": "Plex", "host": "h", "port": 1, "status": "up"}],
        },
    )
    ctx = make_context(tmp_path)
    result = autoheal.run(ctx)

    assert result.ok
    assert result.metrics["proposed_count"] == 0.0
    assert _read_plan(tmp_path)["actions"] == []
    assert "nothing to propose" in _read_summary(tmp_path)


def test_run_missing_uptime_report_records_failure(tmp_path: Path) -> None:
    ctx = make_context(tmp_path)  # no uptime plan written
    result = autoheal.run(ctx)

    assert not result.ok
    assert any("uptime" in f.message for f in result.failures)
    assert result.metrics["proposed_count"] == 0.0
    # Report is still written (empty proposal set).
    assert _read_plan(tmp_path)["proposed_count"] == 0


def test_run_malformed_uptime_report_is_handled(tmp_path: Path) -> None:
    out_dir = tmp_path / "reports" / "uptime"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "plan.json").write_text("{not json", encoding="utf-8")

    ctx = make_context(tmp_path)
    result = autoheal.run(ctx)

    assert not result.ok
    assert result.metrics["proposed_count"] == 0.0


def test_run_honors_ignore_containers(tmp_path: Path) -> None:
    _write_uptime_plan(
        tmp_path,
        {
            "expected_not_running": ["Sonarr", "watchtower"],
            "containers_running": [],
            "targets": [],
        },
    )
    ctx = make_context(
        tmp_path,
        integrations={"autoheal": {"ignore_containers": ["watchtower"]}},
    )
    result = autoheal.run(ctx)

    assert result.ok
    assert result.metrics["proposed_count"] == 1.0
    commands = [a["command"] for a in _read_plan(tmp_path)["actions"]]  # type: ignore[index]
    assert commands == ["docker restart Sonarr"]
