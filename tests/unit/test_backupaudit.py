"""Tests for modules.ops.backupaudit (backup freshness + local-image flagging)."""

from __future__ import annotations

import json
import os
from pathlib import Path

from adapters.docker import ContainerInfo
from modules.ops import backupaudit
from modules.ops.backupaudit import BackupRule
from tests.fakes import make_context

_DAY = 86400.0


def _read_plan(tmp_path: Path) -> dict[str, object]:
    data = json.loads(
        (tmp_path / "reports" / "backupaudit" / "plan.json").read_text(encoding="utf-8")
    )
    assert isinstance(data, dict)
    return data


def _touch(path: Path, mtime: float) -> None:
    path.write_bytes(b"backup")
    os.utime(path, (mtime, mtime))


# --- evaluate_backup (pure) ----------------------------------------------------


def test_backup_ok_when_fresh(tmp_path: Path) -> None:
    _touch(tmp_path / "b1.zip", 1000 * _DAY)
    rule = BackupRule("sonarr", str(tmp_path / "*.zip"), max_age_days=2)
    f = backupaudit.evaluate_backup(rule, now=1000 * _DAY + 3600)
    assert f.status == "ok"


def test_backup_stale_when_old(tmp_path: Path) -> None:
    _touch(tmp_path / "b1.zip", 1000 * _DAY)
    rule = BackupRule("sonarr", str(tmp_path / "*.zip"), max_age_days=2)
    f = backupaudit.evaluate_backup(rule, now=1000 * _DAY + 5 * _DAY)
    assert f.status == "stale"
    assert f.age_days > 2
    assert "old" in f.detail


def test_backup_missing_when_no_file(tmp_path: Path) -> None:
    rule = BackupRule("radarr", str(tmp_path / "none*.zip"), max_age_days=2)
    f = backupaudit.evaluate_backup(rule, now=1000 * _DAY)
    assert f.status == "missing"
    assert f.age_days == -1.0


def test_evaluate_picks_newest(tmp_path: Path) -> None:
    _touch(tmp_path / "old.zip", 1000 * _DAY)
    _touch(tmp_path / "new.zip", 1002 * _DAY)
    rule = BackupRule("s", str(tmp_path / "*.zip"), max_age_days=5)
    f = backupaudit.evaluate_backup(rule, now=1002 * _DAY + 3600)
    assert f.newest.endswith("new.zip")
    assert f.status == "ok"


# --- local images --------------------------------------------------------------


def _ct(name: str, image: str) -> ContainerInfo:
    return ContainerInfo(name=name, image=image, state="running", ports=[], networks=[], mounts=[])


def test_find_local_images_matches_markers() -> None:
    containers = [_ct("sonae", "dyalf/sonae:latest"), _ct("plex", "plexinc/pms-docker:latest")]
    found = backupaudit.find_local_images(containers, ["dyalf/"])
    assert [i.container for i in found] == ["sonae"]


def test_find_local_images_empty_markers() -> None:
    assert backupaudit.find_local_images([_ct("x", "dyalf/x")], []) == []


# --- run -----------------------------------------------------------------------


def test_run_flags_stale_and_missing(tmp_path: Path) -> None:
    now_ts = 1005 * _DAY
    good = tmp_path / "sonarr"
    good.mkdir()
    _touch(good / "b.zip", now_ts - 3600)  # fresh (1h old)
    old = tmp_path / "radarr"
    old.mkdir()
    _touch(old / "b.zip", 1000 * _DAY)  # 5 days old → stale

    ctx = make_context(
        tmp_path,
        integrations={
            "backupaudit": {
                "backups": [
                    {"name": "sonarr", "glob": str(good / "*.zip"), "max_age_days": 2},
                    {"name": "radarr", "glob": str(old / "*.zip"), "max_age_days": 2},
                    {"name": "plex", "glob": str(tmp_path / "plex" / "*.db")},  # missing
                    "garbage",  # skipped
                ]
            }
        },
    )
    result = backupaudit.run(ctx, now=lambda: now_ts, list_fn=lambda: [])

    assert result.metrics["backups_checked"] == 3.0
    assert result.metrics["backups_bad"] == 2.0  # radarr stale + plex missing
    assert not result.ok
    statuses = {b["name"]: b["status"] for b in _read_plan(tmp_path)["backups"]}  # type: ignore[union-attr]
    assert statuses == {"sonarr": "ok", "radarr": "stale", "plex": "missing"}


def test_run_local_image_flag(tmp_path: Path) -> None:
    ctx = make_context(
        tmp_path,
        integrations={"backupaudit": {"local_image_markers": ["dyalf/"]}},
    )
    containers = [_ct("sonae", "dyalf/sonae:latest"), _ct("plex", "plexinc/pms:latest")]
    result = backupaudit.run(ctx, now=lambda: 0.0, list_fn=lambda: containers)

    assert result.metrics["local_images"] == 1.0
    assert _read_plan(tmp_path)["images"] == [{"container": "sonae", "image": "dyalf/sonae:latest"}]


def test_run_no_config_note(tmp_path: Path) -> None:
    ctx = make_context(tmp_path)
    result = backupaudit.run(ctx, list_fn=lambda: [])
    assert result.ok
    assert "No backups configured" in _read_plan(tmp_path)["note"]  # type: ignore[operator]
