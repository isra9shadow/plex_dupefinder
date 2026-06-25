"""Tests for adapters.archive (unar invocation, injected runner — no unar)."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from adapters import archive
from adapters.command import CommandResult


def test_extract_builds_unar_argv_and_returns_ok() -> None:
    captured: list[Sequence[str]] = []

    def runner(argv: Sequence[str]) -> CommandResult:
        captured.append(argv)
        return CommandResult(tuple(argv), 0, "", "")

    archive_path = Path("/dl/movie.part1.rar")
    dest = Path("/dl")
    ok = archive.extract(archive_path, dest, runner=runner)
    assert ok is True
    assert captured[0] == ["unar", "-q", "-f", "-D", "-o", str(dest), str(archive_path)]


def test_extract_false_on_nonzero() -> None:
    def runner(argv: Sequence[str]) -> CommandResult:
        return CommandResult(tuple(argv), 1, "", "corrupt archive")

    assert archive.extract(Path("/dl/x.rar"), Path("/dl"), runner=runner) is False
