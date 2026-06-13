"""Tests for modules.media.media_integrity (verdict engine + report-only run)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from adapters.ffprobe import MediaProbe
from core.types import Verdict
from modules.media import media_integrity as mi
from tests.fakes import make_context


def _p(video: bool, audio: bool, dur: float, decodes: bool) -> MediaProbe:
    return MediaProbe(video, audio, dur, decodes)


def test_classify_no_video() -> None:
    assert mi.classify(_p(False, True, 100, True), "movie")[0] is Verdict.BAD


def test_classify_no_duration_decodes() -> None:
    assert mi.classify(_p(True, True, 0, True), "movie") == (
        Verdict.DOUBTFUL,
        "no_duration_decodes",
    )


def test_classify_no_duration_no_decode() -> None:
    assert mi.classify(_p(True, True, 0, False), "movie")[0] is Verdict.BAD


def test_classify_decode_error() -> None:
    assert mi.classify(_p(True, True, 3600, False), "movie") == (Verdict.BAD, "decode_error")


def test_classify_too_short_movie() -> None:
    assert mi.classify(_p(True, True, 600, True), "movie")[1] == "too_short"


def test_classify_too_short_series() -> None:
    assert mi.classify(_p(True, True, 100, True), "series")[1] == "too_short"


def test_classify_ok() -> None:
    assert mi.classify(_p(True, True, 3600, True), "movie")[0] is Verdict.OK


def test_classify_runtime_hard() -> None:
    # expected 120m, actual 60m -> diff 60 > hard(42) -> BAD
    assert mi.classify(_p(True, True, 3600, True), "movie", 120.0)[0] is Verdict.BAD


def test_classify_runtime_soft() -> None:
    # expected 95m, actual 70m -> diff 25 > soft(20) but < hard(35) -> DOUBTFUL
    assert mi.classify(_p(True, True, 70 * 60, True), "movie", 95.0)[0] is Verdict.DOUBTFUL


def test_title_year() -> None:
    assert mi._title_year("Movie Name (2020)") == ("Movie Name", 2020)
    assert mi._title_year("No Year")[1] is None


def test_run_reports_only(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    movies = tmp_path / "movies"
    (movies / "Good (2020)").mkdir(parents=True)
    (movies / "Good (2020)" / "good.mkv").write_bytes(b"x")
    (movies / "Bad (2019)").mkdir()
    (movies / "Bad (2019)" / "bad.mkv").write_bytes(b"x")

    def fake_probe(path: Path, **_kw: object) -> MediaProbe:
        if "bad" in path.name:
            return MediaProbe(False, False, 0.0, False)
        return MediaProbe(True, True, 3600.0, True)

    monkeypatch.setattr(mi.ffprobe, "probe", fake_probe)
    ctx = make_context(
        tmp_path,
        paths={"movies_root": str(movies), "series_root": str(tmp_path / "series")},
    )
    result = mi.run(ctx)

    assert result.actions == 1  # one flagged
    assert "probe_ms" in result.metrics  # IMP-03 profiling emitted
    data = json.loads(
        (tmp_path / "reports" / "media" / "integrity.json").read_text(encoding="utf-8")
    )
    verdicts = {Path(r["file"]).name: r["verdict"] for r in data}
    assert verdicts["good.mkv"] == "ok"
    assert verdicts["bad.mkv"] == "bad"
    assert "bad.mkv" in (tmp_path / "reports" / "media" / "integrity.md").read_text(
        encoding="utf-8"
    )
