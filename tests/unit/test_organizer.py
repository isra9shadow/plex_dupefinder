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
    assert t == str(Path("/S") / "Show (2020)" / "Season 02" / "Show (2020) - S02E05.mkv")


def test_suggested_target_movie_with_quality_and_id() -> None:
    t = organizer.suggested_target(
        "movie",
        "Dune",
        2021,
        None,
        None,
        ".mkv",
        "/M",
        "/S",
        tmdb_id=438631,
        video_format="Bluray-1080p",
        video_codec="x265",
    )
    expected = Path("/M") / "Dune (2021)" / "Dune (2021) (Bluray-1080p-x265) (tmdbid-438631).mkv"
    assert t == str(expected)


def test_suggested_target_drops_missing_quality_and_id() -> None:
    # no vf/vc/tmdb -> clean name, no empty parens or 'None'
    t = organizer.suggested_target("movie", "Dune", 2021, None, None, ".mkv", "/M", "/S")
    assert t == str(Path("/M") / "Dune (2021)" / "Dune (2021).mkv")


def test_normalize_reads_quality_and_id() -> None:
    s = organizer.normalize_suggestion(
        {
            "filename": "x.mkv",
            "type": "movie",
            "title": "Dune",
            "year": 2021,
            "tmdb_id": 438631,
            "video_format": "WEBDL-2160p",
            "video_codec": "x265",
            "confidence": 96,
        },
        "x.mkv",
        "/M",
        "/S",
    )
    assert s.tmdb_id == 438631
    assert s.video_format == "WEBDL-2160p"
    assert s.target is not None and "(WEBDL-2160p-x265) (tmdbid-438631)" in s.target


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


# --- local heuristic parser (no AI) --------------------------------------------


def test_parse_series_from_well_named_file() -> None:
    rel = (
        "Naruto Shippuden/Season 22/"
        "Naruto Shippuden (2007) - S22E34 - 492 - Cloud [WEBDL-1080p][AAC 2.0][JA][h265 8bit].mkv"
    )
    e = organizer.parse_media_filename(rel)
    assert e is not None
    assert e["type"] == "series"
    assert e["title"] == "Naruto Shippuden"
    assert e["year"] == 2007
    assert e["season"] == 22
    assert e["episode"] == 34
    assert e["episode_title"] == "Cloud"  # captured from the name
    assert e["video_format"] == "WEBDL-1080p"
    assert e["video_codec"] == "x265"


def test_parse_movie_defers_to_ai_when_text_after_year() -> None:
    # Year before the part/extra text -> collision-prone -> parser declines (None)
    assert organizer.parse_media_filename("Tih-Minh (1918) - EP. 01 - Prologo [1080p].mkv") is None
    assert organizer.parse_media_filename("El chico (1921) - Documentary [1080p].mkv") is None
    # Year at the END (no trailing text) -> safe, still claimed
    assert organizer.parse_media_filename("Les Vampires - EP.6 - Los ojos (1916) [1080p].mkv")


def test_suggested_target_series_full_ascii() -> None:
    t = organizer.suggested_target(
        "series",
        "Señora",  # accented -> must be stripped in the path
        2020,
        4,
        17,
        ".mkv",
        "/M",
        "/S",
        episode_title="Adiós, niño",
        tvdb_id=12345,
        video_format="WEBDL-1080p",
        video_codec="x265",
    )
    expected = (
        Path("/S")
        / "Senora (2020)"
        / "Season 04"
        / "Senora (2020) - S04E17 - Adios, nino (WEBDL-1080p-x265) (tvdbid-12345).mkv"
    )
    assert t == str(expected)


def test_parse_movie_from_well_named_file() -> None:
    e = organizer.parse_media_filename("Dune (2021) (Bluray-1080p) [x264].mkv")
    assert e is not None
    assert e["type"] == "movie"
    assert e["title"] == "Dune"
    assert e["year"] == 2021
    assert e["video_format"] == "Bluray-1080p"
    assert e["video_codec"] == "x264"


def test_parse_series_title_falls_back_to_show_folder() -> None:
    # filename has no title before the SxxEyy -> use the top folder name
    e = organizer.parse_media_filename("Breaking Bad/S01E01 [HDTV-720p][x265].mkv")
    assert e is not None
    assert e["title"] == "Breaking Bad"
    assert e["season"] == 1
    assert e["episode"] == 1
    assert e["video_format"] == "HDTV-720p"


def test_parse_returns_none_for_ambiguous() -> None:
    assert organizer.parse_media_filename("random_clip.mkv") is None


def test_parse_year_prefix_movie() -> None:
    e = organizer.parse_media_filename("extracted/2022 - Hotel Transilvania 4 - NEW.mkv")
    assert e is not None
    assert e["type"] == "movie"
    assert e["title"] == "Hotel Transilvania 4"
    assert e["year"] == 2022


def test_parse_txex_series() -> None:
    e = organizer.parse_media_filename("Ladybug/[Ladybug] T5E03 - Destruccion.mkv")
    assert e is not None
    assert e["type"] == "series"
    assert e["season"] == 5
    assert e["episode"] == 3


def test_parse_spanish_temporada_cap_series() -> None:
    e = organizer.parse_media_filename("Merli_Sapere_Aude_Temporada_1_HDTV_720pCap_101AC3_5_1.mkv")
    assert e is not None
    assert e["type"] == "series"
    assert e["title"] == "Merli Sapere Aude"
    assert e["season"] == 1
    assert e["episode"] == 101


def test_parse_bare_embedded_year_movie() -> None:
    e = organizer.parse_media_filename("Trumbo_La_Lista_Negra_2015_Spanish_English_Subs_HD.mkv")
    assert e is not None
    assert e["type"] == "movie"
    assert e["year"] == 2015
    assert "Trumbo" in str(e["title"])


def test_parse_timestamp_home_video_is_none() -> None:
    # "video_2026-06-17_..." is a date-stamped home video, not a film -> review
    assert organizer.parse_media_filename("video_2026-06-17_23-48-32.mp4") is None


def test_parseable_file_does_not_call_ai(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    source = tmp_path / "manuales"
    (source / "Show (2020)").mkdir(parents=True)
    (source / "Show (2020)" / "Show (2020) - S01E01 [WEBDL-1080p][x265].mkv").write_bytes(b"d")

    def boom(self: object, names: list[str], errors: object = None) -> list[dict[str, object]]:
        raise AssertionError("AI must not be called for parseable files")

    monkeypatch.setattr(organizer.GeminiClient, "identify", boom)
    monkeypatch.setattr(organizer.secrets, "require", lambda ref: "KEY")

    ctx = make_context(
        tmp_path,
        paths={"organizer_source": str(source), "movies_root": "/M", "series_root": "/S"},
        integrations={"gemini": {"api_key_ref": "GEMINI_API_KEY"}},
    )
    result = organizer.run(ctx)
    assert result.ok  # parser resolved it locally; no Gemini call, no failure
    plan = json.loads(
        (tmp_path / "reports" / "organizer" / "plan.json").read_text(encoding="utf-8")
    )
    assert plan["confident"][0]["title"] == "Show"


def test_ai_input_enriched_with_ffprobe_hints(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from adapters.ffprobe import MediaTags

    source = tmp_path / "manuales"
    source.mkdir()
    (source / "cryptic_xyz.mkv").write_bytes(b"d")  # parser can't resolve -> AI
    monkeypatch.setattr(
        organizer.ffprobe,
        "probe_tags",
        lambda p: MediaTags(1440.0, {"title": "Akira"}, ["jpn"]),
    )
    seen: dict[str, list[str]] = {}

    def fake(self: object, names: list[str], errors: object = None) -> list[dict[str, object]]:
        seen["names"] = list(names)
        return [
            {
                "filename": "cryptic_xyz.mkv",  # echo the path part (before |HINTS|)
                "type": "movie",
                "title": "Akira",
                "year": 1988,
                "confidence": 96,
            }
        ]

    monkeypatch.setattr(organizer.GeminiClient, "identify", fake)
    monkeypatch.setattr(organizer.secrets, "require", lambda ref: "KEY")

    ctx = make_context(
        tmp_path,
        paths={"organizer_source": str(source), "movies_root": "/M", "series_root": "/S"},
        integrations={"gemini": {"api_key_ref": "GEMINI_API_KEY"}},
    )
    result = organizer.run(ctx)

    assert result.ok
    assert seen["names"] and "|HINTS|" in seen["names"][0]
    assert "title=Akira" in seen["names"][0] and "dur=24min" in seen["names"][0]
    plan = json.loads(
        (tmp_path / "reports" / "organizer" / "plan.json").read_text(encoding="utf-8")
    )
    assert plan["confident"][0]["title"] == "Akira"  # mapped back despite the hints suffix


def test_ai_cascade_ollama_first_then_gemini(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "manuales"
    source.mkdir()
    # Two ambiguous files (no parens-year / no SxxEyy) -> parser can't resolve.
    (source / "weird_one.mkv").write_bytes(b"a")
    (source / "weird_two.mkv").write_bytes(b"b")
    seen: dict[str, list[str]] = {}

    def ollama_identify(
        self: object, names: list[str], errors: object = None
    ) -> list[dict[str, object]]:
        seen["ollama"] = list(names)
        # Ollama resolves only the first one; the other falls through to Gemini.
        return [{"filename": "weird_one.mkv", "type": "movie", "title": "One", "confidence": 95}]

    def gemini_identify(
        self: object, names: list[str], errors: object = None
    ) -> list[dict[str, object]]:
        seen["gemini"] = list(names)
        return [{"filename": "weird_two.mkv", "type": "movie", "title": "Two", "confidence": 92}]

    monkeypatch.setattr(organizer.OllamaClient, "identify", ollama_identify)
    monkeypatch.setattr(organizer.GeminiClient, "identify", gemini_identify)
    monkeypatch.setattr(organizer.secrets, "require", lambda ref: "KEY")

    ctx = make_context(
        tmp_path,
        paths={"organizer_source": str(source), "movies_root": "/M", "series_root": "/S"},
        integrations={
            "ai": {"providers": ["ollama", "gemini"]},
            "ollama": {"base_url": "http://ollama:11434", "model": "qwen3:8b"},
            "gemini": {"api_key_ref": "GEMINI_API_KEY"},
        },
    )
    result = organizer.run(ctx)

    assert result.ok
    assert sorted(seen["ollama"]) == ["weird_one.mkv", "weird_two.mkv"]  # Ollama tried both
    assert seen["gemini"] == ["weird_two.mkv"]  # Gemini only got the leftover
    plan = json.loads(
        (tmp_path / "reports" / "organizer" / "plan.json").read_text(encoding="utf-8")
    )
    got_titles = {s["title"] for s in plan["confident"]}
    assert got_titles == {"One", "Two"}  # both resolved across the cascade


# --- run integration -----------------------------------------------------------


def test_run_fails_without_source(tmp_path: Path) -> None:
    result = organizer.run(make_context(tmp_path))
    assert not result.ok
    assert result.failures[0].category == "config"


def test_run_cleans_junk_and_reports(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    source = tmp_path / "manuales"
    source.mkdir()
    (source / "Dune cryptic.mkv").write_bytes(b"data")
    (source / "Dune.nfo").write_text("x", encoding="utf-8")
    (source / "empty").mkdir()

    def fake_identify(
        self: object, names: list[str], errors: object = None
    ) -> list[dict[str, object]]:
        return [
            {
                "filename": "Dune cryptic.mkv",
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
    assert (source / "Dune cryptic.mkv").exists()  # media never moved (report-only)
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
                "filename": "Dune cryptic.mkv",
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
    (source / "Dune cryptic.mkv").write_bytes(b"data")
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
    assert (source / "Dune cryptic.mkv").exists()  # never moved


def test_apply_live_moves_confident_movie(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    source = tmp_path / "manuales"
    source.mkdir()
    src_file = source / "Dune cryptic.mkv"
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
    src_file = source / "Dune cryptic.mkv"
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
    dest = (
        tmp_path
        / "Series"
        / "Breaking Bad (2008)"
        / "Season 01"
        / "Breaking Bad (2008) - S01E01.mkv"
    )
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


def test_output_dirs_excluded_from_scan(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    source = tmp_path / "Manuales"
    movies_root = source / "Radarr"  # output lives INSIDE the source dump
    (movies_root / "Already (2020)").mkdir(parents=True)
    (movies_root / "Already (2020)" / "Already (2020).mkv").write_bytes(b"x")
    (source / "New.mkv").write_bytes(b"y")
    seen: dict[str, list[str]] = {}

    def fake(self: object, names: list[str], errors: object = None) -> list[dict[str, object]]:
        seen["names"] = list(names)
        return []

    monkeypatch.setattr(organizer.GeminiClient, "identify", fake)
    monkeypatch.setattr(organizer.secrets, "require", lambda ref: "KEY")

    ctx = make_context(
        tmp_path,
        paths={
            "organizer_source": str(source),
            "movies_root": str(movies_root),
            "series_root": str(tmp_path / "S"),
        },
        integrations={"gemini": {"api_key_ref": "GEMINI_API_KEY"}},
    )
    organizer.run(ctx)
    # The already-organized file under the Radarr output is skipped; only New.mkv.
    assert seen["names"] == ["New.mkv"]


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
    src_file = source / "Dune cryptic.mkv"
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


def test_apply_traversal_title_is_sanitised_inside_roots(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "manuales"
    source.mkdir()
    src_file = source / "Dune cryptic.mkv"
    src_file.write_bytes(b"data")

    def fake(self: object, names: list[str], errors: object = None) -> list[dict[str, object]]:
        # Hallucinated title with traversal — must NOT escape the movies root.
        return [
            {
                "filename": "Dune cryptic.mkv",
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

    assert result.ok
    # _safe_name strips the path separators, so the dest stays INSIDE the root
    # (defence in depth alongside relocate's allowed_roots guard).
    escaped = tmp_path.parent / "etc" / "Dune (2021)" / "Dune (2021).mkv"
    assert not escaped.exists()  # nothing written outside the roots
    assert not src_file.exists()  # moved (sanitised) within the root
    assert list((tmp_path / "Movies").rglob("*.mkv"))  # landed inside the movies root


def test_apply_collision_is_skipped_gracefully(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "manuales"
    source.mkdir()
    src_file = source / "Dune cryptic.mkv"
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
