"""Tests for the interactive launcher menu (pure command builders + temp config)."""

from __future__ import annotations

import json
from pathlib import Path

import menu


def test_dupefinder_command_targets_legacy_script() -> None:
    argv = menu.dupefinder_command()
    assert argv[-1].endswith("plex_dupefinder.py")


def test_dupefinder_diagnose_command_has_flag() -> None:
    assert "--diagnose-paths" in menu.dupefinder_diagnose_command()


def test_organizer_command_dry_run_adds_flag() -> None:
    argv = menu.organizer_command(dry=True)
    assert argv[0] == "docker"
    assert "--dry-run" in argv
    assert menu.DOCKER_IMAGE in argv


def test_organizer_command_real_has_no_dry_flag() -> None:
    assert "--dry-run" not in menu.organizer_command(dry=False)


def test_organizer_command_mounts_repo_media_cache() -> None:
    argv = menu.organizer_command(dry=False)
    joined = " ".join(argv)
    assert "/mnt/user:/mnt/user" in joined
    assert "/mnt/cache:/mnt/cache" in joined


def test_set_legacy_dry_run_toggles_flag() -> None:
    assert menu.set_legacy_dry_run({}, True)["DRY_RUN"] is True
    assert menu.set_legacy_dry_run({}, False)["DRY_RUN"] is False


def test_set_izumi_organizer_sets_mode_and_apply() -> None:
    data = menu.set_izumi_organizer({}, live=True, apply_moves=True)
    assert data["safety"]["mode"] == "live"
    assert data["integrations"]["gemini"]["apply"] is True
    data2 = menu.set_izumi_organizer({}, live=False, apply_moves=False)
    assert data2["safety"]["mode"] == "dry_run"
    assert data2["integrations"]["gemini"]["apply"] is False


def test_temp_config_applies_then_restores(tmp_path: Path) -> None:
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"DRY_RUN": False}), encoding="utf-8")
    seen = {}
    with menu.temp_config(str(cfg), lambda d: menu.set_legacy_dry_run(d, True)):
        seen["during"] = json.loads(cfg.read_text(encoding="utf-8"))["DRY_RUN"]
    after = json.loads(cfg.read_text(encoding="utf-8"))["DRY_RUN"]
    assert seen["during"] is True  # override applied inside the block
    assert after is False  # original restored on exit


def test_temp_config_restores_on_error(tmp_path: Path) -> None:
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"DRY_RUN": False}), encoding="utf-8")
    try:
        with menu.temp_config(str(cfg), lambda d: menu.set_legacy_dry_run(d, True)):
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert json.loads(cfg.read_text(encoding="utf-8"))["DRY_RUN"] is False
