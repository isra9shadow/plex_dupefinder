"""Tests for modules.arr.orphans (report-only orphan detection)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from modules.arr import orphans
from tests.fakes import make_context


class _Stub:
    def __init__(self, items: list[dict[str, object]]) -> None:
        self._items = items

    def movies(self) -> list[dict[str, object]]:
        return self._items

    def series(self) -> list[dict[str, object]]:
        return self._items


def test_find_orphans_excludes_system_dirs() -> None:
    assert orphans.find_orphans({"A", "B", "@eaDir"}, {"A"}) == ["B"]


def test_mapped_names() -> None:
    assert orphans._mapped_names([{"path": "/x/A"}, {"path": 123}, {"nope": 1}]) == {"A"}


def test_top_level_dirs(tmp_path: Path) -> None:
    (tmp_path / "d1").mkdir()
    (tmp_path / "f.txt").write_text("x", encoding="utf-8")
    assert orphans._top_level_dirs(tmp_path) == {"d1"}
    assert orphans._top_level_dirs(tmp_path / "nope") == set()


def test_service_resolves_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(orphans.secrets, "require", lambda name: "KEY123")
    url, key = orphans._service({"url": "http://r", "api_key_ref": "RADARR_API_KEY"})
    assert url == "http://r"
    assert key == "KEY123"


def test_run_reports_orphans(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    movies_root = tmp_path / "movies"
    (movies_root / "Mapped (2020)").mkdir(parents=True)
    (movies_root / "Orphan (1999)").mkdir()
    series_root = tmp_path / "series"
    (series_root / "Mapped Show").mkdir(parents=True)
    (series_root / "Orphan Show").mkdir()
    monkeypatch.setattr(orphans, "_radarr", lambda ctx: _Stub([{"path": "/movies/Mapped (2020)"}]))
    monkeypatch.setattr(orphans, "_sonarr", lambda ctx: _Stub([{"path": "/tv/Mapped Show"}]))

    ctx = make_context(
        tmp_path, paths={"movies_root": str(movies_root), "series_root": str(series_root)}
    )
    result = orphans.run(ctx)

    assert result.actions == 2
    assert result.ok
    data = json.loads((tmp_path / "reports" / "arr" / "orphans.json").read_text(encoding="utf-8"))
    assert data["movies"] == ["Orphan (1999)"]
    assert data["series"] == ["Orphan Show"]
    assert "Orphan (1999)" in (tmp_path / "reports" / "arr" / "orphans.md").read_text(
        encoding="utf-8"
    )


def test_run_fails_without_integration_config(tmp_path: Path) -> None:
    result = orphans.run(make_context(tmp_path))
    assert not result.ok
    assert result.failures[0].category == "config"
