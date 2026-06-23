"""Tests for the targeted (per-item) Plex refresh after deletions.

On huge libraries a full ``section.update()`` is slow; we instead partial-scan
only the affected show/movie folder via ``section.update(path=...)``. These tests
cover the target-planning logic and the partial-refresh trigger without touching
a real Plex server.
"""

import plex_dupefinder as pd


class _Show:
    def __init__(self, locations):
        self.locations = locations


class _Episode:
    type = "episode"

    def __init__(self, show):
        self._show = show

    def show(self):
        return self._show


class _Movie:
    type = "movie"

    def __init__(self, locations):
        self.locations = locations


def test_plan_targets_episode_uses_show_root():
    item = _Episode(_Show(["/tv/Charmed"]))
    assert pd.plan_item_refresh_targets(item, "Series") == {("Series", "/tv/Charmed")}


def test_plan_targets_movie_uses_parent_folder():
    item = _Movie(["/movies/Dune (2021)/Dune.mkv"])
    assert pd.plan_item_refresh_targets(item, "Peliculas") == {
        ("Peliculas", "/movies/Dune (2021)")
    }


def test_plan_targets_empty_when_unresolvable():
    class _Bare:
        type = "movie"
        locations = ()

    assert pd.plan_item_refresh_targets(_Bare(), "Peliculas") == set()


def test_plan_targets_show_lookup_failure_is_safe():
    class _BadEpisode:
        type = "episode"

        def show(self):
            raise RuntimeError("plex down")

    assert pd.plan_item_refresh_targets(_BadEpisode(), "Series") == set()


class _Section:
    def __init__(self, sink):
        self._sink = sink

    def update(self, path=None):
        self._sink.append(path)


class _Library:
    def __init__(self, sink):
        self._sink = sink

    def section(self, name):
        return _Section(self._sink)


class _Plex:
    def __init__(self, sink):
        self.library = _Library(sink)


def test_refresh_targets_partial_scans_each_folder(monkeypatch):
    sink = []
    monkeypatch.setattr(pd, "plex", _Plex(sink))
    monkeypatch.setitem(pd.cfg, "PLEX_REFRESH_AFTER", True)
    result = pd.refresh_plex_targets(
        {("Series", "/tv/Charmed"), ("Peliculas", "/movies/Dune (2021)")}
    )
    assert result["attempted"] is True
    assert sorted(sink) == ["/movies/Dune (2021)", "/tv/Charmed"]
    assert len(result["paths"]) == 2
    assert result["scope"] == "item"


def test_refresh_targets_noop_when_disabled(monkeypatch):
    sink = []
    monkeypatch.setattr(pd, "plex", _Plex(sink))
    monkeypatch.setitem(pd.cfg, "PLEX_REFRESH_AFTER", False)
    result = pd.refresh_plex_targets({("Series", "/tv/Charmed")})
    assert result["attempted"] is False
    assert sink == []


def test_refresh_targets_noop_when_empty(monkeypatch):
    sink = []
    monkeypatch.setattr(pd, "plex", _Plex(sink))
    monkeypatch.setitem(pd.cfg, "PLEX_REFRESH_AFTER", True)
    assert pd.refresh_plex_targets(set())["attempted"] is False
    assert sink == []
