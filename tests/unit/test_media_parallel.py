"""media_integrity parallel probing (IMP-04): correctness + cache interaction."""

from __future__ import annotations

from pathlib import Path

import pytest
from adapters.ffprobe import MediaProbe
from core.cache import SqliteCache
from modules.media import media_integrity as mi


def test_probe_all_parallel_maps_results(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    files = []
    for i in range(5):
        media = tmp_path / f"f{i}.mkv"
        media.write_bytes(b"x")
        files.append(media)

    monkeypatch.setattr(
        mi.ffprobe,
        "probe",
        lambda path, **_kw: MediaProbe(True, True, float(int(path.stem[1:]) * 60), True),
    )
    probes = mi._probe_all(files, SqliteCache(tmp_path / "c.db"), workers=4)

    assert len(probes) == 5
    assert probes[files[3]].duration_seconds == 180.0  # f3 -> 3*60, correctly mapped


def test_probe_all_uses_cache(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    media = tmp_path / "f.mkv"
    media.write_bytes(b"x")
    calls = {"n": 0}

    def fake(path: Path, **_kw: object) -> MediaProbe:
        calls["n"] += 1
        return MediaProbe(True, True, 3600.0, True)

    monkeypatch.setattr(mi.ffprobe, "probe", fake)
    cache = SqliteCache(tmp_path / "c.db")
    mi._probe_all([media], cache, workers=4)
    mi._probe_all([media], cache, workers=4)  # served from cache
    assert calls["n"] == 1


def test_probe_all_isolates_a_failing_probe(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    files = []
    for i in range(3):
        f = tmp_path / f"f{i}.mkv"
        f.write_bytes(b"x")
        files.append(f)

    def flaky(path: Path, **_kw: object) -> MediaProbe:
        if path.name == "f1.mkv":
            raise RuntimeError("boom")
        return MediaProbe(True, True, 60.0, True)

    monkeypatch.setattr(mi.ffprobe, "probe", flaky)
    probes = mi._probe_all(files, SqliteCache(tmp_path / "c.db"), workers=4)

    assert len(probes) == 3  # one failure does not abort the batch
    assert probes[files[1]].decodes_ok is False  # the failing file is isolated
    assert probes[files[0]].decodes_ok is True
