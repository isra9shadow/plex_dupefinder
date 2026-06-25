"""Tests for adapters.sysstat (/proc parsing, graceful failure)."""

from __future__ import annotations

from pathlib import Path

from adapters import sysstat

_MEMINFO = """\
MemTotal:       16384000 kB
MemFree:         1000000 kB
MemAvailable:    8192000 kB
Buffers:          200000 kB
"""

_LOADAVG = "0.42 0.55 0.60 1/512 12345\n"


def _write_proc(root: Path, meminfo: str, loadavg: str) -> Path:
    (root / "meminfo").write_text(meminfo, encoding="utf-8")
    (root / "loadavg").write_text(loadavg, encoding="utf-8")
    return root


def test_parses_meminfo_and_loadavg(tmp_path: Path) -> None:
    proc = _write_proc(tmp_path, _MEMINFO, _LOADAVG)
    stats = sysstat.system_stats(proc_root=proc)
    assert stats is not None
    assert stats.mem_total_kb == 16384000
    assert stats.mem_available_kb == 8192000
    assert stats.load1 == 0.42


def test_mem_used_pct(tmp_path: Path) -> None:
    proc = _write_proc(tmp_path, _MEMINFO, _LOADAVG)
    stats = sysstat.system_stats(proc_root=proc)
    assert stats is not None
    # used = 16384000 - 8192000 = 50%
    assert stats.mem_used_pct == 50


def test_none_when_files_missing(tmp_path: Path) -> None:
    assert sysstat.system_stats(proc_root=tmp_path) is None


def test_none_when_key_missing(tmp_path: Path) -> None:
    proc = _write_proc(tmp_path, "MemTotal: 100 kB\n", _LOADAVG)
    assert sysstat.system_stats(proc_root=proc) is None


def test_none_when_loadavg_malformed(tmp_path: Path) -> None:
    proc = _write_proc(tmp_path, _MEMINFO, "")
    assert sysstat.system_stats(proc_root=proc) is None
