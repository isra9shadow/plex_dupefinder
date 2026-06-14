"""Parity lock: the ported quarantine path builder must match the legacy EXACTLY.

This is the oracle that makes the eventual native migration safe — if the port ever
drifts from ``plex_dupefinder._quarantine_logical_path`` (the pure logical-rel part)
or from the legacy ``os.path.join(QUARANTINE_DIR, logical_rel)`` join, these fail.

The legacy collision handling (top-folder library suffix, timestamp fallback) is FS
dependent and intentionally NOT ported, so it is not exercised here — only the pure
path construction is locked.

Run with:  pytest tests/regression/test_dedupe_quarantine_parity.py -q
"""

from __future__ import annotations

import os

import plex_dupefinder as pd
import pytest
from modules.media.dedupe.quarantine import logical_relative_path, quarantine_logical_path

QUARANTINE_DIR = "/mnt/user/appdata/plex_dupefinder/quarantine"

# (src, title) pairs spanning movies, TV, unsafe chars, nesting and odd titles.
_CASES: list[tuple[str, str]] = [
    # Movie folder anchored by partial-title match ("Dune (2021)" vs "Dune").
    ("/mnt/user/Media/Movies/Dune (2021)/Dune.2021.2160p.mkv", "Dune"),
    # Movie, exact "Title (Year)" both as folder and title.
    ("/mnt/user/Media/Movies/Blade Runner 2049 (2017)/br2049.mkv", "Blade Runner 2049 (2017)"),
    # TV: Show / Season NN / Episode.
    ("/mnt/user/Media/TV/Breaking Bad/Season 01/ep.mkv", "Breaking Bad"),
    ("/mnt/user/Media/TV/Breaking Bad/Season 05/S05E14.mkv", "Breaking Bad"),
    # Title with unsafe / special chars (colon, slash-like, ampersand, apostrophe).
    ("/mnt/user/Media/Movies/M3GAN/movie.mkv", "M3GAN"),
    ("/srv/Movies/Mission Impossible - Dead Reckoning/mi.mkv", "Mission: Impossible"),
    ("/srv/TV/Marvel's Agents of S.H.I.E.L.D/s01e01.mkv", "Marvel's Agents of S.H.I.E.L.D."),
    ("/srv/TV/AT&T Presents - The Show/ep.mkv", "AT&T Presents: The Show!"),
    # Title with embedded slash (path-separator char inside the title string).
    ("/srv/Movies/Face Off (1997)/faceoff.mkv", "Face/Off"),
    # No-match → fallback to last two dirs + filename.
    ("/mnt/user/Media/Movies/Some Folder/Inner/file.mkv", "Totally Different Title"),
    # Deeply nested path, anchored title appears mid-tree.
    ("/a/b/c/d/Stranger Things/Season 02/Chapter.One.mkv", "Stranger Things"),
    # Windows-style backslash separators in the source.
    ("D:\\Media\\Movies\\Inception (2010)\\inception.mkv", "Inception"),
    ("\\\\nas\\Media\\TV\\The Office\\Season 03\\ep.mkv", "The Office"),
    # filename only (no directory component).
    ("just-a-file.mkv", "Whatever"),
    ("/leadingslashfile.mkv", "Whatever"),
    # Empty / whitespace / odd titles → no title_key, fallback path.
    ("/mnt/user/Media/Movies/Folder A/Folder B/movie.mkv", ""),
    ("/mnt/user/Media/Movies/Folder A/Folder B/movie.mkv", "   "),
    ("/mnt/user/Media/Movies/Folder A/Folder B/movie.mkv", "!!!"),
    # Title that is a substring of a dir AND title contains the dir (mutual containment edge).
    ("/x/y/The Matrix/Season 01/neo.mkv", "Matrix"),
    # Empty src.
    ("", "Anything"),
    # src that normalises to no parts (only separators).
    ("///", "Anything"),
    # Single trailing-separator directory.
    ("/movies/", "movies"),
    # Title matches the very first directory after root.
    ("/Avatar (2009)/extras/feature.mkv", "Avatar"),
    # Numeric-only and unicode titles.
    ("/media/1917 (2019)/1917.mkv", "1917"),
    ("/media/Amélie (2001)/amelie.mkv", "Amélie"),
]


@pytest.mark.parametrize(("src", "title"), _CASES)
def test_logical_relative_matches_legacy(src: str, title: str) -> None:
    assert logical_relative_path(src, title) == pd._quarantine_logical_path(src, title)


@pytest.mark.parametrize(("src", "title"), _CASES)
def test_absolute_join_matches_legacy(src: str, title: str) -> None:
    legacy_rel = pd._quarantine_logical_path(src, title)
    legacy_dest = os.path.join(QUARANTINE_DIR, legacy_rel)
    assert quarantine_logical_path(src, title, QUARANTINE_DIR) == legacy_dest
