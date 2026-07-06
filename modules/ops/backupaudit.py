"""Backup & image hygiene — verify recent backups exist and flag local images.

Two read-only checks (INVARIANT I1 — moves/deletes nothing; the only output is its
own report):

  1. **Backup freshness.** For each configured backup set (a glob such as
     ``…/Backups/scheduled/*.zip``) it finds the newest file and flags it ``missing``
     (no file at all) or ``stale`` (newest file older than ``max_age_days``). This
     is the safety net behind :mod:`modules.ops.dbrepair`'s ``native_backup`` repair
     strategy: if the newest backup is old/absent, a repair would fall back to the
     riskier ``recover`` rebuild — so surface it early.

  2. **Local-image containers.** Containers whose image matches a configured marker
     (e.g. ``dyalf/``) are locally-built tags Watchtower cannot pull — it errors
     trying. These are flagged so the operator can exclude them from Watchtower
     (a manual compose label; no auto-fix here).

Config (config.json):
  integrations.backupaudit :
    backups : list of {name, glob, max_age_days (default 2)}
    local_image_markers : list of substrings that mark a locally-built image
                          (checked against each container's image ref)

Metrics: ``backups_checked``, ``backups_bad`` (missing+stale), ``local_images``.
"""

from __future__ import annotations

import glob as _glob
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from adapters import docker as docker_adapter
from adapters.docker import ContainerInfo
from core.registry import register
from core.types import FailureRecord, ModuleResult, RunContext

ListContainersFn = Callable[[], list[ContainerInfo]]
ClockFn = Callable[[], float]

_DEFAULT_MAX_AGE_DAYS = 2.0


@dataclass(frozen=True)
class BackupRule:
    name: str
    glob: str
    max_age_days: float


@dataclass(frozen=True)
class BackupFinding:
    name: str
    glob: str
    status: str  # ok | missing | stale
    newest: str  # newest file path, or "" when missing
    age_days: float  # age of the newest file in days (-1 when missing)
    detail: str = ""


@dataclass(frozen=True)
class ImageFinding:
    container: str
    image: str


def _str(raw: object) -> str:
    return raw.strip() if isinstance(raw, str) and raw.strip() else ""


def _str_list(raw: object) -> list[str]:
    if not isinstance(raw, list):
        return []
    return [s.strip() for s in raw if isinstance(s, str) and s.strip()]


def _pos_float(raw: object, default: float) -> float:
    return (
        float(raw)
        if isinstance(raw, int | float) and not isinstance(raw, bool) and raw > 0
        else default
    )


def _backup_rules(ctx: RunContext) -> list[BackupRule]:
    cfg = ctx.config.integrations.get("backupaudit", {})
    raw = cfg.get("backups")
    if not isinstance(raw, list):
        return []
    out: list[BackupRule] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        glob_pat = _str(entry.get("glob"))
        if not glob_pat:
            continue
        name = _str(entry.get("name")) or Path(glob_pat).parent.name or glob_pat
        out.append(
            BackupRule(
                name=name,
                glob=glob_pat,
                max_age_days=_pos_float(entry.get("max_age_days"), _DEFAULT_MAX_AGE_DAYS),
            )
        )
    return out


def _newest(pattern: str) -> Path | None:
    files = [p for p in (Path(x) for x in _glob.glob(pattern)) if p.is_file()]
    return max(files, key=lambda p: p.stat().st_mtime) if files else None


def evaluate_backup(rule: BackupRule, *, now: float) -> BackupFinding:
    """Classify a backup set as ok / missing / stale (pure w.r.t. the clock)."""
    newest = _newest(rule.glob)
    if newest is None:
        return BackupFinding(rule.name, rule.glob, "missing", "", -1.0, "no backup file found")
    age_days = (now - newest.stat().st_mtime) / 86400.0
    if age_days > rule.max_age_days:
        detail = f"newest backup is {age_days:.1f}d old (max {rule.max_age_days:.0f}d)"
        return BackupFinding(rule.name, rule.glob, "stale", str(newest), age_days, detail)
    return BackupFinding(rule.name, rule.glob, "ok", str(newest), age_days)


def find_local_images(containers: list[ContainerInfo], markers: list[str]) -> list[ImageFinding]:
    """Containers whose image contains any marker substring (locally-built tags)."""
    if not markers:
        return []
    out: list[ImageFinding] = []
    for info in containers:
        image = info.image or ""
        if any(marker in image for marker in markers):
            out.append(ImageFinding(container=info.name, image=image))
    return out


def _write_report(
    ctx: RunContext,
    backups: list[BackupFinding],
    images: list[ImageFinding],
    note: str,
) -> None:
    out_dir = ctx.config.reporting.dir / "backupaudit"
    out_dir.mkdir(parents=True, exist_ok=True)
    bad = [b for b in backups if b.status != "ok"]
    (out_dir / "plan.json").write_text(
        json.dumps(
            {
                "backups_checked": len(backups),
                "backups_bad": len(bad),
                "local_images": len(images),
                "note": note,
                "backups": [
                    {
                        "name": b.name,
                        "glob": b.glob,
                        "status": b.status,
                        "newest": b.newest,
                        "age_days": round(b.age_days, 2),
                        "detail": b.detail,
                    }
                    for b in backups
                ],
                "images": [{"container": i.container, "image": i.image} for i in images],
            },
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    lines = [
        "# Backupaudit — frescura de backups + imágenes locales (solo lectura)",
        "",
        f"Backups comprobados: {len(backups)} (con problema: {len(bad)})",
        f"Contenedores con imagen local: {len(images)}",
        "",
        "## Backups",
    ]
    for b in backups:
        suffix = f" — {b.detail}" if b.detail else ""
        lines.append(f"- [{b.status}] {b.name}: {b.newest or b.glob}{suffix}")
    if not backups:
        lines.append("(sin backups configurados — integrations.backupaudit.backups)")
    if images:
        lines += ["", "## Imágenes locales (excluir de Watchtower)"]
        lines += [f"- {i.container}: {i.image}" for i in images]
    if note:
        lines += ["", f"> {note}"]
    (out_dir / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


@register("backupaudit")
def run(
    ctx: RunContext,
    *,
    now: ClockFn = time.time,
    list_fn: ListContainersFn = docker_adapter.list_containers,
) -> ModuleResult:
    """Verify configured backups are fresh and flag local-image containers.

    ``now`` and ``list_fn`` are injected so tests run offline/deterministically.
    Strictly read-only: a stale/missing backup is a FailureRecord (surfaced by the
    health sweep / digest), never an exception.
    """
    result = ModuleResult(module="backupaudit", run_id=ctx.run_id, mode=ctx.mode)
    cfg = ctx.config.integrations.get("backupaudit", {})
    when = now()

    backups = [evaluate_backup(rule, now=when) for rule in _backup_rules(ctx)]
    for finding in backups:
        if finding.status != "ok":
            result.add_failure(
                FailureRecord(
                    category="integration",
                    message=f"backup {finding.name}: {finding.status} — {finding.detail}",
                    src=finding.glob,
                )
            )

    markers = _str_list(cfg.get("local_image_markers"))
    images: list[ImageFinding] = []
    if markers:
        try:
            images = find_local_images(list_fn(), markers)
        except Exception:  # pragma: no cover - list_containers already fails soft
            images = []

    note = ""
    if not backups:
        note = (
            "No backups configured — set integrations.backupaudit.backups to a list of "
            "{name, glob, max_age_days}."
        )
    _write_report(ctx, backups, images, note)
    bad = sum(1 for b in backups if b.status != "ok")
    ctx.logger.info(
        "backupaudit done",
        backups=len(backups),
        bad=bad,
        local_images=len(images),
    )
    result.metrics["backups_checked"] = float(len(backups))
    result.metrics["backups_bad"] = float(bad)
    result.metrics["local_images"] = float(len(images))
    result.actions = 0  # read-only
    return result
