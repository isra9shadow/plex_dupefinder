"""SQLite corruption EXECUTOR — operator-confirmed, SAFE auto-repair.

Companion to the read-only :mod:`modules.ops.dbcheck` detector. Where ``dbcheck``
only *reports* corruption, this module *repairs* it — but only under an explicit
operator confirmation (menu ``y/N`` / Telegram ``/dbrepair``) and along a strictly
safe, idempotent, fully-audited path. See ``docs/design/db-repair-and-tdarr-plex.md``
(DESIGN 1) for the full rationale.

Hard safety rules (mirrors the design):

  * INVARIANT I1 — this module NEVER raw-deletes. The corrupt DB is *moved* to
    quarantine via :class:`core.fs.Fs` (which itself only moves) BEFORE anything is
    touched; the repaired file is *moved* into place; every rollback is a move.
    No raw file-removal call and no ``subprocess`` import live in this file (the
    security suite enforces both).
  * DRY_RUN default — a new module is safe by default. In DRY_RUN (``ctx.mode`` !=
    ``LIVE``) it only PLANS: it read-only integrity-checks each DB and lists the
    steps it *would* take, never stopping a container or moving a file.
  * Container stop/start reuse the existing apply allow-list
    (:func:`aictx.apply.classify` + :func:`aictx.apply.apply_action`,
    category ``docker-lifecycle``) — no new privileged command path.
  * Secrets only via ``core/secrets`` (none needed here).

Safe step sequence per corrupt database ``(app, container, db_path)``:

  1. Pre-flight integrity_check (read-only). If already ``ok`` → nothing to do
     (idempotent: re-running after a successful repair is a no-op).
  2. Snapshot the live DB (+ ``-wal`` / ``-shm`` siblings) to quarantine — ALWAYS,
     before touching anything. The restore command lives in the sidecar.
  3. Stop the container (allow-list ``docker stop``); verify it is not running.
  4. Repair, first strategy that verifies ``ok`` wins:
       (a) ``native_backup`` — newest file in the app's own backup glob (a ``.zip``
           is unpacked via ``adapters.archive``); integrity-checked, then moved in.
       (b) ``recover`` — rebuild from the quarantined corrupt copy with SQLite's
           in-process dump/restore (no shell, no pipe); integrity-checked, moved in.
  5. Start the container (allow-list ``docker start``); verify it is running.
  6. Post-repair integrity_check must be ``ok`` — otherwise ROLLBACK.
  7. On any failure after the snapshot: ROLLBACK (restore the original DB, restart
     the container) and record a FailureRecord — we always end where we started.

Config (config.json), under ``integrations.dbrepair``::

    integrations.dbrepair :
      verify_timeout_s      : per-op timeout hint (default 30)
      databases             : list of {app, container, db_path, native_backup_glob}
      repair_strategy_order : subset/order of ["native_backup", "recover"]
      target_fingerprint    : optional — restrict the run to one confirmed finding
                              (the front-end sets it for the operator-chosen item)

Metrics: ``corrupt_count`` (found), ``repaired_count`` (verified ``ok`` after repair).
"""

from __future__ import annotations

import glob as _glob
import json
import sqlite3
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from adapters import archive
from adapters import docker as docker_adapter
from adapters.docker import ContainerInfo
from aictx.apply import (
    ApplyAction,
    Runner,
    apply_action,
    classify,
    default_runner,
    finding_fingerprint,
)
from core.cache import SqliteCache
from core.fs import Fs
from core.registry import register
from core.types import FailureRecord, ModuleResult, QuarantineEntry, RunContext, SafetyMode

# Injected boundaries (real defaults; tests pass fakes so nothing touches docker /
# a real DB when they don't want it to).
IntegrityFn = Callable[[str], list[str]]
RecoverFn = Callable[[str, str], bool]
ExtractFn = Callable[[Path, Path], bool]
ListContainersFn = Callable[[], list[ContainerInfo]]

_DEFAULT_STRATEGY_ORDER = ("native_backup", "recover")
_DEFAULT_VERIFY_TIMEOUT = 30.0


@dataclass(frozen=True)
class DbTarget:
    """One repairable database as configured under ``integrations.dbrepair``."""

    app: str
    container: str
    db_path: str
    native_backup_glob: str

    @property
    def title(self) -> str:
        """Stable finding title (numbers masked by ``finding_fingerprint``)."""
        return f"DB corruption: {self.app}"

    @property
    def fingerprint(self) -> str:
        return finding_fingerprint(self.title)


@dataclass(frozen=True)
class _Settings:
    databases: list[DbTarget]
    strategy_order: tuple[str, ...]
    verify_timeout_s: float
    target_fingerprint: str | None


@dataclass
class RepairStep:
    """One audited step of a repair (name, whether it succeeded, human detail)."""

    name: str
    ok: bool
    detail: str = ""


@dataclass
class RepairOutcome:
    """The full audit trail of (attempting to) repair one database."""

    app: str
    db_path: str
    container: str
    status: str  # ok | already_ok | dry_run | failed | rolled_back | skipped
    strategy: str = ""
    steps: list[RepairStep] = field(default_factory=list)
    restore_command: str = ""
    detail: str = ""

    def add(self, name: str, ok: bool, detail: str = "") -> RepairStep:
        step = RepairStep(name=name, ok=ok, detail=detail)
        self.steps.append(step)
        return step

    @property
    def repaired(self) -> bool:
        return self.status == "ok"


# --- config --------------------------------------------------------------------


def _str(raw: object, default: str = "") -> str:
    return raw if isinstance(raw, str) and raw.strip() else default


def _targets(raw: object) -> list[DbTarget]:
    """Parse ``integrations.dbrepair.databases`` into typed targets.

    Entries missing an ``app``, ``container`` or ``db_path`` are skipped (never
    hardcode a user's appdata layout); ``native_backup_glob`` may be empty (then
    only the ``recover`` strategy applies).
    """
    if not isinstance(raw, list):
        return []
    out: list[DbTarget] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        app = _str(entry.get("app"))
        container = _str(entry.get("container"))
        db_path = _str(entry.get("db_path"))
        if not (app and container and db_path):
            continue
        out.append(
            DbTarget(
                app=app,
                container=container,
                db_path=db_path,
                native_backup_glob=_str(entry.get("native_backup_glob")),
            )
        )
    return out


def _settings(ctx: RunContext) -> _Settings:
    cfg = ctx.config.integrations.get("dbrepair", {})
    raw_order = cfg.get("repair_strategy_order")
    order = (
        tuple(s for s in raw_order if s in _DEFAULT_STRATEGY_ORDER)
        if isinstance(raw_order, list)
        else ()
    ) or _DEFAULT_STRATEGY_ORDER
    timeout = cfg.get("verify_timeout_s")
    return _Settings(
        databases=_targets(cfg.get("databases")),
        strategy_order=order,
        verify_timeout_s=(
            float(timeout)
            if isinstance(timeout, int | float) and not isinstance(timeout, bool) and timeout > 0
            else _DEFAULT_VERIFY_TIMEOUT
        ),
        target_fingerprint=_str(cfg.get("target_fingerprint")) or None,
    )


# --- SQLite integrity + recover (pure, no shell) -------------------------------


def integrity_check(path: str) -> list[str]:
    """Run ``PRAGMA integrity_check`` on ``path`` opened strictly read-only.

    ``mode=ro&immutable=1`` guarantees no write / journal / lock on the live DB.
    Returns the pragma rows (``["ok"]`` when healthy). Raises ``sqlite3.Error`` /
    ``OSError`` on a missing file / non-database — the caller maps that to corrupt.
    """
    uri = f"file:{Path(path).as_posix()}?mode=ro&immutable=1"
    conn = sqlite3.connect(uri, uri=True, timeout=0)
    try:
        rows = conn.execute("PRAGMA integrity_check").fetchall()
    finally:
        conn.close()
    return [str(row[0]) for row in rows] if rows else []


def is_healthy(rows: list[str]) -> bool:
    """Healthy iff the integrity check produced the single row ``ok``."""
    cleaned = [r.strip() for r in rows if r.strip()]
    return cleaned == ["ok"]


def _safe_integrity(path: str, checker: IntegrityFn) -> tuple[bool, str]:
    """Integrity check that never raises: any error means 'not healthy'."""
    if not Path(path).is_file():
        return False, "file not found"
    try:
        rows = checker(path)
    except (sqlite3.Error, OSError) as exc:
        return False, f"not a database / unreadable: {exc}"
    ok = is_healthy(rows)
    return ok, "ok" if ok else "; ".join(r.strip() for r in rows if r.strip()) or "corrupt"


def recover_db(src: str, dst: str) -> bool:
    """Rebuild ``src`` into a fresh DB at ``dst`` using SQLite's in-process dump.

    Opens the (corrupt) source read-only and replays ``iterdump()`` into a brand
    new database via ``executescript`` — the in-process equivalent of the CLI
    ``.dump``/``.recover`` pipe, but with NO shell, NO pipe and NO metacharacters
    (INVARIANT-friendly). Returns True only if the dump replays cleanly; the caller
    still integrity-checks ``dst`` before trusting it.
    """
    src_uri = f"file:{Path(src).as_posix()}?mode=ro&immutable=1"
    try:
        src_conn = sqlite3.connect(src_uri, uri=True, timeout=0)
    except sqlite3.Error:
        return False
    try:
        script = "\n".join(src_conn.iterdump())
    except sqlite3.Error:
        return False
    finally:
        src_conn.close()
    try:
        dst_conn = sqlite3.connect(dst)
        try:
            dst_conn.executescript(script)
            dst_conn.commit()
        finally:
            dst_conn.close()
    except sqlite3.Error:
        return False
    return True


def _newest(pattern: str) -> Path | None:
    """Newest file matching ``pattern`` (empty pattern / no match → None)."""
    if not pattern:
        return None
    matches = [Path(p) for p in _glob.glob(pattern)]
    files = [p for p in matches if p.is_file()]
    if not files:
        return None
    return max(files, key=lambda p: p.stat().st_mtime)


# --- container lifecycle (through the apply allow-list) ------------------------


def _container_state(name: str, list_fn: ListContainersFn) -> str:
    """Current docker state of ``name`` ("running" / "exited" / … / "absent")."""
    try:
        for info in list_fn():
            if info.name == name:
                return info.state or "unknown"
    except Exception:  # pragma: no cover - defensive; list_containers already safe
        return "unknown"
    return "absent"


def _docker_lifecycle(command: str, runner: Runner) -> tuple[bool, str]:
    """Run an allow-listed ``docker stop|start <name>`` via the apply layer.

    Re-classifies (defense in depth) then executes through :func:`apply_action`,
    so this reuses the exact audited boundary the operator-confirmed apply uses.
    """
    verdict = classify(command)
    if not verdict.allowed:
        return False, verdict.reason
    action = ApplyAction(
        command=command,
        category=verdict.category,
        finding_title="dbrepair",
        fingerprint="",
        severity="info",
    )
    outcome = apply_action(action, runner=runner)
    return outcome.ok, (outcome.output or outcome.error).strip()


# --- repair strategies ---------------------------------------------------------


def _resolve_backup_db(
    backup: Path, app: str, work_dir: Path, extract_fn: ExtractFn
) -> Path | None:
    """Resolve the app DB file inside a native backup (a ``.db`` or a ``.zip``)."""
    if backup.suffix.lower() == ".zip":
        dest = work_dir / "backup_unpacked"
        dest.mkdir(parents=True, exist_ok=True)
        if not extract_fn(backup, dest):
            return None
        # Prefer a DB named after the app, else the first *.db in the archive.
        named = sorted(dest.rglob(f"{app}.db"))
        if named:
            return named[0]
        any_db = sorted(dest.rglob("*.db"))
        return any_db[0] if any_db else None
    if backup.suffix.lower() == ".db":
        return backup
    return None


def _try_native_backup(
    target: DbTarget,
    work_dir: Path,
    checker: IntegrityFn,
    extract_fn: ExtractFn,
) -> tuple[Path | None, str]:
    """Return a verified-healthy backup DB to move into place (or None + reason)."""
    backup = _newest(target.native_backup_glob)
    if backup is None:
        return None, "no native backup found"
    candidate = _resolve_backup_db(backup, target.app, work_dir, extract_fn)
    if candidate is None:
        return None, f"could not extract a DB from backup {backup.name}"
    ok, detail = _safe_integrity(str(candidate), checker)
    if not ok:
        return None, f"backup {backup.name} also fails integrity: {detail}"
    return candidate, f"native backup {backup.name}"


def _try_recover(
    corrupt_copy: str,
    work_dir: Path,
    checker: IntegrityFn,
    recover_fn: RecoverFn,
) -> tuple[Path | None, str]:
    """Return a verified-healthy recovered DB to move into place (or None + reason)."""
    fresh = work_dir / "recovered.db"
    if not recover_fn(corrupt_copy, str(fresh)):
        return None, "recover dump failed"
    ok, detail = _safe_integrity(str(fresh), checker)
    if not ok:
        return None, f"recovered DB still fails integrity: {detail}"
    return fresh, "recover rebuild"


# --- orchestration -------------------------------------------------------------


def _snapshot(fs: Fs, db_path: str) -> list[QuarantineEntry]:
    """Quarantine the DB and any ``-wal`` / ``-shm`` siblings; return the entries.

    The FIRST entry is always the main DB (used to restore the original on rollback).
    """
    entries = [fs.quarantine(Path(db_path), reason="dbrepair: pre-repair snapshot")]
    for suffix in ("-wal", "-shm"):
        sibling = Path(db_path + suffix)
        if sibling.exists():
            entries.append(fs.quarantine(sibling, reason=f"dbrepair: snapshot {suffix} sibling"))
    return entries


def _rollback(fs: Fs, entries: list[QuarantineEntry], placed: Path | None, db_path: str) -> None:
    """Undo a failed repair: quarantine any placed file, restore the originals.

    Every move goes through ``core/fs`` (never a delete). After this the live DB is
    exactly the original corrupt file again — no data invented, none lost.
    """
    if placed is not None and Path(db_path).exists():
        fs.quarantine(Path(db_path), reason="dbrepair-failed: discard un-verified repair")
    for entry in entries:
        fs.restore(entry)


def _plan_dry(target: DbTarget, detail: str, settings: _Settings) -> RepairOutcome:
    """Build the DRY-RUN plan for a corrupt DB (no side effects whatsoever)."""
    outcome = RepairOutcome(
        app=target.app,
        db_path=target.db_path,
        container=target.container,
        status="dry_run",
        detail=detail,
    )
    outcome.add("preflight", False, f"corrupt: {detail}")
    outcome.add("snapshot", True, "would quarantine the DB (+wal/-shm) first")
    outcome.add("stop_container", True, f"would run: docker stop {target.container}")
    backup = _newest(target.native_backup_glob)
    strat = ", ".join(settings.strategy_order)
    outcome.add(
        "repair",
        True,
        f"would try [{strat}]"
        + (f"; newest backup: {backup.name}" if backup else "; no native backup present"),
    )
    outcome.add("start_container", True, f"would run: docker start {target.container}")
    outcome.add("verify", True, "would re-run integrity_check and rollback if not ok")
    return outcome


def repair_target(
    target: DbTarget,
    *,
    fs: Fs,
    settings: _Settings,
    runner: Runner,
    checker: IntegrityFn,
    recover_fn: RecoverFn,
    extract_fn: ExtractFn,
    list_fn: ListContainersFn,
) -> RepairOutcome:
    """Run the full safe repair sequence for one already-corrupt target (LIVE)."""
    outcome = RepairOutcome(
        app=target.app,
        db_path=target.db_path,
        container=target.container,
        status="failed",
    )

    # 2) Snapshot BEFORE touching anything.
    entries = _snapshot(fs, target.db_path)
    main_entry = entries[0]
    outcome.restore_command = main_entry.restore_command
    corrupt_copy = main_entry.quarantine_path
    outcome.add("snapshot", True, f"quarantined to {corrupt_copy}")

    # 3) Stop the container.
    stop_ok, stop_detail = _docker_lifecycle(f"docker stop {target.container}", runner)
    if not stop_ok or _container_state(target.container, list_fn) == "running":
        outcome.add("stop_container", False, stop_detail or "container still running")
        _rollback(fs, entries, None, target.db_path)
        outcome.status = "rolled_back"
        outcome.detail = "could not stop container; original restored"
        return outcome
    outcome.add("stop_container", True, stop_detail or "stopped")

    # 4) Repair — first strategy that verifies healthy wins.
    with tempfile.TemporaryDirectory(prefix="dbrepair_") as tmp:
        work_dir = Path(tmp)
        placed: Path | None = None
        reasons: list[str] = []
        for strategy in settings.strategy_order:
            if strategy == "native_backup":
                candidate, reason = _try_native_backup(target, work_dir, checker, extract_fn)
            elif strategy == "recover":
                candidate, reason = _try_recover(corrupt_copy, work_dir, checker, recover_fn)
            else:  # pragma: no cover - order is filtered in _settings
                continue
            reasons.append(f"{strategy}: {reason}")
            if candidate is not None:
                fs.relocate(candidate, Path(target.db_path), reason=f"dbrepair: {reason}")
                placed = Path(target.db_path)
                outcome.strategy = strategy
                outcome.add("repair", True, reason)
                break

        if placed is None:
            outcome.add("repair", False, " | ".join(reasons) or "no strategy available")
            _rollback(fs, entries, None, target.db_path)
            _docker_lifecycle(f"docker start {target.container}", runner)
            outcome.status = "rolled_back"
            outcome.detail = "all repair strategies failed; original restored, container restarted"
            return outcome

        # 5) Start the container back up.
        start_ok, start_detail = _docker_lifecycle(f"docker start {target.container}", runner)
        outcome.add(
            "start_container", start_ok, start_detail or ("started" if start_ok else "failed")
        )

        # 6) Verify the now-live DB.
        ok, detail = _safe_integrity(target.db_path, checker)
        outcome.add("verify", ok, detail)
        if not ok:
            _rollback(fs, entries, placed, target.db_path)
            _docker_lifecycle(f"docker start {target.container}", runner)
            outcome.status = "rolled_back"
            outcome.detail = f"post-repair integrity not ok ({detail}); original restored"
            return outcome

    outcome.status = "ok"
    outcome.detail = f"repaired via {outcome.strategy}"
    return outcome


def _select(settings: _Settings) -> list[DbTarget]:
    """Targets to consider, honouring an optional confirmed ``target_fingerprint``."""
    if settings.target_fingerprint:
        return [t for t in settings.databases if t.fingerprint == settings.target_fingerprint]
    return settings.databases


def _resolve_incident(ctx: RunContext, fingerprint: str, command: str) -> None:
    """Best-effort: mark the DB-corruption incident resolved so it stops re-flagging."""
    try:
        cache = SqliteCache(ctx.config.reporting.dir / "cache" / "incidents.db")
        try:
            cache.resolve_incident(fingerprint, applied=[command])
            cache.save()
        finally:
            cache.close()
    except sqlite3.Error:  # pragma: no cover - the memory loop is best-effort
        pass


def _write_report(ctx: RunContext, outcomes: list[RepairOutcome], dry_run: bool, note: str) -> None:
    out_dir = ctx.config.reporting.dir / "dbrepair"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "plan.json").write_text(
        json.dumps(
            {
                "dry_run": dry_run,
                "note": note,
                "results": [
                    {
                        "app": o.app,
                        "db_path": o.db_path,
                        "container": o.container,
                        "status": o.status,
                        "strategy": o.strategy,
                        "restore_command": o.restore_command,
                        "detail": o.detail,
                        "steps": [
                            {"name": s.name, "ok": s.ok, "detail": s.detail} for s in o.steps
                        ],
                    }
                    for o in outcomes
                ],
            },
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    lines = [
        "# Dbrepair — reparación segura de DB SQLite corrupta",
        "",
        f"Modo: {'DRY-RUN (solo plan)' if dry_run else 'LIVE'}",
        f"Bases procesadas: {len(outcomes)}",
        "",
        "## Resultados",
    ]
    if outcomes:
        for o in outcomes:
            lines.append(f"- [{o.status}] {o.app} ({o.db_path}) — {o.detail or o.strategy}")
            for s in o.steps:
                mark = "OK" if s.ok else "FALLO"
                lines.append(f"  - [{mark}] {s.name}: {s.detail}")
            if o.restore_command and not dry_run:
                lines.append(f"  - restore: `{o.restore_command}`")
    else:
        lines.append("(sin bases corruptas — nada que reparar)")
    if note:
        lines += ["", f"> {note}"]
    (out_dir / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


@register("dbrepair")
def run(
    ctx: RunContext,
    *,
    runner: Runner = default_runner,
    checker: IntegrityFn = integrity_check,
    recover_fn: RecoverFn = recover_db,
    extract_fn: ExtractFn = archive.extract,
    list_fn: ListContainersFn = docker_adapter.list_containers,
) -> ModuleResult:
    """Detect and (in LIVE) repair corrupt SQLite databases, safely and audited.

    All external boundaries (docker command runner, integrity checker, recover,
    zip extraction, container listing) are injected with real defaults so tests run
    fully offline. DRY_RUN (default) plans only — it never stops a container or
    moves a file.
    """
    result = ModuleResult(module="dbrepair", run_id=ctx.run_id, mode=ctx.mode)
    settings = _settings(ctx)
    dry_run = ctx.mode != SafetyMode.LIVE

    targets = _select(settings)
    outcomes: list[RepairOutcome] = []
    note = ""
    if not settings.databases:
        note = (
            "No databases configured — set integrations.dbrepair.databases to a list of "
            "{app, container, db_path, native_backup_glob}."
        )
    elif not targets:
        note = "target_fingerprint set but no configured database matches it."

    corrupt = 0
    repaired = 0
    for target in targets:
        ok, detail = _safe_integrity(target.db_path, checker)
        if ok:
            outcomes.append(
                RepairOutcome(
                    app=target.app,
                    db_path=target.db_path,
                    container=target.container,
                    status="already_ok",
                    detail="integrity ok — nothing to do",
                )
            )
            continue

        corrupt += 1
        if dry_run:
            outcomes.append(_plan_dry(target, detail, settings))
            continue

        outcome = repair_target(
            target,
            fs=ctx.fs,
            settings=settings,
            runner=runner,
            checker=checker,
            recover_fn=recover_fn,
            extract_fn=extract_fn,
            list_fn=list_fn,
        )
        outcomes.append(outcome)
        if outcome.repaired:
            repaired += 1
            _resolve_incident(ctx, target.fingerprint, f"dbrepair {target.app}")
        else:
            result.add_failure(
                FailureRecord(
                    category="integration",
                    message=f"{target.app}: {outcome.detail or outcome.status}",
                    src=target.db_path,
                )
            )

    _write_report(ctx, outcomes, dry_run, note)
    ctx.logger.info(
        "dbrepair done",
        databases=len(targets),
        corrupt=corrupt,
        repaired=repaired,
        dry_run=dry_run,
    )
    result.metrics["corrupt_count"] = float(corrupt)
    result.metrics["repaired_count"] = float(repaired)
    result.actions = repaired  # real, verified repairs (0 in dry-run)
    return result
