"""Tests for aictx.apply — the safe, operator-confirmed AI action executor."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from aictx import apply

# --- fingerprint ----------------------------------------------------------------


def test_finding_fingerprint_is_stable_and_masks_numbers() -> None:
    a = apply.finding_fingerprint("MySQL error 13 on disk1")
    b = apply.finding_fingerprint("MySQL error 99 on disk5")
    assert a == b  # numbers masked -> same recurrence id
    assert len(a) == 16
    assert apply.finding_fingerprint("Other") != a


# --- classify (positive allow-list) ---------------------------------------------


def test_classify_allows_docker_lifecycle() -> None:
    for cmd in ("docker restart radarr", "docker start sonarr", "docker stop binhex-lidarr"):
        verdict = apply.classify(cmd)
        assert verdict.allowed and verdict.category == "docker-lifecycle"


def test_classify_allows_docker_logs_with_flags() -> None:
    assert apply.classify("docker logs radarr").allowed
    assert apply.classify("docker logs --tail 100 radarr").allowed
    assert apply.classify("docker logs --since 2026-06-20 traefik").allowed


def test_classify_allows_perms_and_mkdir() -> None:
    assert apply.classify("chmod 600 /letsencrypt/acme.json").category == "chmod"
    assert apply.classify("chown nobody:users /mnt/cache/x").category == "chown"
    assert apply.classify("mkdir -p /mnt/user/media/new").category == "mkdir"


def test_classify_rejects_guard_vetoed_commands() -> None:
    for cmd in ("systemctl restart containerd", "apt install foo", "docker-compose up -d"):
        verdict = apply.classify(cmd)
        assert not verdict.allowed
        assert "guard" in verdict.reason


def test_classify_rejects_shell_metacharacters() -> None:
    assert not apply.classify("docker restart radarr; rm -rf /").allowed
    assert not apply.classify("docker restart radarr && reboot").allowed
    assert not apply.classify("chmod 600 /a > /etc/passwd").allowed
    assert not apply.classify("docker restart $(echo radarr)").allowed


def test_classify_rejects_destructive_and_unknown() -> None:
    for cmd in ("rm -rf /mnt/user", "mv /a /b", "dd if=/dev/zero of=/dev/sda", "echo hi"):
        assert not apply.classify(cmd).allowed
    assert not apply.classify("chmod -R 777 /").allowed  # recursive not allow-listed
    assert not apply.classify("").allowed
    assert not apply.classify("   ").allowed


def test_classify_rejects_non_string() -> None:
    assert not apply.classify(None).allowed  # type: ignore[arg-type]


# --- extract / load / collect ---------------------------------------------------


def _diag(*commands: str) -> dict[str, Any]:
    return {
        "summary": "x",
        "findings": [
            {
                "title": "Radarr DNS falla",
                "severity": "error",
                "unraid_commands": list(commands),
            }
        ],
    }


def test_extract_actions_keeps_only_allowed_and_dedupes() -> None:
    diag = _diag(
        "docker restart radarr",
        "systemctl restart radarr",  # vetoed -> dropped
        "docker restart radarr",  # duplicate -> dropped
        "rm -rf /mnt",  # unknown -> dropped
    )
    actions = apply.extract_actions(diag)
    assert [a.command for a in actions] == ["docker restart radarr"]
    assert actions[0].finding_title == "Radarr DNS falla"
    assert actions[0].severity == "error"
    assert actions[0].fingerprint == apply.finding_fingerprint("Radarr DNS falla")


def test_extract_actions_handles_malformed() -> None:
    assert apply.extract_actions(None) == []
    assert apply.extract_actions({"findings": "nope"}) == []
    assert apply.extract_actions({"findings": [{"unraid_commands": [123, None]}]}) == []


def test_diagnosis_from_plan() -> None:
    assert apply.diagnosis_from_plan({"diagnosis": {"findings": []}}) == {"findings": []}
    assert apply.diagnosis_from_plan({"diagnosis": None}) is None
    assert apply.diagnosis_from_plan("nope") is None


def test_load_actions_from_file(tmp_path: Path) -> None:
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"diagnosis": _diag("docker restart radarr")}), encoding="utf-8")
    actions = apply.load_actions_from_file(plan)
    assert [a.command for a in actions] == ["docker restart radarr"]
    assert apply.load_actions_from_file(tmp_path / "missing.json") == []


def test_collect_actions_unions_across_files(tmp_path: Path) -> None:
    p1 = tmp_path / "logwatch.json"
    p2 = tmp_path / "analyst.json"
    p1.write_text(json.dumps({"diagnosis": _diag("docker restart radarr")}), encoding="utf-8")
    p2.write_text(
        json.dumps({"diagnosis": _diag("docker restart radarr", "docker restart sonarr")}),
        encoding="utf-8",
    )
    actions = apply.collect_actions([p1, p2])
    assert [a.command for a in actions] == ["docker restart radarr", "docker restart sonarr"]


# --- apply_action ---------------------------------------------------------------


def _action(command: str) -> apply.ApplyAction:
    return apply.ApplyAction(
        command=command,
        category="docker-lifecycle",
        finding_title="t",
        fingerprint="f",
        severity="error",
    )


def test_apply_action_runs_allowed_via_runner() -> None:
    seen: list[str] = []

    def runner(cmd: str) -> tuple[int, str]:
        seen.append(cmd)
        return 0, "done"

    outcome = apply.apply_action(_action("docker restart radarr"), runner=runner)
    assert outcome.ran and outcome.ok and outcome.returncode == 0
    assert outcome.output == "done"
    assert seen == ["docker restart radarr"]


def test_apply_action_refuses_non_allowed_without_running() -> None:
    def runner(cmd: str) -> tuple[int, str]:
        raise AssertionError("must not run a non-allow-listed command")

    outcome = apply.apply_action(_action("rm -rf /mnt"), runner=runner)
    assert not outcome.ran and not outcome.ok
    assert outcome.error  # carries the rejection reason


def test_apply_action_reports_nonzero_returncode() -> None:
    outcome = apply.apply_action(_action("docker restart radarr"), runner=lambda c: (1, "boom"))
    assert outcome.ran and not outcome.ok and outcome.returncode == 1


def test_default_runner_splits_and_combines_output(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    def fake_run(argv, *, timeout=30.0, env=None):
        captured["argv"] = list(argv)
        return SimpleNamespace(returncode=0, stdout="out", stderr="err")

    monkeypatch.setattr(apply.command_adapter, "run", fake_run)
    rc, output = apply.default_runner("docker restart radarr")
    assert rc == 0
    assert output == "outerr"
    assert captured["argv"] == ["docker", "restart", "radarr"]


# --- autoheal actions[] plan shape ---------------------------------------------


def test_actions_from_serialized_reclassifies_and_dedupes() -> None:
    items = [
        {
            "command": "docker restart radarr",
            "finding_title": "container down: radarr",
            "severity": "warning",
            "category": "ignored-recomputed",
        },
        {"command": "docker restart radarr", "finding_title": "dup"},  # duplicate command
        {"command": "rm -rf /mnt", "finding_title": "evil"},  # rejected by classify
        {"nope": 1},  # not a dict-with-command
    ]
    actions = apply.actions_from_serialized(items)
    assert [a.command for a in actions] == ["docker restart radarr"]
    action = actions[0]
    assert action.category == "docker-lifecycle"  # re-classified, not the stored value
    assert action.fingerprint == apply.finding_fingerprint("container down: radarr")


def test_actions_from_serialized_ignores_non_list() -> None:
    assert apply.actions_from_serialized(None) == []
    assert apply.actions_from_serialized({"command": "docker restart x"}) == []


def test_load_actions_from_file_reads_autoheal_actions_shape(tmp_path: Path) -> None:
    plan = tmp_path / "plan.json"
    plan.write_text(
        json.dumps(
            {
                "proposed_count": 1,
                "actions": [
                    {
                        "command": "docker restart sonarr",
                        "category": "docker-lifecycle",
                        "finding_title": "container down: sonarr",
                        "severity": "warning",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    actions = apply.load_actions_from_file(plan)
    assert [a.command for a in actions] == ["docker restart sonarr"]


def test_collect_actions_unions_diagnosis_and_autoheal(tmp_path: Path) -> None:
    diag = tmp_path / "analyst.json"
    diag.write_text(
        json.dumps(
            {
                "diagnosis": {
                    "findings": [
                        {
                            "title": "svc down",
                            "severity": "warning",
                            "unraid_commands": ["docker restart plex"],
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    auto = tmp_path / "autoheal.json"
    auto.write_text(
        json.dumps(
            {
                "actions": [
                    {
                        "command": "docker restart sonarr",
                        "finding_title": "container down: sonarr",
                        "severity": "warning",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    actions = apply.collect_actions([diag, auto])
    assert {a.command for a in actions} == {"docker restart plex", "docker restart sonarr"}
