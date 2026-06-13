"""Tests for adapters.command (the single audited subprocess boundary)."""

from __future__ import annotations

import sys

from adapters import command


def test_run_ok() -> None:
    result = command.run([sys.executable, "-c", "print('hi')"])
    assert result.ok
    assert result.returncode == 0
    assert "hi" in result.stdout


def test_run_missing_command() -> None:
    result = command.run(["definitely_not_a_command_xyz_123"])
    assert not result.ok
    assert result.returncode == 127
    assert "not found" in result.stderr


def test_run_timeout() -> None:
    result = command.run([sys.executable, "-c", "import time; time.sleep(3)"], timeout=0.2)
    assert not result.ok
    assert result.returncode == 124


def test_run_nonzero_exit() -> None:
    result = command.run([sys.executable, "-c", "import sys; sys.exit(3)"])
    assert not result.ok
    assert result.returncode == 3


def test_run_passes_env_merged_over_inherited() -> None:
    result = command.run(
        [sys.executable, "-c", "import os; print(os.environ.get('IZUMI_X', ''))"],
        env={"IZUMI_X": "hello"},
    )
    assert result.ok
    assert "hello" in result.stdout
    # PATH (inherited) is still present — env is merged, not replaced.
    path_check = command.run(
        [sys.executable, "-c", "import os; print('PATH' in os.environ)"],
        env={"IZUMI_X": "hello"},
    )
    assert "True" in path_check.stdout
