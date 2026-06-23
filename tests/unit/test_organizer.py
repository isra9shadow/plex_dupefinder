"""Tests for modules.media.organizer (real cleanup, report-only identify)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from core.types import SafetyMode
from modules.media import organizer
from tests.fakes import make_context

# --- pure scanning / classification -------------------------------------------

def test_is_junk_by_suffix_and_zero_byte(tmp_path: Path) -> None:
    nfo = tmp_path / "a.nfo"
    nfo.write_text("x", encoding="utf-8")
    empty = tmp_path / "b.mkv"
    empty.write_bytes(b"")
    real = tmp_path / "c.mkv"
    real.write_bytes(b"data")
    assert organizer.is_junk(nfo) is True
    assert organizer.is_junk(empty) is True  # zero-byte
    assert organizer.is_junk(real) is False


def test_scan_splits_junk_and_media(tmp_path: Path) -> None:
    (tmp_path / "movie.mkv").write_bytes(b"data")
    (tmp_path / "info.nfo").write_text("x", encoding="utf-8")
    (tmp_path / "link.url").write_text("x", encoding="utf-8")
    junk, media = organizer.scan(tmp_path)
    assert {p.name for p in junk} == {"info.nfo", "link.url"}
    assert {p.name for p in media} == {"movie.mkv"}


def test_empty_dirs_detected_deepest_first(tmp_path: Path) -> None:
    (tmp_path / "outer" / "inner").mkdir(parents=True)
    (tmp_path / "full").mkdir()
    (tmp_path / "full" / "f.mkv").write_bytes(b"x")
    empties = organizer.empty_dirs(tmp_path)
    assert (tmp_path / "outer" / "inner") in empties
    assert (tmp_path / "full") not in empties


# --- suggestion shaping --------------------------------------------------------

def test_suggested_target_movie() -> None:
    t = organizer.suggested_target("movie", "Dune", 2021, None, None, ".mkv", "/M", "/S")
    assert t == str(Path("/M") / "Dune (2021)" / "Dune (2021).mkv")


def test_suggested_target_series() -> None:
    t = organizer.suggested_target("series", "Show", 2020, 2, 5, ".mkv", "/M", "/S")
    assert t == str(Path("/S") / "Show (2020)" / "Season 02" / "Show - S02E05.mkv")


def test_suggested_target_unknown_is_none() -> None:
    assert organizer.suggested_target("unknown", "X", None, None, None, ".mkv", "/M", "/S") is None
    # series missing episode → no confident target
    assert organizer.suggested_target("series", "X", 2020, 1, None, ".mkv", "/M", "/S") is None


def test_normalize_suggestion_clamps_and_defaults() -> None:
    s = organizer.normalize_suggestion(
        {"type": "movie", "title": "  Dune ", "year": 2021, "confidence": 150},
        "fallback.mkv", "/M", "/S",
    )
    assert s.filename == "fallback.mkv"  # model omitted filename → fallback
    assert s.title == "Dune"
    assert s.confidence == 100.0  # clamped
    assert s.target is not None


# --- run integration -----------------------------------------------------------

def test_run_fails_without_source(tmp_path: Path) -> None:
    result = organizer.run(make_context(tmp_path))
    assert not result.ok
    assert result.failures[0].category == "config"


def test_run_cleans_junk_and_reports(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    source = tmp_path / "manuales"
    source.mkdir()
    (source / "Dune.2021.mkv").write_bytes(b"data")
    (source / "Dune.nfo").write_text("x", encoding="utf-8")
    (source / "empty").mkdir()

    def fake_identify(self: object, names: list[str]) -> list[dict[str, object]]:
        return [{"filename": "Dune.2021.mkv", "type": "movie", "title": "Dune",
                 "year": 2021, "confidence": 97}]

    monkeypatch.setattr(organizer.GeminiClient, "identify", fake_identify)
    monkeypatch.setattr(organizer.secrets, "require", lambda ref: "KEY")

    ctx = make_context(
        tmp_path,
        mode=SafetyMode.LIVE,
        paths={"organizer_source": str(source), "movies_root": "/M", "series_root": "/S"},
        integrations={"gemini": {"api_key_ref": "GEMINI_API_KEY"}},
    )
    result = organizer.run(ctx)

    assert result.ok
    assert result.quarantined == 2  # the .nfo and the empty dir
    assert not (source / "Dune.nfo").exists()  # moved to quarantine (LIVE mode)
    assert (source / "Dune.2021.mkv").exists()  # media never moved (report-only)
    plan_file = tmp_path / "reports" / "organizer" / "plan.json"
    plan = json.loads(plan_file.read_text(encoding="utf-8"))
    assert plan["confident"][0]["title"] == "Dune"
    assert result.actions == 3  # 2 cleaned + 1 confident suggestion


def test_run_dry_run_does_not_move(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    source = tmp_path / "manuales"
    source.mkdir()
    (source / "junk.nfo").write_text("x", encoding="utf-8")
    monkeypatch.setattr(organizer.GeminiClient, "identify", lambda self, names: [])
    monkeypatch.setattr(organizer.secrets, "require", lambda ref: "KEY")

    ctx = make_context(
        tmp_path,
        paths={"organizer_source": str(source)},
        integrations={"gemini": {"api_key_ref": "GEMINI_API_KEY"}},
    )
    result = organizer.run(ctx)
    assert result.quarantined == 1  # planned
    assert (source / "junk.nfo").exists()  # but not actually moved in DRY_RUN
