"""Guards for the opt-in cross-run partial-hash cache (perf, default OFF).

The cache persists file head/tail hashes across runs so re-runs skip re-reading
each file. It must: (1) round-trip, (2) persist ONLY the keys seen this run (so
the store stays bounded to the live library), (3) invalidate on file change via
the mtime_ns+size in the key, and (4) mark every access as seen.
"""

from __future__ import annotations

import plex_dupefinder as pd
import pytest


@pytest.fixture(autouse=True)
def _isolated_store(tmp_path, monkeypatch):
    monkeypatch.setattr(pd, "partial_hash_cache_filename", str(tmp_path / "ph.db"))
    pd._partial_hash_memo.clear()
    pd._partial_hash_seen.clear()
    yield
    pd._partial_hash_memo.clear()
    pd._partial_hash_seen.clear()


def _key(path, mtime_ns=111, size=10, hb=1024):
    return (path, mtime_ns, size, hb)


def test_save_load_roundtrip() -> None:
    memo = {_key("/m/a.mkv"): {"size": 10, "head_sha256": "aa", "tail_sha256": "bb"}}
    pd._save_partial_hash_cache(memo, set(memo))
    loaded = pd._load_partial_hash_cache()
    assert loaded == memo


def test_persists_only_seen_keys() -> None:
    ka, kb = _key("/m/a.mkv"), _key("/m/b.mkv")
    memo = {
        ka: {"size": 10, "head_sha256": "aa", "tail_sha256": "bb"},
        kb: {"size": 10, "head_sha256": "cc", "tail_sha256": "dd"},
    }
    pd._save_partial_hash_cache(memo, {ka})  # only ka seen this run
    loaded = pd._load_partial_hash_cache()
    assert set(loaded) == {ka}  # kb (not re-seen) is dropped -> bounded growth


def test_load_missing_store_is_empty() -> None:
    assert pd._load_partial_hash_cache() == {}


def test_compute_marks_seen_and_memoises(tmp_path) -> None:
    f = tmp_path / "movie.mkv"
    f.write_bytes(b"x" * 5000)
    result = pd.compute_partial_hashes(str(f), hash_bytes=1024)
    assert result is not None and result["size"] == 5000
    # The exact memo_key for this file/run is marked seen and memoised.
    import os

    st = os.stat(str(f))
    key = (str(f), st.st_mtime_ns, 5000, 1024)
    assert key in pd._partial_hash_seen
    assert pd._partial_hash_memo[key] == result
    # Second call is a hit (same object from the memo).
    assert pd.compute_partial_hashes(str(f), hash_bytes=1024) == result


def test_changed_file_invalidates_via_key(tmp_path) -> None:
    f = tmp_path / "movie.mkv"
    f.write_bytes(b"x" * 5000)
    import os

    st = os.stat(str(f))
    stale_key = (str(f), st.st_mtime_ns - 1, 5000, 1024)  # a different mtime
    pd._partial_hash_memo[stale_key] = {
        "size": 5000, "head_sha256": "STALE", "tail_sha256": "STALE"
    }
    # Current mtime differs from the stale key -> miss -> fresh compute, not "STALE".
    result = pd.compute_partial_hashes(str(f), hash_bytes=1024)
    assert result["head_sha256"] != "STALE"
