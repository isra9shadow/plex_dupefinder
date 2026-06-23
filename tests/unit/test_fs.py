"""Tests for core.fs — the single chokepoint for moving/deleting user data."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from core.config import Config, SafetyConfig
from core.errors import QuarantineError, SafetyError, ValidationError
from core.fs import Fs
from core.types import QuarantineEntry


def _cfg(qdir: Path, retention: float = 7.0) -> Config:
    return Config(safety=SafetyConfig(quarantine_dir=qdir, retention_days=retention))


def test_quarantine_moves_with_sidecar(tmp_path: Path) -> None:
    src = tmp_path / "movie.mkv"
    src.write_bytes(b"data")
    fs = Fs(_cfg(tmp_path / "q"), dry_run=False)
    entry = fs.quarantine(src, reason="bad")
    assert not src.exists()
    assert Path(entry.quarantine_path).exists()
    sidecar = json.loads(Path(entry.sidecar_path).read_text(encoding="utf-8"))
    assert sidecar["original_path"] == str(src)
    assert entry.restore_command.startswith("mv -- ")


def test_quarantine_dry_run_noop(tmp_path: Path) -> None:
    src = tmp_path / "movie.mkv"
    src.write_bytes(b"data")
    qdir = tmp_path / "q"
    entry = Fs(_cfg(qdir), dry_run=True).quarantine(src, reason="bad")
    assert src.exists()
    assert not Path(entry.quarantine_path).exists()
    assert not qdir.exists()


def test_quarantine_missing_raises(tmp_path: Path) -> None:
    with pytest.raises(QuarantineError):
        Fs(_cfg(tmp_path / "q"), dry_run=False).quarantine(tmp_path / "nope", reason="x")


def test_restore_round_trip(tmp_path: Path) -> None:
    src = tmp_path / "a.mkv"
    src.write_bytes(b"data")
    fs = Fs(_cfg(tmp_path / "q"), dry_run=False)
    entry = fs.quarantine(src, reason="x")
    assert fs.restore(entry) == src
    assert src.exists()
    assert not Path(entry.quarantine_path).exists()


def test_restore_dry_run(tmp_path: Path) -> None:
    entry = QuarantineEntry("o", "q", "s", "r", "c", 0.0)
    assert Fs(_cfg(tmp_path / "q"), dry_run=True).restore(entry) == Path("o")


def test_restore_missing_raises(tmp_path: Path) -> None:
    entry = QuarantineEntry(str(tmp_path / "o"), str(tmp_path / "missing"), "s", "r", "c", 0.0)
    with pytest.raises(QuarantineError):
        Fs(_cfg(tmp_path / "q"), dry_run=False).restore(entry)


def test_purge_deletes_old(tmp_path: Path) -> None:
    src = tmp_path / "a.mkv"
    src.write_bytes(b"data")
    fs = Fs(_cfg(tmp_path / "q", retention=0.0), dry_run=False)
    fs.quarantine(src, reason="x")
    result = fs.purge(retention_days=0.0)
    assert result.deleted == 1
    assert result.bytes_reclaimed > 0
    assert result.simulated is False
    assert list((tmp_path / "q").glob("*")) == []


def test_purge_simulate_keeps(tmp_path: Path) -> None:
    src = tmp_path / "a.mkv"
    src.write_bytes(b"data")
    fs = Fs(_cfg(tmp_path / "q"), dry_run=False)
    entry = fs.quarantine(src, reason="x")
    result = fs.purge(retention_days=0.0, simulate=True)
    assert result.deleted == 1
    assert result.simulated is True
    assert Path(entry.quarantine_path).exists()


def test_purge_keeps_fresh(tmp_path: Path) -> None:
    src = tmp_path / "a.mkv"
    src.write_bytes(b"data")
    fs = Fs(_cfg(tmp_path / "q", retention=7.0), dry_run=False)
    fs.quarantine(src, reason="x")
    assert fs.purge().deleted == 0


def test_purge_no_dir(tmp_path: Path) -> None:
    assert Fs(_cfg(tmp_path / "nope"), dry_run=False).purge().deleted == 0


def test_quarantine_dest_stays_within_quarantine_dir(tmp_path: Path) -> None:
    src = tmp_path / "movie.mkv"
    src.write_bytes(b"data")
    qdir = tmp_path / "q"
    entry = Fs(_cfg(qdir), dry_run=False).quarantine(src, reason="bad")
    assert Path(entry.quarantine_path).resolve().is_relative_to(qdir.resolve())


def test_quarantine_same_name_no_collision(tmp_path: Path) -> None:
    qdir = tmp_path / "q"
    fs = Fs(_cfg(qdir), dry_run=False)
    dests = set()
    for sub in ("a", "b"):
        d = tmp_path / sub
        d.mkdir()
        f = d / "movie.mkv"  # identical basename
        f.write_bytes(b"data")
        dests.add(Fs(_cfg(qdir), dry_run=False).quarantine(f, reason="x").quarantine_path)
    assert len(dests) == 2  # uuid suffix prevents collision
    del fs


def test_restore_rejects_path_outside_quarantine(tmp_path: Path) -> None:
    # Tampered sidecar pointing the source outside the quarantine dir.
    evil = tmp_path / "etc_passwd"
    evil.write_bytes(b"x")
    entry = QuarantineEntry(str(tmp_path / "dst"), str(evil), "s", "x", "c", 0.0)
    with pytest.raises(QuarantineError):
        Fs(_cfg(tmp_path / "q"), dry_run=False).restore(entry)


def test_relocate_moves_and_creates_parents(tmp_path: Path) -> None:
    src = tmp_path / "manuales" / "Dune.2021.mkv"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"data")
    dest = tmp_path / "movies" / "Dune (2021)" / "Dune (2021).mkv"
    rec = Fs(_cfg(tmp_path / "q"), dry_run=False).relocate(src, dest, reason="organize")
    assert not src.exists()
    assert dest.exists()
    assert rec.action == "relocate" and rec.dest == str(dest) and rec.bytes == 4


def test_relocate_dry_run_noop(tmp_path: Path) -> None:
    src = tmp_path / "a.mkv"
    src.write_bytes(b"data")
    dest = tmp_path / "movies" / "A (2020)" / "A.mkv"
    rec = Fs(_cfg(tmp_path / "q"), dry_run=True).relocate(src, dest, reason="x")
    assert src.exists()  # nothing moved
    assert not dest.exists()
    assert rec.dest == str(dest)


def test_relocate_refuses_to_overwrite(tmp_path: Path) -> None:
    src = tmp_path / "a.mkv"
    src.write_bytes(b"data")
    dest = tmp_path / "b.mkv"
    dest.write_bytes(b"existing")  # already there → never clobber
    with pytest.raises(SafetyError):
        Fs(_cfg(tmp_path / "q"), dry_run=False).relocate(src, dest, reason="x")
    assert src.exists() and dest.read_bytes() == b"existing"


def test_relocate_missing_source_raises(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        Fs(_cfg(tmp_path / "q"), dry_run=False).relocate(
            tmp_path / "nope.mkv", tmp_path / "d.mkv", reason="x"
        )


def test_relocate_identical_src_dest_raises(tmp_path: Path) -> None:
    src = tmp_path / "a.mkv"
    src.write_bytes(b"data")
    with pytest.raises(ValidationError):
        Fs(_cfg(tmp_path / "q"), dry_run=False).relocate(src, src, reason="x")


def test_quarantine_and_purge_directory(tmp_path: Path) -> None:
    show = tmp_path / "show"
    (show / "s01").mkdir(parents=True)
    (show / "s01" / "e.mkv").write_bytes(b"xyz")
    fs = Fs(_cfg(tmp_path / "q", retention=0.0), dry_run=False)
    fs.quarantine(show, reason="x")
    result = fs.purge(retention_days=0.0)
    assert result.deleted == 1
    assert result.bytes_reclaimed >= 3
