"""SQLite corruption DETECTOR — read-only health check of the databases that rot
under heavy media volume (Plex, Sonarr, Radarr, plus any extras the operator adds).

What it does:

  For every database configured under ``integrations.dbcheck.databases`` it opens
  the file READ-ONLY (``sqlite3`` with ``mode=ro`` over a ``file:`` URI) and runs
  ``PRAGMA quick_check`` — the fast integrity scan that surfaces page/structure
  corruption without the full ``integrity_check`` cost. A database whose check
  returns the single row ``ok`` is healthy; anything else is reported verbatim as
  the corruption detail. A missing file, a locked database, or a file that is not
  an SQLite database at all is recorded via ``result.add_failure`` and never aborts
  the run.

This module is strictly READ-ONLY (INVARIANT I1): it never moves, deletes, repairs,
or modifies any host file or database — repair is a separate, future feature. The
only thing it writes is its own report under ``reporting.dir / "dbcheck"``.

Config (config.json):
  integrations.dbcheck :
    databases : list of {"name": str, "path": str(absolute .db path)} (default: [])
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from core.registry import register
from core.types import FailureRecord, ModuleResult, RunContext

# A function that, given an absolute db path, returns the lines produced by
# ``PRAGMA quick_check`` (``["ok"]`` when healthy). Injected so tests can drive the
# logic without touching sqlite3 directly when they want to.
QuickCheckFn = Callable[[str], list[str]]


@dataclass(frozen=True)
class DbResult:
    """Outcome of checking one configured database."""

    name: str
    path: str
    ok: bool
    detail: str  # "ok" when healthy, otherwise the corruption / error message

    @property
    def corrupt(self) -> bool:
        return not self.ok


def _databases(ctx: RunContext) -> list[tuple[str, str]]:
    """Read ``integrations.dbcheck.databases`` into ``(name, path)`` pairs.

    Entries without a usable string ``path`` are skipped; ``name`` falls back to
    the file's basename. The default is an EMPTY list — never hardcode a user's
    appdata layout.
    """
    cfg = ctx.config.integrations.get("dbcheck", {})
    raw = cfg.get("databases")
    if not isinstance(raw, list):
        return []
    out: list[tuple[str, str]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        path = entry.get("path")
        if not isinstance(path, str) or not path:
            continue
        name = entry.get("name")
        label = name if isinstance(name, str) and name else Path(path).name
        out.append((label, path))
    return out


def quick_check(path: str) -> list[str]:
    """Run ``PRAGMA quick_check`` on ``path`` opened strictly read-only.

    Returns the rows the pragma produced (``["ok"]`` for a healthy database). The
    ``immutable=1`` flag plus ``mode=ro`` guarantees no write, no journal, and no
    locking side effects on the live database. Raises ``sqlite3.Error`` /
    ``OSError`` on a missing file, a locked database, or a non-database file; the
    caller turns those into a FailureRecord.
    """
    # ``file:`` URI so we can pass ``mode=ro``; the path is quoted for spaces/specials.
    uri = f"file:{Path(path).as_posix()}?mode=ro&immutable=1"
    conn = sqlite3.connect(uri, uri=True, timeout=0)
    try:
        rows = conn.execute("PRAGMA quick_check").fetchall()
    finally:
        conn.close()
    return [str(row[0]) for row in rows] if rows else []


def evaluate(rows: list[str]) -> tuple[bool, str]:
    """Interpret ``PRAGMA quick_check`` output: healthy iff the only row is ``ok``."""
    cleaned = [r.strip() for r in rows if r.strip()]
    if cleaned == ["ok"]:
        return True, "ok"
    return False, "; ".join(cleaned) if cleaned else "empty quick_check result"


def check_database(name: str, path: str, checker: QuickCheckFn) -> DbResult:
    """Check one database, mapping any failure to a corrupt/unreadable result.

    Never raises — a missing file, locked database, or not-a-database file becomes
    a ``DbResult`` with ``ok=False`` and a descriptive detail so one bad DB cannot
    abort the run.
    """
    if not Path(path).is_file():
        return DbResult(name=name, path=path, ok=False, detail="file not found")
    try:
        rows = checker(path)
    except sqlite3.DatabaseError as exc:
        return DbResult(name=name, path=path, ok=False, detail=f"not a database / corrupt: {exc}")
    except sqlite3.OperationalError as exc:
        return DbResult(name=name, path=path, ok=False, detail=f"locked or unreadable: {exc}")
    except (sqlite3.Error, OSError) as exc:
        return DbResult(name=name, path=path, ok=False, detail=f"check failed: {exc}")
    ok, detail = evaluate(rows)
    return DbResult(name=name, path=path, ok=ok, detail=detail)


def _write_report(ctx: RunContext, results: list[DbResult], note: str) -> None:
    out_dir = ctx.config.reporting.dir / "dbcheck"
    out_dir.mkdir(parents=True, exist_ok=True)
    corrupt = [r for r in results if r.corrupt]
    (out_dir / "plan.json").write_text(
        json.dumps(
            {
                "databases_checked": len(results),
                "corrupt_count": len(corrupt),
                "note": note,
                "results": [
                    {"name": r.name, "path": r.path, "ok": r.ok, "detail": r.detail}
                    for r in results
                ],
            },
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    detail_lines: list[str] = []
    for r in sorted(results, key=lambda x: (x.ok, x.name)):
        marker = "OK" if r.ok else "CORRUPT"
        detail_lines.append(f"- [{marker}] {r.name} ({r.path})")
        if r.corrupt:
            detail_lines.append(f"  - {r.detail}")
    lines = [
        "# Dbcheck — SQLite corruption detector (solo lectura)",
        "",
        f"Databases checked: {len(results)}",
        f"Corrupt: {len(corrupt)}",
        "",
        "## Resultados",
        *(detail_lines or ["(no databases configured — integrations.dbcheck.databases)"]),
    ]
    if note:
        lines += ["", f"> {note}"]
    (out_dir / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


@register("dbcheck")
def run(ctx: RunContext) -> ModuleResult:
    """Detect SQLite corruption across the configured databases (read-only).

    The integrity checker is looked up as the module-level ``quick_check`` so it is
    trivially monkeypatched in tests; production always uses the real read-only
    ``PRAGMA quick_check``.
    """
    result = ModuleResult(module="dbcheck", run_id=ctx.run_id, mode=ctx.mode)
    databases = _databases(ctx)
    checker: QuickCheckFn = quick_check

    results: list[DbResult] = []
    note = ""
    if not databases:
        note = (
            "No databases configured — set integrations.dbcheck.databases to a list "
            "of {name, path} pointing at the .db files to monitor (Plex, Sonarr, Radarr…)."
        )
    for name, path in databases:
        db_result = check_database(name, path, checker)
        results.append(db_result)
        if db_result.corrupt:
            result.add_failure(
                FailureRecord(
                    category="integration",
                    message=f"{name}: {db_result.detail}",
                    src=path,
                )
            )

    corrupt_count = sum(1 for r in results if r.corrupt)
    _write_report(ctx, results, note)
    ctx.logger.info(
        "dbcheck done",
        databases=len(results),
        corrupt=corrupt_count,
    )
    result.metrics["databases_checked"] = float(len(results))
    result.metrics["corrupt_count"] = float(corrupt_count)
    result.actions = 0  # strictly read-only
    return result
