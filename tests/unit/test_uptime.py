"""Tests for modules.ops.uptime (read-only service/container up-check)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from modules.ops import uptime
from tests.fakes import make_context


def _read_plan(tmp_path: Path) -> dict[str, object]:
    return json.loads((tmp_path / "reports" / "uptime" / "plan.json").read_text(encoding="utf-8"))


def _read_summary(tmp_path: Path) -> str:
    return (tmp_path / "reports" / "uptime" / "summary.md").read_text(encoding="utf-8")


# --- pure target parsing -------------------------------------------------------


def test_parse_targets_keeps_valid_and_drops_malformed() -> None:
    raw = [
        {"name": "Plex", "host": "10.0.0.2", "port": 32400},
        {"name": "  Sonarr  ", "host": " 10.0.0.3 ", "port": 8989},  # trimmed
        {"name": "", "host": "x", "port": 80},  # empty name -> dropped
        {"name": "NoHost", "host": "", "port": 80},  # empty host -> dropped
        {"name": "BadPort", "host": "x", "port": "80"},  # non-int port -> dropped
        {"name": "OutOfRange", "host": "x", "port": 70000},  # out of range -> dropped
        {"name": "BoolPort", "host": "x", "port": True},  # bool is not a port -> dropped
        "not-a-dict",  # ignored
    ]
    targets = uptime.parse_targets(raw)
    assert [t.name for t in targets] == ["Plex", "Sonarr"]
    assert targets[1].host == "10.0.0.3"  # whitespace stripped
    assert targets[1].port == 8989


def test_parse_targets_handles_non_list() -> None:
    assert uptime.parse_targets(None) == []
    assert uptime.parse_targets({"name": "x"}) == []


# --- pure up/down evaluation ---------------------------------------------------


def test_probe_targets_uses_injected_prober() -> None:
    targets = [
        uptime.Target("Up", "h1", 1),
        uptime.Target("Down", "h2", 2),
    ]

    def prober(host: str, port: int, timeout: float) -> bool:
        return host == "h1"

    results = uptime.probe_targets(targets, prober, 1.0)
    by_name = {r.name: r.up for r in results}
    assert by_name == {"Up": True, "Down": False}


# --- the 'ignore batch containers' rule ----------------------------------------


def test_missing_containers_flags_expected_but_not_running() -> None:
    missing = uptime.missing_containers(
        expect_running=["Plex", "Sonarr", "Radarr"],
        running=["Plex"],
        ignore=set(),
    )
    assert missing == ["Sonarr", "Radarr"]


def test_missing_containers_ignores_batch_oneshots() -> None:
    # recyclarr/configarr/watchtower exit normally and must NOT be flagged down,
    # even though they appear in expect_running and are not currently running.
    missing = uptime.missing_containers(
        expect_running=["Plex", "recyclarr", "Configarr", "watchtower"],
        running=["Plex"],
        ignore={"recyclarr", "configarr", "watchtower"},
    )
    assert missing == []


def test_missing_containers_is_case_insensitive_and_dedupes() -> None:
    missing = uptime.missing_containers(
        expect_running=["Plex", "plex", "Sonarr"],
        running=["PLEX"],  # different case but same container
        ignore=set(),
    )
    assert missing == ["Sonarr"]  # Plex matched case-insensitively, dupe collapsed


# --- run integration -----------------------------------------------------------


def _all_down(host: str, port: int, timeout: float) -> bool:
    return False


def _all_up(host: str, port: int, timeout: float) -> bool:
    return True


def test_run_writes_reports_and_counts_down(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(uptime.docker, "container_names", lambda **kw: ["Plex"])

    ctx = make_context(
        tmp_path,
        integrations={
            "uptime": {
                "targets": [
                    {"name": "Plex", "host": "10.0.0.2", "port": 32400},
                    {"name": "Sonarr", "host": "10.0.0.3", "port": 8989},
                ],
                "expect_running": ["Plex", "Radarr"],
            }
        },
    )

    def prober(host: str, port: int, timeout: float) -> bool:
        return host == "10.0.0.2"  # only Plex's TCP endpoint is up

    result = uptime.run(ctx, prober=prober)

    assert result.actions == 0  # read-only
    # 1 down target (Sonarr) + 1 missing container (Radarr) = 2.
    assert result.metrics["down_count"] == 2.0
    assert not result.ok  # failures recorded

    plan = _read_plan(tmp_path)
    assert plan["down_count"] == 2
    statuses = {t["name"]: t["status"] for t in plan["targets"]}
    assert statuses == {"Plex": "up", "Sonarr": "down"}
    assert plan["expected_not_running"] == ["Radarr"]

    summary = _read_summary(tmp_path)
    assert "DOWN: Sonarr" in summary
    assert "Radarr" in summary


def test_run_all_up_has_no_failures(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(uptime.docker, "container_names", lambda **kw: ["Plex", "Sonarr"])

    ctx = make_context(
        tmp_path,
        integrations={
            "uptime": {
                "targets": [{"name": "Plex", "host": "h", "port": 1}],
                "expect_running": ["Plex", "Sonarr"],
            }
        },
    )
    result = uptime.run(ctx, prober=_all_up)

    assert result.ok
    assert result.metrics["down_count"] == 0.0
    assert "all expected containers are running" in _read_summary(tmp_path)


def test_run_ignores_batch_containers(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # watchtower is expected but not running; because it's in ignore_containers it
    # must NOT count as down.
    monkeypatch.setattr(uptime.docker, "container_names", lambda **kw: ["Plex"])

    ctx = make_context(
        tmp_path,
        integrations={
            "uptime": {
                "expect_running": ["Plex", "watchtower"],
                "ignore_containers": ["watchtower"],
            }
        },
    )
    result = uptime.run(ctx, prober=_all_up)

    assert result.ok
    assert result.metrics["down_count"] == 0.0
    assert _read_plan(tmp_path)["expected_not_running"] == []


def test_run_reports_docker_unreachable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(uptime.docker, "container_names", lambda **kw: [])
    monkeypatch.setattr(uptime.docker, "probe", lambda **kw: "")  # docker not reachable

    ctx = make_context(tmp_path, integrations={"uptime": {"expect_running": ["Plex"]}})
    result = uptime.run(ctx, prober=_all_up)

    assert not result.ok
    assert any("Docker NOT reachable" in f.message for f in result.failures)
    # Container check skipped, so nothing is falsely flagged as down.
    assert result.metrics["down_count"] == 0.0
    assert "Docker NOT reachable" in _read_summary(tmp_path)


def test_run_no_config_is_clean(tmp_path: Path) -> None:
    ctx = make_context(tmp_path)  # no integrations.uptime at all
    result = uptime.run(ctx, prober=_all_down)

    assert result.ok
    assert result.metrics["down_count"] == 0.0
    plan = _read_plan(tmp_path)
    assert plan["targets"] == []
    assert plan["expected_not_running"] == []


def test_run_skips_docker_when_no_expected_containers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # If expect_running is empty, docker must not even be queried.
    def boom(**_: object) -> object:
        raise AssertionError("docker must not be queried with no expected containers")

    monkeypatch.setattr(uptime.docker, "container_names", boom)

    ctx = make_context(
        tmp_path,
        integrations={"uptime": {"targets": [{"name": "Plex", "host": "h", "port": 1}]}},
    )
    result = uptime.run(ctx, prober=_all_up)

    assert result.ok
    assert result.metrics["down_count"] == 0.0
