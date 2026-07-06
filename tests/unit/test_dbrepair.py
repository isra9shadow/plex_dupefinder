"""Tests for modules.ops.dbrepair (operator-confirmed SAFE SQLite auto-repair).

Everything runs offline: docker is a recording fake ``Runner``, container listing
is stubbed, and repair strategies operate on real tiny SQLite files in ``tmp_path``.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from adapters.docker import ContainerInfo
from core.types import SafetyMode
from modules.ops import dbrepair
from tests.fakes import make_context

# --- fixtures ------------------------------------------------------------------


def _make_healthy_db(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("CREATE TABLE metadata (id INTEGER PRIMARY KEY, title TEXT)")
        conn.executemany(
            "INSERT INTO metadata (title) VALUES (?)",
            [("Movie A",), ("Show B",), ("Episode C",)],
        )
        conn.commit()
    finally:
        conn.close()


def _make_corrupt_db(path: Path) -> None:
    """A file that opens as SQLite by header but fails integrity_check."""
    path.write_bytes(b"SQLite format 3\x00 totally not a real database, just noise" * 8)


class _Runner:
    """Recording docker runner: records every command, returns (0, 'ok')."""

    def __init__(self, *, fail_prefixes: tuple[str, ...] = ()) -> None:
        self.commands: list[str] = []
        self._fail = fail_prefixes

    def __call__(self, command: str) -> tuple[int, str]:
        self.commands.append(command)
        if any(command.startswith(p) for p in self._fail):
            return 1, "boom"
        return 0, "ok"


def _no_containers() -> list[ContainerInfo]:
    return []


def _one_db_config(db_path: Path, backup_glob: str = "", **extra: object) -> dict[str, object]:
    cfg: dict[str, object] = {
        "databases": [
            {
                "app": "sonarr",
                "container": "sonarr",
                "db_path": str(db_path),
                "native_backup_glob": backup_glob,
            }
        ],
    }
    cfg.update(extra)
    return {"dbrepair": cfg}


def _read_plan(tmp_path: Path) -> dict[str, object]:
    data = json.loads((tmp_path / "reports" / "dbrepair" / "plan.json").read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


# --- pure logic ----------------------------------------------------------------


def test_is_healthy() -> None:
    assert dbrepair.is_healthy(["ok"]) is True
    assert dbrepair.is_healthy(["*** in database main ***", "page 3 broken"]) is False
    assert dbrepair.is_healthy([]) is False


def test_integrity_check_real_healthy(tmp_path: Path) -> None:
    db = tmp_path / "ok.db"
    _make_healthy_db(db)
    assert dbrepair.integrity_check(str(db)) == ["ok"]


def test_safe_integrity_maps_corrupt(tmp_path: Path) -> None:
    db = tmp_path / "bad.db"
    _make_corrupt_db(db)
    ok, detail = dbrepair._safe_integrity(str(db), dbrepair.integrity_check)
    assert ok is False
    assert detail != "ok"


def test_safe_integrity_missing_file(tmp_path: Path) -> None:
    ok, detail = dbrepair._safe_integrity(str(tmp_path / "nope.db"), dbrepair.integrity_check)
    assert ok is False
    assert detail == "file not found"


def test_recover_db_rebuilds_healthy(tmp_path: Path) -> None:
    src = tmp_path / "src.db"
    dst = tmp_path / "dst.db"
    _make_healthy_db(src)
    assert dbrepair.recover_db(str(src), str(dst)) is True
    assert dbrepair.integrity_check(str(dst)) == ["ok"]


def test_recover_db_on_garbage_returns_false(tmp_path: Path) -> None:
    src = tmp_path / "garbage.db"
    dst = tmp_path / "out.db"
    _make_corrupt_db(src)
    assert dbrepair.recover_db(str(src), str(dst)) is False


def test_newest_picks_latest(tmp_path: Path) -> None:
    old = tmp_path / "a.db"
    new = tmp_path / "b.db"
    old.write_bytes(b"1")
    new.write_bytes(b"2")
    import os

    os.utime(old, (1, 1))
    os.utime(new, (10_000, 10_000))
    assert dbrepair._newest(str(tmp_path / "*.db")) == new


def test_newest_no_pattern_or_match(tmp_path: Path) -> None:
    assert dbrepair._newest("") is None
    assert dbrepair._newest(str(tmp_path / "none*.db")) is None


# --- config --------------------------------------------------------------------


def test_targets_skips_incomplete(tmp_path: Path) -> None:
    ctx = make_context(
        tmp_path,
        integrations={
            "dbrepair": {
                "databases": [
                    {"app": "plex", "container": "plex", "db_path": "/d/p.db"},
                    {"app": "no-container", "db_path": "/d/x.db"},  # skipped
                    {"container": "c", "db_path": "/d/y.db"},  # skipped: no app
                    "garbage",  # skipped
                ]
            }
        },
    )
    settings = dbrepair._settings(ctx)
    assert [t.app for t in settings.databases] == ["plex"]
    assert settings.databases[0].native_backup_glob == ""


def test_settings_strategy_order_and_timeout(tmp_path: Path) -> None:
    ctx = make_context(
        tmp_path,
        integrations={
            "dbrepair": {
                "databases": [],
                "repair_strategy_order": ["recover", "bogus"],
                "verify_timeout_s": 12,
            }
        },
    )
    settings = dbrepair._settings(ctx)
    assert settings.strategy_order == ("recover",)  # bogus filtered out
    assert settings.verify_timeout_s == 12.0


def test_select_honours_target_fingerprint(tmp_path: Path) -> None:
    ctx = make_context(tmp_path, integrations=_one_db_config(tmp_path / "s.db"))
    settings = dbrepair._settings(ctx)
    fp = settings.databases[0].fingerprint
    ctx2 = make_context(
        tmp_path,
        integrations=_one_db_config(tmp_path / "s.db", target_fingerprint=fp),
    )
    settings2 = dbrepair._settings(ctx2)
    assert [t.app for t in dbrepair._select(settings2)] == ["sonarr"]
    # A non-matching fingerprint selects nothing.
    ctx3 = make_context(
        tmp_path, integrations=_one_db_config(tmp_path / "s.db", target_fingerprint="deadbeef")
    )
    assert dbrepair._select(dbrepair._settings(ctx3)) == []


# --- run: dry-run (default) ----------------------------------------------------


def test_dry_run_plans_only_no_side_effects(tmp_path: Path) -> None:
    db = tmp_path / "sonarr.db"
    _make_corrupt_db(db)
    original = db.read_bytes()
    runner = _Runner()

    ctx = make_context(tmp_path, integrations=_one_db_config(db))  # DRY_RUN default
    result = dbrepair.run(ctx, runner=runner, list_fn=_no_containers)

    assert result.actions == 0
    assert result.metrics["corrupt_count"] == 1.0
    assert result.metrics["repaired_count"] == 0.0
    assert runner.commands == []  # never touched docker
    assert db.read_bytes() == original  # never touched the DB
    assert not any((tmp_path / "q").glob("*")) if (tmp_path / "q").exists() else True

    plan = _read_plan(tmp_path)
    assert plan["dry_run"] is True
    results = plan["results"]
    assert isinstance(results, list)
    assert results[0]["status"] == "dry_run"


def test_healthy_db_is_already_ok(tmp_path: Path) -> None:
    db = tmp_path / "sonarr.db"
    _make_healthy_db(db)
    runner = _Runner()

    ctx = make_context(tmp_path, mode=SafetyMode.LIVE, integrations=_one_db_config(db))
    result = dbrepair.run(ctx, runner=runner, list_fn=_no_containers)

    assert result.ok
    assert result.metrics["corrupt_count"] == 0.0
    assert runner.commands == []
    assert _read_plan(tmp_path)["results"][0]["status"] == "already_ok"  # type: ignore[index]


def test_no_databases_configured_note(tmp_path: Path) -> None:
    ctx = make_context(tmp_path)
    result = dbrepair.run(ctx)
    assert result.ok
    assert "No databases configured" in _read_plan(tmp_path)["note"]  # type: ignore[operator]


# --- run: LIVE repair paths ----------------------------------------------------


def test_live_native_backup_repairs_and_verifies(tmp_path: Path) -> None:
    db = tmp_path / "sonarr.db"
    _make_corrupt_db(db)
    backups = tmp_path / "backups"
    backups.mkdir()
    _make_healthy_db(backups / "sonarr.db")  # a healthy native backup
    runner = _Runner()

    ctx = make_context(
        tmp_path,
        mode=SafetyMode.LIVE,
        integrations=_one_db_config(db, backup_glob=str(backups / "*.db")),
    )
    result = dbrepair.run(ctx, runner=runner, list_fn=_no_containers)

    assert result.ok
    assert result.actions == 1
    assert result.metrics["repaired_count"] == 1.0
    # DB is now healthy in place.
    assert dbrepair.integrity_check(str(db)) == ["ok"]
    # Container was stopped BEFORE start, both through the allow-list.
    assert runner.commands == ["docker stop sonarr", "docker start sonarr"]

    outcome = _read_plan(tmp_path)["results"][0]  # type: ignore[index]
    assert outcome["status"] == "ok"
    assert outcome["strategy"] == "native_backup"
    step_names = [s["name"] for s in outcome["steps"]]
    assert step_names.index("snapshot") < step_names.index("stop_container")


def test_live_recover_when_no_backup(tmp_path: Path) -> None:
    db = tmp_path / "sonarr.db"
    _make_corrupt_db(db)
    runner = _Runner()

    def fake_recover(src: str, dst: str) -> bool:
        _make_healthy_db(Path(dst))  # recovery produces a healthy DB
        return True

    ctx = make_context(
        tmp_path,
        mode=SafetyMode.LIVE,
        integrations=_one_db_config(db, repair_strategy_order=["recover"]),  # type: ignore[arg-type]
    )
    result = dbrepair.run(ctx, runner=runner, recover_fn=fake_recover, list_fn=_no_containers)

    assert result.ok
    assert dbrepair.integrity_check(str(db)) == ["ok"]
    assert _read_plan(tmp_path)["results"][0]["strategy"] == "recover"  # type: ignore[index]


def test_live_both_strategies_fail_rolls_back(tmp_path: Path) -> None:
    db = tmp_path / "sonarr.db"
    _make_corrupt_db(db)
    original = db.read_bytes()
    runner = _Runner()

    ctx = make_context(
        tmp_path,
        mode=SafetyMode.LIVE,
        integrations=_one_db_config(db),  # no backup glob, real recover fails on garbage
    )
    result = dbrepair.run(ctx, runner=runner, list_fn=_no_containers)

    assert not result.ok
    # Original bytes restored exactly (no data lost/invented).
    assert db.read_bytes() == original
    # Container was stopped then restarted so the app is never left down.
    assert runner.commands == ["docker stop sonarr", "docker start sonarr"]
    assert _read_plan(tmp_path)["results"][0]["status"] == "rolled_back"  # type: ignore[index]


def test_live_stop_failure_rolls_back_before_repair(tmp_path: Path) -> None:
    db = tmp_path / "sonarr.db"
    _make_corrupt_db(db)
    original = db.read_bytes()
    runner = _Runner(fail_prefixes=("docker stop",))

    ctx = make_context(tmp_path, mode=SafetyMode.LIVE, integrations=_one_db_config(db))
    result = dbrepair.run(ctx, runner=runner, list_fn=_no_containers)

    assert not result.ok
    assert db.read_bytes() == original  # untouched-equivalent
    assert runner.commands == ["docker stop sonarr"]  # never started repair
    assert _read_plan(tmp_path)["results"][0]["status"] == "rolled_back"  # type: ignore[index]


def test_live_post_verify_failure_rolls_back(tmp_path: Path) -> None:
    db = tmp_path / "sonarr.db"
    _make_corrupt_db(db)
    original = db.read_bytes()
    backups = tmp_path / "backups"
    backups.mkdir()
    _make_healthy_db(backups / "sonarr.db")
    runner = _Runner()

    # Checker: preflight corrupt, backup healthy, post-verify corrupt again.
    verdicts = iter([["corrupt"], ["ok"], ["corrupt"]])

    def flaky_checker(path: str) -> list[str]:
        return next(verdicts)

    ctx = make_context(
        tmp_path,
        mode=SafetyMode.LIVE,
        integrations=_one_db_config(db, backup_glob=str(backups / "*.db")),
    )
    result = dbrepair.run(ctx, runner=runner, checker=flaky_checker, list_fn=_no_containers)

    assert not result.ok
    assert db.read_bytes() == original  # rolled back to the original corrupt file
    assert runner.commands.count("docker start sonarr") >= 1  # container restarted
    outcome = _read_plan(tmp_path)["results"][0]  # type: ignore[index]
    assert outcome["status"] == "rolled_back"
    assert "post-repair integrity not ok" in outcome["detail"]
