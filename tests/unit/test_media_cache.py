"""media_integrity reuses cached ffprobe results across runs (IMP-06)."""

from __future__ import annotations

from pathlib import Path

import pytest
from adapters.ffprobe import MediaProbe
from modules.media import media_integrity as mi
from tests.fakes import make_context


def test_probes_cached_between_runs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls = {"n": 0}

    def fake_probe(path: Path, **_kw: object) -> MediaProbe:
        calls["n"] += 1
        return MediaProbe(True, True, 3600.0, True)

    monkeypatch.setattr(mi.ffprobe, "probe", fake_probe)
    movies = tmp_path / "movies" / "Movie (2020)"
    movies.mkdir(parents=True)
    (movies / "m.mkv").write_bytes(b"x")
    ctx = make_context(
        tmp_path,
        paths={"movies_root": str(tmp_path / "movies"), "series_root": str(tmp_path / "series")},
    )

    mi.run(ctx)  # first run: probes and caches
    mi.run(ctx)  # second run: served from the persistent cache

    assert calls["n"] == 1
