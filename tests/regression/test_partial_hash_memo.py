"""Tests for the within-run partial-hash memo (A2-02 / reduce duplicate reads).

The hash is a pure function of (path, mtime, size, hash_bytes), so reuse is
result-identical; it just avoids re-reading the same file in PASS 2 and across
the parallel pre-gather. A changed file (new size/mtime) misses and re-reads,
keeping the PASS1↔PASS2 drift check correct.

Run with:  pytest tests/regression/test_partial_hash_memo.py -q
"""

from __future__ import annotations

from pathlib import Path

import plex_dupefinder as pd


def test_memo_reuses_for_unchanged_file(tmp_path: Path) -> None:
    pd._partial_hash_memo.clear()
    f = tmp_path / "m.mkv"
    f.write_bytes(b"abcdefgh")

    first = pd.compute_partial_hashes(str(f), hash_bytes=4)
    assert first is not None
    assert len(pd._partial_hash_memo) == 1

    # Second call on the unchanged file is served from the memo (same object).
    second = pd.compute_partial_hashes(str(f), hash_bytes=4)
    assert second is first
    assert len(pd._partial_hash_memo) == 1


def test_memo_invalidates_on_size_change(tmp_path: Path) -> None:
    pd._partial_hash_memo.clear()
    f = tmp_path / "m.mkv"
    f.write_bytes(b"abcdefgh")
    before = pd.compute_partial_hashes(str(f), hash_bytes=4)

    f.write_bytes(b"abcdefghIJKL")  # size changes -> new key -> fresh read
    after = pd.compute_partial_hashes(str(f), hash_bytes=4)

    assert after is not before
    assert after["size"] != before["size"]
    assert len(pd._partial_hash_memo) == 2


def test_missing_file_returns_none(tmp_path: Path) -> None:
    pd._partial_hash_memo.clear()
    assert pd.compute_partial_hashes(str(tmp_path / "nope.mkv")) is None


def test_empty_path_returns_none() -> None:
    assert pd.compute_partial_hashes("") is None
