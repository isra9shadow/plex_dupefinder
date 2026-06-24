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
        "fallback.mkv",
        "/M",
        "/S",
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

    def fake_identify(
        self: object, names: list[str], errors: object = None
    ) -> list[dict[str, object]]:
        return [
            {
                "filename": "Dune.2021.mkv",
                "type": "movie",
                "title": "Dune",
                "year": 2021,
                "confidence": 97,
            }
        ]

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
    monkeypatch.setattr(organizer.GeminiClient, "identify", lambda self, names, errors=None: [])
    monkeypatch.setattr(organizer.secrets, "require", lambda ref: "KEY")

    ctx = make_context(
        tmp_path,
        paths={"organizer_source": str(source)},
        integrations={"gemini": {"api_key_ref": "GEMINI_API_KEY"}},
    )
    result = organizer.run(ctx)
    assert result.quarantined == 1  # planned
    assert (source / "junk.nfo").exists()  # but not actually moved in DRY_RUN


# --- apply step ----------------------------------------------------------------


def _movie_identify(title: str = "Dune", year: int = 2021, confidence: int = 97):
    def fake(self: object, names: list[str], errors: object = None) -> list[dict[str, object]]:
        return [
            {
                "filename": "Dune.2021.mkv",
                "type": "movie",
                "title": title,
                "year": year,
                "confidence": confidence,
            }
        ]

    return fake


def _make_apply_ctx(
    tmp_path: Path,
    source: Path,
    *,
    mode: SafetyMode,
    apply: bool,
    movies_root: str | None = None,
):
    return make_context(
        tmp_path,
        mode=mode,
        paths={
            "organizer_source": str(source),
            "movies_root": movies_root or str(tmp_path / "Movies"),
            "series_root": str(tmp_path / "Series"),
        },
        integrations={"gemini": {"api_key_ref": "GEMINI_API_KEY", "apply": apply}},
    )


def test_apply_disabled_by_default_does_not_move(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "manuales"
    source.mkdir()
    (source / "Dune.2021.mkv").write_bytes(b"data")
    monkeypatch.setattr(organizer.GeminiClient, "identify", _movie_identify())
    monkeypatch.setattr(organizer.secrets, "require", lambda ref: "KEY")

    # apply key omitted entirely → defaults False, even in LIVE mode.
    ctx = make_context(
        tmp_path,
        mode=SafetyMode.LIVE,
        paths={
            "organizer_source": str(source),
            "movies_root": str(tmp_path / "Movies"),
            "series_root": str(tmp_path / "Series"),
        },
        integrations={"gemini": {"api_key_ref": "GEMINI_API_KEY"}},
    )
    result = organizer.run(ctx)

    assert result.ok
    assert result.metrics["relocated"] == 0.0
    assert (source / "Dune.2021.mkv").exists()  # never moved


def test_apply_live_moves_confident_movie(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    source = tmp_path / "manuales"
    source.mkdir()
    src_file = source / "Dune.2021.mkv"
    src_file.write_bytes(b"data")
    monkeypatch.setattr(organizer.GeminiClient, "identify", _movie_identify())
    monkeypatch.setattr(organizer.secrets, "require", lambda ref: "KEY")

    ctx = _make_apply_ctx(tmp_path, source, mode=SafetyMode.LIVE, apply=True)
    result = organizer.run(ctx)

    assert result.ok
    assert result.metrics["relocated"] == 1.0
    dest = tmp_path / "Movies" / "Dune (2021)" / "Dune (2021).mkv"
    assert dest.exists()  # moved into canonical path
    assert not src_file.exists()  # gone from source
    plan = json.loads(
        (tmp_path / "reports" / "organizer" / "plan.json").read_text(encoding="utf-8")
    )
    assert plan["applied"][0]["dest"] == str(dest)
    assert plan["applied"][0]["dry_run"] is False


def test_apply_low_confidence_not_moved(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    source = tmp_path / "manuales"
    source.mkdir()
    src_file = source / "Dune.2021.mkv"
    src_file.write_bytes(b"data")
    monkeypatch.setattr(organizer.GeminiClient, "identify", _movie_identify(confidence=50))
    monkeypatch.setattr(organizer.secrets, "require", lambda ref: "KEY")

    ctx = _make_apply_ctx(tmp_path, source, mode=SafetyMode.LIVE, apply=True)
    result = organizer.run(ctx)

    assert result.metrics["relocated"] == 0.0
    assert src_file.exists()  # below threshold → left in place


def test_identify_sends_relative_path_and_maps_back(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "manuales"
    (source / "Breaking Bad" / "S01").mkdir(parents=True)
    src = source / "Breaking Bad" / "S01" / "ep01.mkv"
    src.write_bytes(b"data")
    seen: dict[str, list[str]] = {}

    def fake(self: object, names: list[str], errors: object = None) -> list[dict[str, object]]:
        seen["names"] = list(names)
        return [
            {
                "filename": names[0],  # echo the relative path back
                "type": "series",
                "title": "Breaking Bad",
                "year": 2008,
                "season": 1,
                "episode": 1,
                "confidence": 95,
            }
        ]

    monkeypatch.setattr(organizer.GeminiClient, "identify", fake)
    monkeypatch.setattr(organizer.secrets, "require", lambda ref: "KEY")

    ctx = _make_apply_ctx(tmp_path, source, mode=SafetyMode.LIVE, apply=True)
    result = organizer.run(ctx)

    # The model receives the FULL relative path (folder hint), not just the file.
    assert seen["names"] == ["Breaking Bad/S01/ep01.mkv"]
    assert result.metrics["relocated"] == 1.0
    dest = tmp_path / "Series" / "Breaking Bad (2008)" / "Season 01" / "Breaking Bad - S01E01.mkv"
    assert dest.exists()
    assert not src.exists()


def test_largest_files_identified_first(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    source = tmp_path / "manuales"
    source.mkdir()
    (source / "small.mkv").write_bytes(b"x" * 10)
    (source / "big.mkv").write_bytes(b"x" * 10_000)
    (source / "mid.mkv").write_bytes(b"x" * 1_000)
    seen: dict[str, list[str]] = {}

    def fake(self: object, names: list[str], errors: object = None) -> list[dict[str, object]]:
        seen["names"] = list(names)
        return []

    monkeypatch.setattr(organizer.GeminiClient, "identify", fake)
    monkeypatch.setattr(organizer.secrets, "require", lambda ref: "KEY")

    ctx = make_context(
        tmp_path,
        paths={"organizer_source": str(source)},
        integrations={"gemini": {"api_key_ref": "GEMINI_API_KEY"}},
    )
    organizer.run(ctx)
    assert seen["names"] == ["big.mkv", "mid.mkv", "small.mkv"]  # largest first


def test_apply_unknown_target_none_not_moved(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "manuales"
    source.mkdir()
    src_file = source / "Mystery.mkv"
    src_file.write_bytes(b"data")

    def fake(self: object, names: list[str], errors: object = None) -> list[dict[str, object]]:
        # high confidence but unknown type → target is None → not applicable.
        return [
            {"filename": "Mystery.mkv", "type": "unknown", "title": "Mystery", "confidence": 99}
        ]

    monkeypatch.setattr(organizer.GeminiClient, "identify", fake)
    monkeypatch.setattr(organizer.secrets, "require", lambda ref: "KEY")

    ctx = _make_apply_ctx(tmp_path, source, mode=SafetyMode.LIVE, apply=True)
    result = organizer.run(ctx)

    assert result.metrics["relocated"] == 0.0
    assert src_file.exists()  # no target → left in place


def test_apply_dry_run_plans_but_does_not_move(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "manuales"
    source.mkdir()
    src_file = source / "Dune.2021.mkv"
    src_file.write_bytes(b"data")
    monkeypatch.setattr(organizer.GeminiClient, "identify", _movie_identify())
    monkeypatch.setattr(organizer.secrets, "require", lambda ref: "KEY")

    ctx = _make_apply_ctx(tmp_path, source, mode=SafetyMode.DRY_RUN, apply=True)
    result = organizer.run(ctx)

    assert result.metrics["relocated"] == 1.0  # planned
    dest = tmp_path / "Movies" / "Dune (2021)" / "Dune (2021).mkv"
    assert src_file.exists()  # not actually moved
    assert not dest.exists()
    plan = json.loads(
        (tmp_path / "reports" / "organizer" / "plan.json").read_text(encoding="utf-8")
    )
    assert plan["applied"][0]["dry_run"] is True


def test_apply_target_outside_roots_is_skipped_gracefully(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "manuales"
    source.mkdir()
    src_file = source / "Dune.2021.mkv"
    src_file.write_bytes(b"data")

    def fake(self: object, names: list[str], errors: object = None) -> list[dict[str, object]]:
        # Hallucinated title with traversal → target escapes the movies root.
        return [
            {
                "filename": "Dune.2021.mkv",
                "type": "movie",
                "title": "../../../../etc/Dune",
                "year": 2021,
                "confidence": 99,
            }
        ]

    monkeypatch.setattr(organizer.GeminiClient, "identify", fake)
    monkeypatch.setattr(organizer.secrets, "require", lambda ref: "KEY")

    ctx = _make_apply_ctx(tmp_path, source, mode=SafetyMode.LIVE, apply=True)
    result = organizer.run(ctx)

    assert result.ok  # the bad item does not fail the run (per-item skip)
    assert result.metrics["relocated"] == 0.0  # escapes roots → skipped
    assert src_file.exists()  # media left in place
    escaped = tmp_path.parent / "etc" / "Dune (2021)" / "Dune (2021).mkv"
    assert not escaped.exists()  # nothing written outside the roots


def test_apply_collision_is_skipped_gracefully(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "manuales"
    source.mkdir()
    src_file = source / "Dune.2021.mkv"
    src_file.write_bytes(b"data")
    # Pre-create the destination so relocate raises SafetyError (no clobber).
    dest = tmp_path / "Movies" / "Dune (2021)" / "Dune (2021).mkv"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"existing")

    monkeypatch.setattr(organizer.GeminiClient, "identify", _movie_identify())
    monkeypatch.setattr(organizer.secrets, "require", lambda ref: "KEY")

    ctx = _make_apply_ctx(tmp_path, source, mode=SafetyMode.LIVE, apply=True)
    result = organizer.run(ctx)

    assert result.ok  # one bad item does not fail the run
    assert result.metrics["relocated"] == 0.0  # collision → skipped
    assert src_file.exists()  # media left in place
    assert dest.read_bytes() == b"existing"  # existing file untouched
