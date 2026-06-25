"""Tests for adapters.gpu (nvidia-smi parsing, graceful failure)."""

from __future__ import annotations

from collections.abc import Callable, Sequence

from adapters import gpu
from adapters.command import CommandResult

Runner = Callable[[Sequence[str]], CommandResult]


def _ok(stdout: str) -> Runner:
    def run(argv: Sequence[str]) -> CommandResult:
        return CommandResult(tuple(argv), 0, stdout, "")

    return run


def test_parses_first_gpu_line() -> None:
    stats = gpu.gpu_stats(runner=_ok("17, 2048, 8192, 54\n"))
    assert stats is not None
    assert stats.util_pct == 17
    assert stats.vram_used_mb == 2048
    assert stats.vram_total_mb == 8192
    assert stats.temp_c == 54


def test_uses_first_of_multiple_gpus() -> None:
    stats = gpu.gpu_stats(runner=_ok("10, 100, 200, 40\n90, 7000, 8000, 80\n"))
    assert stats is not None
    assert stats.util_pct == 10
    assert stats.vram_total_mb == 200


def test_none_on_command_failure() -> None:
    def run(argv: Sequence[str]) -> CommandResult:
        return CommandResult(tuple(argv), 127, "", "command not found: nvidia-smi")

    assert gpu.gpu_stats(runner=run) is None


def test_none_on_empty_output() -> None:
    assert gpu.gpu_stats(runner=_ok("\n")) is None


def test_none_on_unparseable_output() -> None:
    assert gpu.gpu_stats(runner=_ok("[N/A], -, -, -\n")) is None


def test_none_on_too_few_fields() -> None:
    assert gpu.gpu_stats(runner=_ok("10, 20\n")) is None
