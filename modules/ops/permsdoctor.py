"""Permissions doctor — detect appdata owner/mode drift and PROPOSE the fix.

Under heavy container churn, appdata files drift to the wrong owner/mode and a
service then fails (mysql ``error 13``, wizarr "unable to open database file",
traefik ``acme.json`` too open, …). This READ-ONLY module compares each configured
path's current owner (``uid:gid``) and mode against the expected values and, on a
mismatch, emits an operator-confirmable ``chown`` / ``chmod`` action.

It never changes anything itself (INVARIANT I1 + proposal-only, like
:mod:`modules.ops.autoheal`): it writes its plan under ``reporting.dir/permsdoctor``
in the SAME ``actions[]`` shape the apply layer understands, so the operator applies
them through the existing confirmed ``/apply`` flow. ``chmod`` / ``chown`` are already
on the :func:`aictx.apply.classify` positive allow-list, so no new privileged path.

Config (config.json):
  integrations.permsdoctor :
    paths : list of {path, owner ("uid:gid"), mode ("0775"|"775")}
            owner and/or mode may be omitted to check only the other.

Metrics: ``checked``, ``drifted`` (paths needing a fix), ``proposed`` (actions).
"""

from __future__ import annotations

import json
import os
import stat as stat_mod
from collections.abc import Callable
from dataclasses import dataclass

from aictx.apply import ApplyAction, classify, finding_fingerprint
from core.registry import register
from core.types import FailureRecord, ModuleResult, RunContext

# (path) -> (uid, gid, mode_bits) or None if the path is missing/unreadable.
# Injected so tests never depend on real ownership (which they can't set portably).
StatFn = Callable[[str], "PathPerm | None"]


@dataclass(frozen=True)
class PathPerm:
    uid: int
    gid: int
    mode: int  # the permission bits only (st_mode & 0o7777)


@dataclass(frozen=True)
class PathRule:
    path: str
    owner: str | None  # "uid:gid" or None to skip owner check
    mode: str | None  # "0775" / "775" or None to skip mode check


@dataclass(frozen=True)
class PathFinding:
    path: str
    status: str  # ok | missing | drift | error
    current_owner: str
    current_mode: str
    commands: tuple[str, ...]
    detail: str = ""


def _str(raw: object) -> str:
    return raw.strip() if isinstance(raw, str) and raw.strip() else ""


def _rules(ctx: RunContext) -> list[PathRule]:
    cfg = ctx.config.integrations.get("permsdoctor", {})
    raw = cfg.get("paths")
    if not isinstance(raw, list):
        return []
    out: list[PathRule] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        path = _str(entry.get("path"))
        if not path:
            continue
        out.append(
            PathRule(
                path=path,
                owner=_str(entry.get("owner")) or None,
                mode=_str(entry.get("mode")) or None,
            )
        )
    return out


def default_stat(path: str) -> PathPerm | None:
    """Real ``os.stat`` → :class:`PathPerm` (None if missing/unreadable)."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return PathPerm(uid=st.st_uid, gid=st.st_gid, mode=stat_mod.S_IMODE(st.st_mode))


def _norm_mode(mode: str) -> int | None:
    """Parse an octal mode string ("0775"/"775") to its int, or None if invalid."""
    try:
        return int(mode, 8)
    except ValueError:
        return None


def evaluate_path(rule: PathRule, perm: PathPerm | None) -> PathFinding:
    """Compare a path's actual perms against its rule; build the fix commands.

    Pure: the current perms are passed in. A missing path is reported, never a
    crash. Only mismatching aspects yield a command, and each command is
    allow-list-classified (defense in depth) before being included.
    """
    if perm is None:
        return PathFinding(rule.path, "missing", "-", "-", (), "path not found")

    cur_owner = f"{perm.uid}:{perm.gid}"
    cur_mode = f"{perm.mode:04o}"
    commands: list[str] = []
    drifted = False

    if rule.owner is not None and rule.owner != cur_owner:
        drifted = True
        cmd = f"chown {rule.owner} {rule.path}"
        if classify(cmd).allowed:
            commands.append(cmd)

    if rule.mode is not None:
        want = _norm_mode(rule.mode)
        if want is None:
            return PathFinding(
                rule.path,
                "error",
                cur_owner,
                cur_mode,
                (),
                f"invalid mode in config: {rule.mode!r}",
            )
        if want != perm.mode:
            drifted = True
            cmd = f"chmod {rule.mode} {rule.path}"
            if classify(cmd).allowed:
                commands.append(cmd)

    status = "drift" if drifted else "ok"
    return PathFinding(rule.path, status, cur_owner, cur_mode, tuple(commands))


def _to_actions(findings: list[PathFinding]) -> list[ApplyAction]:
    """Turn drifted findings' commands into de-duplicated ApplyActions."""
    out: list[ApplyAction] = []
    seen: set[str] = set()
    for finding in findings:
        title = f"permisos: {finding.path}"
        for cmd in finding.commands:
            if cmd in seen:
                continue
            verdict = classify(cmd)
            if not verdict.allowed:
                continue
            seen.add(cmd)
            out.append(
                ApplyAction(
                    command=cmd,
                    category=verdict.category,
                    finding_title=title,
                    fingerprint=finding_fingerprint(title),
                    severity="warning",
                )
            )
    return out


def _write_report(
    ctx: RunContext, findings: list[PathFinding], actions: list[ApplyAction], note: str
) -> None:
    out_dir = ctx.config.reporting.dir / "permsdoctor"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "plan.json").write_text(
        json.dumps(
            {
                "checked": len(findings),
                "drifted": sum(1 for f in findings if f.status == "drift"),
                "proposed_count": len(actions),
                "note": note,
                "findings": [
                    {
                        "path": f.path,
                        "status": f.status,
                        "current_owner": f.current_owner,
                        "current_mode": f.current_mode,
                        "commands": list(f.commands),
                        "detail": f.detail,
                    }
                    for f in findings
                ],
                "actions": [
                    {
                        "command": a.command,
                        "category": a.category,
                        "finding_title": a.finding_title,
                        "fingerprint": a.fingerprint,
                        "severity": a.severity,
                    }
                    for a in actions
                ],
            },
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    lines = [
        "# Permsdoctor — deriva de permisos en appdata (solo lectura)",
        "",
        f"Rutas comprobadas: {len(findings)}",
        f"Con deriva: {sum(1 for f in findings if f.status == 'drift')}",
        f"Acciones propuestas: {len(actions)}",
        "",
        "> Solo propuestas — nada se aplica aquí. Confirma con /apply (menú o bot).",
        "",
        "## Rutas",
    ]
    for f in findings:
        lines.append(f"- [{f.status}] {f.path} (owner={f.current_owner}, mode={f.current_mode})")
        for cmd in f.commands:
            lines.append(f"    → `{cmd}`")
        if f.detail:
            lines.append(f"    {f.detail}")
    if not findings:
        lines.append("(sin rutas configuradas — integrations.permsdoctor.paths)")
    if note:
        lines += ["", f"> {note}"]
    (out_dir / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


@register("permsdoctor")
def run(ctx: RunContext, *, statfn: StatFn = default_stat) -> ModuleResult:
    """Check configured appdata paths' owner/mode and propose chown/chmod fixes.

    ``statfn`` is injected so tests drive ownership deterministically (real uid/gid
    can't be set portably in a test). Strictly read-only: proposals go to the
    report; the operator applies them via the existing confirmed ``/apply`` flow.
    """
    result = ModuleResult(module="permsdoctor", run_id=ctx.run_id, mode=ctx.mode)
    rules = _rules(ctx)

    findings: list[PathFinding] = []
    for rule in rules:
        finding = evaluate_path(rule, statfn(rule.path))
        findings.append(finding)
        if finding.status in ("missing", "error"):
            result.add_failure(
                FailureRecord(
                    category="config",
                    message=f"{rule.path}: {finding.detail}",
                    src=rule.path,
                )
            )

    actions = _to_actions(findings)
    note = ""
    if not rules:
        note = (
            "No paths configured — set integrations.permsdoctor.paths to a list of "
            "{path, owner, mode} (e.g. owner '99:100', mode '0775')."
        )
    _write_report(ctx, findings, actions, note)
    ctx.logger.info(
        "permsdoctor done",
        checked=len(findings),
        drifted=sum(1 for f in findings if f.status == "drift"),
        proposed=len(actions),
    )
    result.metrics["checked"] = float(len(findings))
    result.metrics["drifted"] = float(sum(1 for f in findings if f.status == "drift"))
    result.metrics["proposed"] = float(len(actions))
    result.actions = 0  # proposal-only; execution happens via /apply
    return result
