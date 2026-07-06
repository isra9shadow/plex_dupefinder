"""Tests for modules.ops.permsdoctor (appdata owner/mode drift → chmod/chown proposals)."""

from __future__ import annotations

import json
from pathlib import Path

from modules.ops import permsdoctor
from modules.ops.permsdoctor import PathPerm, PathRule
from tests.fakes import make_context


def _read_plan(tmp_path: Path) -> dict[str, object]:
    data = json.loads(
        (tmp_path / "reports" / "permsdoctor" / "plan.json").read_text(encoding="utf-8")
    )
    assert isinstance(data, dict)
    return data


# --- evaluate_path (pure) ------------------------------------------------------


def test_owner_and_mode_ok_no_commands() -> None:
    rule = PathRule("/mnt/appdata/x", owner="99:100", mode="0775")
    f = permsdoctor.evaluate_path(rule, PathPerm(99, 100, 0o775))
    assert f.status == "ok"
    assert f.commands == ()


def test_owner_drift_proposes_chown() -> None:
    rule = PathRule("/mnt/appdata/x", owner="99:100", mode=None)
    f = permsdoctor.evaluate_path(rule, PathPerm(0, 0, 0o755))
    assert f.status == "drift"
    assert f.commands == ("chown 99:100 /mnt/appdata/x",)
    assert f.current_owner == "0:0"


def test_mode_drift_proposes_chmod() -> None:
    rule = PathRule("/mnt/appdata/x", owner=None, mode="0775")
    f = permsdoctor.evaluate_path(rule, PathPerm(99, 100, 0o777))
    assert f.status == "drift"
    assert f.commands == ("chmod 0775 /mnt/appdata/x",)
    assert f.current_mode == "0777"


def test_both_drift_proposes_both() -> None:
    rule = PathRule("/mnt/appdata/x", owner="99:100", mode="0775")
    f = permsdoctor.evaluate_path(rule, PathPerm(0, 0, 0o700))
    assert set(f.commands) == {"chown 99:100 /mnt/appdata/x", "chmod 0775 /mnt/appdata/x"}


def test_missing_path() -> None:
    f = permsdoctor.evaluate_path(PathRule("/nope", "99:100", "0775"), None)
    assert f.status == "missing"
    assert f.commands == ()


def test_invalid_mode_in_config_is_error() -> None:
    f = permsdoctor.evaluate_path(PathRule("/x", None, "zzz"), PathPerm(99, 100, 0o755))
    assert f.status == "error"
    assert "invalid mode" in f.detail


def test_unsafe_path_drops_the_command() -> None:
    # A path with a space cannot be a safe allow-listed argv → drift, but no command.
    rule = PathRule("/mnt/app data/x", owner="99:100", mode=None)
    f = permsdoctor.evaluate_path(rule, PathPerm(0, 0, 0o755))
    assert f.status == "drift"
    assert f.commands == ()  # classify rejected it


# --- _to_actions ---------------------------------------------------------------


def test_to_actions_dedupes_and_classifies() -> None:
    findings = [
        permsdoctor.PathFinding(
            "/a", "drift", "0:0", "0755", ("chown 99:100 /a", "chown 99:100 /a")
        ),
        permsdoctor.PathFinding("/b", "drift", "0:0", "0777", ("chmod 0775 /b",)),
    ]
    actions = permsdoctor._to_actions(findings)
    assert [a.command for a in actions] == ["chown 99:100 /a", "chmod 0775 /b"]
    assert actions[0].category == "chown"


# --- run -----------------------------------------------------------------------


def test_run_proposes_for_drifted_paths(tmp_path: Path) -> None:
    perms = {
        "/mnt/appdata/mysql": PathPerm(0, 0, 0o700),  # drift owner+mode
        "/mnt/appdata/ok": PathPerm(99, 100, 0o775),  # fine
    }
    ctx = make_context(
        tmp_path,
        integrations={
            "permsdoctor": {
                "paths": [
                    {"path": "/mnt/appdata/mysql", "owner": "99:100", "mode": "0775"},
                    {"path": "/mnt/appdata/ok", "owner": "99:100", "mode": "0775"},
                    "garbage",  # skipped
                ]
            }
        },
    )
    result = permsdoctor.run(ctx, statfn=lambda p: perms.get(p))

    assert result.actions == 0  # proposal-only
    assert result.metrics["checked"] == 2.0
    assert result.metrics["drifted"] == 1.0
    plan = _read_plan(tmp_path)
    assert plan["proposed_count"] == 2  # chown + chmod for the mysql path
    commands = {a["command"] for a in plan["actions"]}  # type: ignore[union-attr]
    assert commands == {"chown 99:100 /mnt/appdata/mysql", "chmod 0775 /mnt/appdata/mysql"}


def test_run_missing_path_recorded(tmp_path: Path) -> None:
    ctx = make_context(
        tmp_path,
        integrations={"permsdoctor": {"paths": [{"path": "/gone", "owner": "99:100"}]}},
    )
    result = permsdoctor.run(ctx, statfn=lambda p: None)
    assert any("/gone" in f.message for f in result.failures)
    assert _read_plan(tmp_path)["proposed_count"] == 0


def test_run_no_paths_note(tmp_path: Path) -> None:
    ctx = make_context(tmp_path)
    result = permsdoctor.run(ctx)
    assert result.ok
    assert "No paths configured" in _read_plan(tmp_path)["note"]  # type: ignore[operator]
