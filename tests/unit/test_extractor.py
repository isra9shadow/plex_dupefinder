"""Tests for modules.media.extractor (volume grouping + quarantine-on-success)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from core.types import SafetyMode
from modules.media import extractor
from tests.fakes import make_context


def _names(sets: list[tuple[Path, list[Path]]]) -> dict[str, list[str]]:
    """Map first-volume name -> sorted member names for easy assertions."""
    return {first.name: sorted(p.name for p in vols) for first, vols in sets}


# --- pure grouping -------------------------------------------------------------


def test_single_archives_each_their_own_set() -> None:
    files = [Path("/d/movie.zip"), Path("/d/show.7z"), Path("/d/film.rar")]
    grouped = _names(extractor.archive_sets(files))
    assert grouped == {
        "movie.zip": ["movie.zip"],
        "show.7z": ["show.7z"],
        "film.rar": ["film.rar"],
    }


def test_multipart_rar_partn_groups_under_first() -> None:
    files = [
        Path("/d/movie.part3.rar"),
        Path("/d/movie.part1.rar"),
        Path("/d/movie.part2.rar"),
    ]
    grouped = _names(extractor.archive_sets(files))
    assert list(grouped) == ["movie.part1.rar"]  # first volume only
    assert grouped["movie.part1.rar"] == [
        "movie.part1.rar",
        "movie.part2.rar",
        "movie.part3.rar",
    ]


def test_old_style_rar_with_rnn_volumes() -> None:
    files = [Path("/d/show.r01"), Path("/d/show.rar"), Path("/d/show.r00")]
    grouped = _names(extractor.archive_sets(files))
    assert list(grouped) == ["show.rar"]
    assert grouped["show.rar"] == ["show.r00", "show.r01", "show.rar"]


def test_split_7z_groups_under_001() -> None:
    files = [Path("/d/data.7z.002"), Path("/d/data.7z.001"), Path("/d/data.7z.003")]
    grouped = _names(extractor.archive_sets(files))
    assert list(grouped) == ["data.7z.001"]
    assert grouped["data.7z.001"] == ["data.7z.001", "data.7z.002", "data.7z.003"]


def test_incomplete_part_downloads_are_skipped() -> None:
    # qBittorrent in-progress files end in .part (no archive extension) -> ignored.
    files = [Path("/d/Greenland.rar.part"), Path("/d/video.part")]
    assert extractor.archive_sets(files) == []


def test_orphan_non_first_volume_is_not_returned() -> None:
    # part2 without part1 -> incomplete set, nothing to hand to unar.
    files = [Path("/d/movie.part2.rar"), Path("/d/movie.part3.rar")]
    assert extractor.archive_sets(files) == []


# --- run integration -----------------------------------------------------------


def _read_plan(tmp_path: Path) -> dict[str, object]:
    return json.loads(
        (tmp_path / "reports" / "extractor" / "plan.json").read_text(encoding="utf-8")
    )


def test_run_extracts_then_quarantines_volumes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    src = tmp_path / "Manuales"
    src.mkdir()
    (src / "movie.part1.rar").write_bytes(b"a")
    (src / "movie.part2.rar").write_bytes(b"b")
    (src / "incomplete.rar.part").write_bytes(b"c")  # must be left alone

    calls: list[Path] = []
    monkeypatch.setattr(
        extractor.archive, "extract", lambda first, dest: calls.append(first) or True
    )

    ctx = make_context(tmp_path, mode=SafetyMode.LIVE, paths={"extract_source": str(src)})
    result = extractor.run(ctx)

    assert result.ok
    assert calls == [src / "movie.part1.rar"]  # only the first volume handed to unar
    # Both rar volumes moved to quarantine; the .part download untouched.
    assert not (src / "movie.part1.rar").exists()
    assert not (src / "movie.part2.rar").exists()
    assert (src / "incomplete.rar.part").exists()
    plan = _read_plan(tmp_path)
    assert plan["extracted"] == 1
    assert plan["quarantined"] == 2


def test_run_dry_run_moves_nothing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    src = tmp_path / "Manuales"
    src.mkdir()
    (src / "film.zip").write_bytes(b"a")

    def boom(first: Path, dest: Path) -> bool:
        raise AssertionError("dry-run must not call unar")

    monkeypatch.setattr(extractor.archive, "extract", boom)

    ctx = make_context(tmp_path, mode=SafetyMode.DRY_RUN, paths={"extract_source": str(src)})
    result = extractor.run(ctx)

    assert result.ok
    assert (src / "film.zip").exists()  # nothing moved in a dry run
    plan = _read_plan(tmp_path)
    assert plan["extracted"] == 1  # planned (optimistic) but not executed


def test_run_failed_extract_keeps_archive(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    src = tmp_path / "Manuales"
    src.mkdir()
    (src / "corrupt.rar").write_bytes(b"a")
    monkeypatch.setattr(extractor.archive, "extract", lambda first, dest: False)

    ctx = make_context(tmp_path, mode=SafetyMode.LIVE, paths={"extract_source": str(src)})
    result = extractor.run(ctx)

    assert not result.ok  # failure recorded
    assert (src / "corrupt.rar").exists()  # never quarantined when extraction fails
    plan = _read_plan(tmp_path)
    assert plan["quarantined"] == 0


def test_run_missing_source_is_config_failure(tmp_path: Path) -> None:
    ctx = make_context(tmp_path, paths={"extract_source": str(tmp_path / "nope")})
    result = extractor.run(ctx)
    assert not result.ok
    assert result.failures[0].category == "config"
