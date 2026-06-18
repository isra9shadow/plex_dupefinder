"""Tests for the opt-in PASS0 fingerprint fast-path (IMP-11).

The fast-path skips the expensive analyze+poll for items whose underlying
files are byte-identical to the last sane analyze. These tests exercise the
pure helpers (fingerprint + cache I/O) with real temp files and fake Plex
item objects — no Plex server required.

Run with:  pytest tests/test_pass0_cache.py -q
"""

from __future__ import annotations

import os

import plex_dupefinder as pd


class _Part:
    def __init__(self, file):
        self.file = file


class _Media:
    def __init__(self, parts):
        self.parts = parts


class _Item:
    """Minimal stand-in for a plexapi video item."""

    def __init__(self, key, files):
        self.key = key
        self.media = [_Media([_Part(f) for f in files])]


def test_fingerprint_changes_with_file_bytes(tmp_path, monkeypatch):
    # Identity path mapping: resolve_fs_path returns the path unchanged.
    monkeypatch.setattr(pd, "resolve_fs_path", lambda p: p)
    f = tmp_path / "movie.mkv"
    f.write_bytes(b"hello")
    item = _Item("/library/metadata/1", [str(f)])

    fp1 = pd._pass0_fingerprint(item)
    assert fp1 is not None
    # Stable across repeated reads while bytes are unchanged.
    assert pd._pass0_fingerprint(item) == fp1

    # Growing the file (new size) changes the fingerprint -> cache miss.
    f.write_bytes(b"hello world")
    assert pd._pass0_fingerprint(item) != fp1


def test_fingerprint_none_when_unreadable(tmp_path, monkeypatch):
    monkeypatch.setattr(pd, "resolve_fs_path", lambda p: p)
    item = _Item("/k", [str(tmp_path / "missing.mkv")])
    # Missing file -> None -> fast-path disabled (fail-safe: re-analyze).
    assert pd._pass0_fingerprint(item) is None


def test_fingerprint_none_when_no_parts(monkeypatch):
    monkeypatch.setattr(pd, "resolve_fs_path", lambda p: p)

    class _Empty:
        def __init__(self):
            self.key = "/k"
            self.media = []

    assert pd._pass0_fingerprint(_Empty()) is None


def test_cache_roundtrip(tmp_path, monkeypatch):
    cache_file = tmp_path / "pass0_fingerprints.json"
    monkeypatch.setattr(pd, "pass0_cache_filename", str(cache_file))
    monkeypatch.setattr(pd, "default_plans_dir", str(tmp_path))

    assert pd._load_pass0_cache() == {}  # absent file -> empty
    pd._save_pass0_cache({"/k": {"fingerprint": "abc", "status": "sane_unchanged"}})
    assert pd._load_pass0_cache()["/k"]["fingerprint"] == "abc"


def test_cache_corrupt_file_yields_empty(tmp_path, monkeypatch):
    cache_file = tmp_path / "pass0_fingerprints.json"
    cache_file.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(pd, "pass0_cache_filename", str(cache_file))
    # Never raise on a corrupt cache — it's an optimisation, not truth.
    assert pd._load_pass0_cache() == {}


def test_save_is_atomic(tmp_path, monkeypatch):
    cache_file = tmp_path / "pass0_fingerprints.json"
    monkeypatch.setattr(pd, "pass0_cache_filename", str(cache_file))
    monkeypatch.setattr(pd, "default_plans_dir", str(tmp_path))
    pd._save_pass0_cache({"/k": {"fingerprint": "x"}})
    # No leftover temp file after an atomic replace.
    assert not os.path.exists(str(cache_file) + ".tmp")
