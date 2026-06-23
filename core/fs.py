"""The ONLY module permitted to move or delete user data (INVARIANT I1, ADR-0002).

Destruction is quarantine: items are MOVED to the quarantine dir with a restore
sidecar. The single real delete in the whole codebase is the audited retention
purge below, which respects DRY_RUN / simulate.
"""

from __future__ import annotations

import json
import shutil
import time
import uuid
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path

from core.config import Config
from core.errors import QuarantineError, SafetyError, ValidationError
from core.types import ActionRecord, PurgeResult, QuarantineEntry

_SIDECAR_SUFFIX = ".izumi.json"


def _ensure_within(base: Path, target: Path) -> None:
    """Guard against path traversal / tampered sidecars: ``target`` must resolve
    inside ``base``. Raises QuarantineError otherwise (CTO-16)."""
    try:
        target.resolve().relative_to(base.resolve())
    except ValueError as exc:
        raise QuarantineError(f"path {target} escapes quarantine dir {base}") from exc


def _within_any(target: Path, roots: Sequence[Path]) -> bool:
    """True iff ``target`` resolves inside at least one of the resolved ``roots``
    (containment guard for relocate, mirroring ``_ensure_within``)."""
    resolved = target.resolve()
    for root in roots:
        try:
            resolved.relative_to(root.resolve())
        except ValueError:
            continue
        return True
    return False


def _dir_size(path: Path) -> int:
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def _item_size(path: Path) -> int:
    if path.is_dir():
        return _dir_size(path)
    try:
        return path.stat().st_size
    except OSError:  # pragma: no cover - defensive
        return 0


def _read_ts(sidecar: Path) -> float:
    try:
        data: object = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):  # pragma: no cover - defensive
        return sidecar.stat().st_mtime
    if isinstance(data, dict):
        ts = data.get("ts")
        if isinstance(ts, int | float) and not isinstance(ts, bool):
            return float(ts)
    return sidecar.stat().st_mtime  # pragma: no cover - sidecar always has ts


def _remove(path: Path) -> None:
    """The only real deletion in the codebase (called solely by Fs.purge)."""
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
    else:
        path.unlink(missing_ok=True)


class Fs:
    """Filesystem operations. Construct with the run's dry_run flag."""

    def __init__(self, config: Config, *, dry_run: bool) -> None:
        self._cfg = config.safety
        self._dry_run = dry_run

    @property
    def dry_run(self) -> bool:
        return self._dry_run

    @property
    def quarantine_dir(self) -> Path:
        return self._cfg.quarantine_dir

    def quarantine(self, path: Path, *, reason: str) -> QuarantineEntry:
        """Move ``path`` to quarantine (never delete) and write a restore sidecar."""
        if not path.exists():
            raise QuarantineError(f"path does not exist: {path}")
        ts = time.time()
        # Short uuid suffix guarantees uniqueness even for same-named files moved
        # in the same millisecond (CTO-A11). path.name strips any directory.
        dest = self._cfg.quarantine_dir / f"{path.name}.{int(ts * 1000)}.{uuid.uuid4().hex[:8]}"
        _ensure_within(self._cfg.quarantine_dir, dest)
        sidecar = dest.with_name(f"{dest.name}{_SIDECAR_SUFFIX}")
        entry = QuarantineEntry(
            original_path=str(path),
            quarantine_path=str(dest),
            sidecar_path=str(sidecar),
            reason=reason,
            restore_command=f'mv -- "{dest}" "{path}"',
            ts=ts,
        )
        if self._dry_run:
            return entry
        self._cfg.quarantine_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), str(dest))
        sidecar.write_text(json.dumps(asdict(entry), indent=2), encoding="utf-8")
        return entry

    def relocate(
        self,
        src: Path,
        dest: Path,
        *,
        reason: str,
        allowed_roots: Sequence[Path] | None = None,
    ) -> ActionRecord:
        """Move a kept file to a new in-library path (reorganize), never clobbering.

        The second sanctioned mover besides ``quarantine`` (INVARIANT I1): it
        restructures a correctly-identified media file into its canonical
        Movies/Series path. It refuses to overwrite an existing ``dest``
        (``SafetyError`` — never destroy user data), never deletes, and honours
        ``dry_run`` (returns the planned ``ActionRecord`` without touching disk).
        ``reason`` documents intent for the caller's audit log.

        When ``allowed_roots`` is provided (non-empty), the resolved ``dest`` must
        live inside at least one of the resolved roots, else ``SafetyError`` — this
        stops a misidentified/hallucinated target (``../../etc``-style) from
        escaping the intended media roots. When None/empty, behaviour is unchanged.
        """
        if not src.exists():
            raise ValidationError(f"relocate source missing: {src}")
        if src.resolve() == dest.resolve():
            raise ValidationError(f"relocate src and dest are identical: {src}")
        if allowed_roots and not _within_any(dest, allowed_roots):
            raise SafetyError(f"relocate dest escapes allowed roots: {dest}")
        if dest.exists():
            raise SafetyError(f"relocate would overwrite existing path: {dest}")
        record = ActionRecord(
            action="relocate", src=str(src), dest=str(dest), bytes=_item_size(src)
        )
        if self._dry_run:
            return record
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dest))
        return record

    def restore(self, entry: QuarantineEntry) -> Path:
        """Move a quarantined item back to its original path."""
        src = Path(entry.quarantine_path)
        dst = Path(entry.original_path)
        if self._dry_run:
            return dst
        # A tampered sidecar must not make us move an arbitrary system path: the
        # source has to live inside the quarantine dir (CTO-16).
        _ensure_within(self._cfg.quarantine_dir, src)
        if not src.exists():
            raise QuarantineError(f"quarantined item missing: {src}")
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        Path(entry.sidecar_path).unlink(missing_ok=True)
        return dst

    def purge(self, retention_days: float | None = None, *, simulate: bool = False) -> PurgeResult:
        """Delete quarantined items older than retention. The only real delete."""
        days = self._cfg.retention_days if retention_days is None else retention_days
        simulated = simulate or self._dry_run
        dest_dir = self._cfg.quarantine_dir
        if not dest_dir.exists():
            return PurgeResult(0, 0, simulated)
        cutoff = time.time() - days * 86400.0
        deleted = 0
        reclaimed = 0
        for sidecar in sorted(dest_dir.glob(f"*{_SIDECAR_SUFFIX}")):
            if days > 0 and _read_ts(sidecar) > cutoff:
                continue
            item = sidecar.with_name(sidecar.name[: -len(_SIDECAR_SUFFIX)])
            reclaimed += _item_size(item)
            if not simulated:
                _remove(item)
                sidecar.unlink(missing_ok=True)
            deleted += 1
        return PurgeResult(deleted, reclaimed, simulated)
