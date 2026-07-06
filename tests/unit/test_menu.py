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


def test_parse_version_splits_sha_and_date() -> None:
    assert menu.parse_version("abc1234|2026-06-24") == ("abc1234", "2026-06-24")
    assert menu.parse_version("") == ("?", "?")


def test_cycle_providers_rotates_presets() -> None:
    assert menu._cycle_providers(["ollama", "gemini"]) == ["ollama"]
    assert menu._cycle_providers(["ollama"]) == ["gemini"]
    assert menu._cycle_providers(["gemini"]) == []
    assert menu._cycle_providers([]) == ["ollama", "gemini"]
    assert menu._cycle_providers(["weird"]) == ["ollama", "gemini"]  # unknown -> first


def test_cfg_get_set_roundtrip(tmp_path: Path) -> None:
    cfg = tmp_path / "config.json"
    cfg.write_text("{}", encoding="utf-8")
    menu._cfg_set(str(cfg), "live", "safety", "mode")
    menu._cfg_set(str(cfg), True, "integrations", "gemini", "apply")
    assert menu._cfg_get(str(cfg), "safety", "mode") == "live"
    assert menu._cfg_get(str(cfg), "integrations", "gemini", "apply") is True
    assert menu._cfg_get(str(cfg), "nope", default="d") == "d"


def test_cfg_get_missing_file_returns_default(tmp_path: Path) -> None:
    assert menu._cfg_get(str(tmp_path / "absent.json"), "a", default=7) == 7


def test_prepare_dirs_command_is_a_root_container_that_chowns() -> None:
    argv = menu.prepare_dirs_command()
    assert argv[0] == "docker"
    assert "--user" not in argv  # must run as root to chown
    joined = " ".join(argv)
    assert "mkdir -p" in joined and "chown -R 99:100" in joined and "chown 99:100" in joined
    assert "/app/plans" in joined  # legacy discovery-plan dir on the repo mount


def test_health_command_runs_health_in_container() -> None:
    argv = menu.health_command()
    assert argv[0] == "docker"
    assert argv[-3:] == ["python", "run.py", "health"]
    assert menu.DOCKER_IMAGE in argv


def test_full_maintenance_runs_extract_dupes_then_organizer(monkeypatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(menu, "_run", lambda argv: calls.append(argv) or 0)
    monkeypatch.setattr(menu, "ensure_image", lambda: menu.LOCAL_IMAGE)  # no docker build
    monkeypatch.setattr("builtins.input", lambda *a: "s")  # confirm yes

    from contextlib import contextmanager

    @contextmanager
    def _noop(*a, **k):
        yield

    monkeypatch.setattr(menu, "temp_config", _noop)  # don't touch real config files
    menu.action_full_maintenance()

    assert len(calls) == 3
    assert "extractor" in calls[0]  # extract archives first
    assert calls[1][-1].endswith("plex_dupefinder.py")  # then duplicates
    assert "organizer" in calls[2]  # then organize


def test_destructive_action_aborts_when_declined(monkeypatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(menu, "_run", lambda argv: calls.append(argv))
    monkeypatch.setattr("builtins.input", lambda *a: "n")  # decline
    menu.action_dupefinder_real()
    assert calls == []  # nothing ran


def test_menu_items_are_dispatchable_and_headers_excluded() -> None:
    items = menu._menu_items()
    # Every dispatchable item has a callable action; section headers are excluded.
    assert items and all(callable(action) for _label, action in items)
    # Headers exist in MENU but carry no action.
    assert any(action is None for _label, action in menu.MENU)


def test_render_menu_numbers_items_not_headers() -> None:
    out = menu.render_menu("v1")
    # Home shows section headers (unnumbered) and one number per dispatchable item.
    assert "Rápido" in out and "Avanzado" in out
    assert "10)" in out  # ten dispatchable items -> reaches 10
    assert "11)" not in out  # and no more


def _noop_temp_config(_path, _mutate):
    from contextlib import contextmanager

    @contextmanager
    def _cm():
        yield

    return _cm()


def test_dupes_guided_simulates_then_moves_on_confirm(monkeypatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(menu, "ensure_image", lambda: "img")
    monkeypatch.setattr(menu, "_run", lambda argv: calls.append(argv))
    monkeypatch.setattr(menu, "temp_config", _noop_temp_config)  # don't touch real config
    monkeypatch.setattr(menu, "confirm", lambda _prompt: True)  # accept the move
    menu.action_dupes_guided()
    # Two dupefinder runs: the simulation first, then the real move after confirm.
    assert len(calls) == 2
    assert all(c[-1].endswith("plex_dupefinder.py") for c in calls)


def test_dupes_guided_only_simulates_when_declined(monkeypatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(menu, "ensure_image", lambda: "img")
    monkeypatch.setattr(menu, "_run", lambda argv: calls.append(argv))
    monkeypatch.setattr(menu, "temp_config", _noop_temp_config)
    monkeypatch.setattr(menu, "confirm", lambda _prompt: False)  # decline the move
    menu.action_dupes_guided()
    assert len(calls) == 1  # only the simulation ran; nothing moved


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
