"""The single audited boundary for running host commands (read-only callers).

Always list-args (never a shell), always with a timeout, never raises on a
non-zero exit — callers inspect ``CommandResult.ok``. This is the only place in
the codebase allowed to call ``subprocess`` (enforced by a security test).
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

_NOT_FOUND = 127
_TIMEOUT = 124


@dataclass(frozen=True)
class CommandResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def run(
    argv: Sequence[str], *, timeout: float = 30.0, env: Mapping[str, str] | None = None
) -> CommandResult:
    """Run ``argv`` read-only with a timeout. Never raises; check ``.ok``.

    ``env`` is merged over the inherited environment (does not replace it), so
    callers can pass a few extra variables without losing PATH etc.
    """
    args = list(argv)
    proc_env = {**os.environ, **env} if env else None
    try:
        proc = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            stdin=subprocess.DEVNULL,
            env=proc_env,
        )
    except FileNotFoundError:
        return CommandResult(tuple(args), _NOT_FOUND, "", f"command not found: {args[0]}")
    except subprocess.TimeoutExpired:
        return CommandResult(tuple(args), _TIMEOUT, "", f"timeout after {timeout}s")
    return CommandResult(tuple(args), proc.returncode, proc.stdout, proc.stderr)
