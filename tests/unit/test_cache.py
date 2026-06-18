"""Tests for core.cache (persistent cross-run cache)."""

from __future__ import annotations

from pathlib import Path

from core.cache import Cache


def test_get_set() -> None:
    cache = Cache(Path("nonexistent") / "c.json")
    assert cache.get("x") is None
    cache.set("x", 5)
    assert cache.get("x") == 5


def test_roundtrip_persists(tmp_path: Path) -> None:
    path = tmp_path / "c.json"
    first = Cache(path)
    first.set("k", {"a": 1})
    first.save()
    assert path.is_file()
    second = Cache(path)
    assert second.get("k") == {"a": 1}


def test_save_noop_when_not_dirty(tmp_path: Path) -> None:
    path = tmp_path / "c.json"
    Cache(path).save()
    assert not path.exists()


def test_file_fingerprint_invalidation(tmp_path: Path) -> None:
    target = tmp_path / "f.bin"
    target.write_bytes(b"abc")
    cache = Cache(tmp_path / "c.json")
    assert cache.get_for_file(target, "ns") is None
    cache.set_for_file(target, "ns", {"v": 1})
    assert cache.get_for_file(target, "ns") == {"v": 1}
    target.write_bytes(b"abcd")  # size changes -> key changes -> miss
    assert cache.get_for_file(target, "ns") is None


def test_ignores_corrupt_file(tmp_path: Path) -> None:
    path = tmp_path / "c.json"
    path.write_text("{not json", encoding="utf-8")
    assert Cache(path).get("k") is None
