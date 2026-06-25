"""Extract finished archives (zip / rar / 7z, incl. multi-volume) then move the
archive set to QUARANTINE — only when extraction succeeds.

Safe-by-default and INVARIANT-I1 compliant:

  * Extraction is delegated to ``unar`` via ``adapters/archive`` (the sanctioned
    subprocess boundary). The decoded files land next to the archive.
  * The source archives are NEVER ``rm``-ed: on a clean extraction the whole
    volume set is MOVED to quarantine via ``core/fs`` (recoverable, auto-purged
    by the retention job). If extraction fails, nothing is touched.
  * Incomplete downloads (``*.part`` — e.g. qBittorrent's in-progress files) are
    skipped: we only match real archive extensions, so a half-downloaded ``.rar``
    is left alone until it finishes.
  * Honours DRY_RUN: in a dry run nothing is extracted or moved; the plan still
    lists what *would* happen.

Config (config.json):
  paths.extract_source : directory to scan (defaults to paths.organizer_source)
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from adapters import archive
from core.registry import register
from core.types import FailureRecord, ModuleResult, RunContext

# Multi-volume naming schemes we understand. We hand ``unar`` only the FIRST
# volume of each set (it pulls the siblings itself) but track every volume so we
# can quarantine the whole set once extraction succeeds.
_PARTN_RE = re.compile(r"(?i)^(?P<base>.+)\.part(?P<n>\d+)\.rar$")  # name.part01.rar
_RNN_RE = re.compile(r"(?i)^(?P<base>.+)\.r(?P<n>\d{2,3})$")  # name.r00 / name.r01
_7Z_VOL_RE = re.compile(r"(?i)^(?P<base>.+)\.7z\.(?P<n>\d{3})$")  # name.7z.001


def _partnum(name: str) -> int:
    match = _PARTN_RE.match(name)
    return int(match.group("n")) if match else 0


def _siblings(files: list[Path], base: str, suffix_re: str) -> list[Path]:
    pattern = re.compile(re.escape(base) + suffix_re, re.IGNORECASE)
    return sorted((p for p in files if pattern.match(p.name)), key=lambda p: p.name.lower())


def archive_sets(files: list[Path]) -> list[tuple[Path, list[Path]]]:
    """Group archive files into ``(first_volume, [all_volumes])`` sets.

    Non-first volumes are folded into their set (not returned standalone), and
    plain ``*.part`` download fragments are ignored entirely (no archive ext).
    """
    ordered = sorted(files, key=lambda p: p.name.lower())
    used: set[str] = set()
    sets: list[tuple[Path, list[Path]]] = []
    for f in ordered:
        key = f.name.lower()
        if key in used:
            continue
        # Multi-volume RAR (new style): name.partNN.rar
        if _PARTN_RE.match(f.name):
            base = _PARTN_RE.match(f.name).group("base")  # type: ignore[union-attr]
            vols = _siblings(ordered, base, r"\.part\d+\.rar$")
            used.update(p.name.lower() for p in vols)
            first = next((p for p in vols if _partnum(p.name) == 1), None)
            if first is not None:
                sets.append((first, vols))
            continue
        # Multi-volume 7z: name.7z.001, name.7z.002, ...
        if _7Z_VOL_RE.match(f.name):
            base = _7Z_VOL_RE.match(f.name).group("base")  # type: ignore[union-attr]
            vols = _siblings(ordered, base, r"\.7z\.\d{3}$")
            used.update(p.name.lower() for p in vols)
            first = next((p for p in vols if p.name.lower().endswith(".7z.001")), None)
            if first is not None:
                sets.append((first, vols))
            continue
        # Old-style RAR continuation (name.r00) — folded in with its .rar below.
        if _RNN_RE.match(f.name):
            continue
        low = key
        if low.endswith(".rar"):
            base = f.name[:-4]
            vols = [f, *_siblings(ordered, base, r"\.r\d{2,3}$")]
            used.update(p.name.lower() for p in vols)
            sets.append((f, vols))
        elif low.endswith((".zip", ".7z")):
            used.add(key)
            sets.append((f, [f]))
    return sets


def _write_report(
    ctx: RunContext, plan: list[dict[str, object]], extracted: int, removed: int
) -> None:
    out_dir = ctx.config.reporting.dir / "extractor"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "plan.json").write_text(
        json.dumps(
            {"extracted": extracted, "quarantined": removed, "archives": plan},
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    lines = [
        "# Extractor plan",
        "",
        f"Archives extracted: {extracted}",
        f"Volumes quarantined: {removed}",
        "",
        f"## Sets ({len(plan)})",
        *[
            f"- [{'ok' if a['extracted'] else 'FAIL'}] {a['archive']}"
            f" ({len(a['volumes']) if isinstance(a['volumes'], list) else 0} vol)"
            + (" (dry-run)" if a["dry_run"] else "")
            for a in plan
        ],
        "",
    ]
    (out_dir / "plan.md").write_text("\n".join(lines), encoding="utf-8")


@register("extractor")
def run(ctx: RunContext) -> ModuleResult:
    result = ModuleResult(module="extractor", run_id=ctx.run_id, mode=ctx.mode)
    paths = ctx.config.paths
    source = Path(paths.get("extract_source") or paths.get("organizer_source", ""))
    if not str(source) or not source.is_dir():
        result.add_failure(
            FailureRecord(category="config", message=f"extract source not a dir: {source}")
        )
        return result

    files = [p for p in source.rglob("*") if p.is_file()]
    sets = archive_sets(files)

    extracted = 0
    removed = 0
    plan: list[dict[str, object]] = []
    for first, vols in sets:
        dest = first.parent
        # In a dry run we cannot know the real outcome, so we optimistically plan
        # success (extract + quarantine) without touching disk.
        ok = True if ctx.fs.dry_run else archive.extract(first, dest)
        if ok:
            extracted += 1
            for vol in vols:
                try:
                    ctx.fs.quarantine(vol, reason="extractor: archive extracted ok")
                    removed += 1
                except Exception as exc:  # never abort the batch on one bad move
                    ctx.logger.warning(
                        "extractor quarantine skipped", path=str(vol), error=str(exc)
                    )
        else:
            result.add_failure(
                FailureRecord(category="integration", message=f"extract failed: {first}")
            )
        plan.append(
            {
                "archive": str(first),
                "volumes": [str(v) for v in vols],
                "dest": str(dest),
                "extracted": ok,
                "dry_run": ctx.fs.dry_run,
            }
        )

    _write_report(ctx, plan, extracted, removed)
    ctx.logger.info(
        "extractor done",
        archives=len(sets),
        extracted=extracted,
        quarantined=removed,
        dry_run=ctx.fs.dry_run,
    )
    result.quarantined = removed
    result.actions = extracted + removed
    return result
