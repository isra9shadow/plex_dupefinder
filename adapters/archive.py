"""Archive extraction via ``unar`` (handles zip / rar / 7z, incl. multi-volume).

``unar`` (The Unarchiver — Debian ``unar`` package, in *main*) auto-detects the
format and pulls sibling volumes automatically when given the FIRST volume of a
set, so the caller only needs to hand it the first volume. The subprocess goes
through the sanctioned ``adapters.command`` boundary (the only place allowed to
call ``subprocess``). It never raises — returns True on success, False otherwise
— so a broken/corrupt archive can never trigger a delete of the source.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path

from adapters import command
from adapters.command import CommandResult

Runner = Callable[[Sequence[str]], CommandResult]

# Extraction of a big multi-GB rar set can take a while; give it room.
_EXTRACT_TIMEOUT = 900.0


def _default_runner(argv: Sequence[str]) -> CommandResult:  # pragma: no cover - thin wrapper
    return command.run(argv, timeout=_EXTRACT_TIMEOUT)


def extract(archive: Path, dest_dir: Path, *, runner: Runner = _default_runner) -> bool:
    """Extract ``archive`` (the first volume of a set) into ``dest_dir``.

    Flags: ``-q`` quiet, ``-f`` force-overwrite (re-runs are idempotent), ``-D``
    no enclosing folder (extract straight into ``dest_dir``), ``-o`` output dir.
    Returns True only on a clean exit (code 0) — the caller relies on this to
    decide whether it is safe to quarantine the source archives.
    """
    argv = ["unar", "-q", "-f", "-D", "-o", str(dest_dir), str(archive)]
    try:
        return runner(argv).ok
    except Exception:  # pragma: no cover - defensive; command.run already swallows
        return False
