"""Config doctor — a read-only audit of EVERY configurable setting.

It loads the real environment (``.env`` / ``os.environ`` via ``core/secrets``)
and the real ``config.json`` tree, then runs the declarative registry in
``core/configspec`` to classify each setting as OK / MISSING / INVALID with a
human remediation hint. Output is grouped in ``summary.md`` + ``plan.json`` under
``reporting.dir / "configcheck"``.

Strictly READ-ONLY (INVARIANT I1): it reads env + config + (for ``*_exists``
validators) checks whether paths exist on disk. It never moves, deletes or writes
anything outside its own report.

The actual catalogue of settings lives in ``core/configspec`` (the single source
of truth shared with the future menu editor); this module only wires the real
env/config/filesystem into the pure evaluator and renders the report.

Config (config.json):
  integrations.configcheck :
    extra_required : optional list of configspec keys to treat as required even
                     if the registry marks them optional (operator can tighten).
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass, replace

from core import configspec
from core.configspec import PathChecker, SettingSpec, SettingStatus, Status
from core.registry import register
from core.secrets import get_secret
from core.types import FailureRecord, ModuleResult, RunContext

# Reads a secret/env var by name; returns None when unset. Injected for tests.
EnvReader = Callable[[str], "str | None"]
"""Type alias: ``(name) -> str | None`` secret/env reader (see ``_read_env``)."""


@dataclass(frozen=True)
class _Settings:
    extra_required: frozenset[str]


def _str_set(raw: object) -> frozenset[str]:
    if not isinstance(raw, list):
        return frozenset()
    return frozenset(item.strip() for item in raw if isinstance(item, str) and item.strip())


def _settings(ctx: RunContext) -> _Settings:
    cfg = ctx.config.integrations.get("configcheck", {})
    return _Settings(extra_required=_str_set(cfg.get("extra_required")))


def _read_env(specs: list[SettingSpec], reader: EnvReader) -> dict[str, str]:
    """Resolve every env-located spec into a plain ``{name: value}`` dict.

    ``reader`` is a ``(name) -> str | None`` callable (defaults to
    ``core/secrets.get_secret`` with ``required=False``) so the real ``.env`` is
    consulted exactly once per key and tests can inject a fake. Absent values are
    simply omitted (the evaluator treats an absent env key as unset).
    """
    out: dict[str, str] = {}
    for spec in specs:
        if not spec.is_env:
            continue
        value = reader(spec.key)
        if isinstance(value, str) and value != "":
            out[spec.key] = value
    return out


def _secret_reader(name: str) -> str | None:
    """Default env reader: never raises, never logs the value (fail-soft)."""
    return get_secret(name, required=False)


def _apply_extra_required(
    specs: list[SettingSpec], extra_required: frozenset[str]
) -> list[SettingSpec]:
    """Return specs with ``extra_required`` keys forced to ``required=True``."""
    if not extra_required:
        return specs
    return [replace(spec, required=True) if spec.key in extra_required else spec for spec in specs]


def _redact(spec: SettingSpec, status: SettingStatus) -> str | None:
    """Never echo a secret's value in the report — only whether it is set.

    Env-located settings are treated as secrets: we report ``"(set)"`` instead of
    the raw value. config.json values are not secrets (they are committed sample
    values like URLs/paths) and are shown verbatim to aid remediation.
    """
    if status.value is None:
        return None
    if spec.is_env:
        return "(set)"
    return status.value


def _write_report(
    ctx: RunContext,
    statuses: list[SettingStatus],
) -> tuple[int, int]:
    """Write summary.md + plan.json grouped by status; return (missing, invalid)."""
    out_dir = ctx.config.reporting.dir / "configcheck"
    out_dir.mkdir(parents=True, exist_ok=True)

    grouped = configspec.by_status(statuses)
    missing = grouped[Status.MISSING]
    invalid = grouped[Status.INVALID]
    ok = grouped[Status.OK]

    def _entry(st: SettingStatus) -> dict[str, object]:
        return {
            "key": st.spec.key,
            "location": st.spec.location,
            "group": st.spec.group,
            "status": st.status.value,
            "required": st.spec.required,
            "value": _redact(st.spec, st),
            "detail": st.detail,
            "remediation": st.remediation,
        }

    (out_dir / "plan.json").write_text(
        json.dumps(
            {
                "total": len(statuses),
                "ok_count": len(ok),
                "missing_count": len(missing),
                "invalid_count": len(invalid),
                "settings": [_entry(st) for st in statuses],
            },
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    def _section(title: str, items: list[SettingStatus], *, show_reason: bool) -> list[str]:
        lines = [f"## {title} ({len(items)})"]
        if not items:
            lines.append("(none)")
            return lines
        for st in items:
            shown = _redact(st.spec, st)
            head = f"- {st.spec.key} [{st.spec.location}]"
            if shown is not None and not show_reason:
                head += f" = {shown}"
            lines.append(head)
            if show_reason:
                reason = st.detail or "required but not set"
                lines.append(f"    - reason: {reason}")
                lines.append(f"    - fix: {st.remediation}")
        return lines

    lines = [
        "# Configcheck summary",
        "",
        f"Settings checked: {len(statuses)}",
        f"OK: {len(ok)}  MISSING: {len(missing)}  INVALID: {len(invalid)}",
        "",
        *_section("MISSING", missing, show_reason=True),
        "",
        *_section("INVALID", invalid, show_reason=True),
        "",
        *_section("OK", ok, show_reason=False),
        "",
    ]
    (out_dir / "summary.md").write_text("\n".join(lines), encoding="utf-8")
    return len(missing), len(invalid)


@register("configcheck")
def run(
    ctx: RunContext,
    *,
    env_reader: EnvReader = _secret_reader,
    path_exists: PathChecker = os.path.exists,
    dir_exists: PathChecker = os.path.isdir,
) -> ModuleResult:
    """Audit the live env + config against ``core/configspec`` (read-only).

    The I/O seams (``env_reader``, ``path_exists``, ``dir_exists``) are injected so
    tests run fully offline/deterministically; the production defaults read the
    real ``.env`` (via ``core/secrets``) and the real filesystem.
    """
    result = ModuleResult(module="configcheck", run_id=ctx.run_id, mode=ctx.mode)
    settings = _settings(ctx)

    specs = _apply_extra_required(list(configspec.SPECS), settings.extra_required)

    # Build the plain env + config dicts the pure evaluator consumes.
    try:
        env = _read_env(specs, env_reader)
    except Exception as exc:  # an env read must never abort the doctor
        env = {}
        result.add_failure(FailureRecord(category="config", message=f"env read failed: {exc}"))

    config = _live_config(ctx)

    statuses = configspec.evaluate(
        env,
        config,
        specs=specs,
        path_exists=path_exists,
        dir_exists=dir_exists,
    )

    missing_count, invalid_count = _write_report(ctx, statuses)

    for st in statuses:
        if st.status is Status.MISSING:
            result.add_failure(
                FailureRecord(
                    category="config",
                    message=f"missing: {st.spec.key} — {st.remediation}",
                )
            )
        elif st.status is Status.INVALID:
            result.add_failure(
                FailureRecord(
                    category="config",
                    message=f"invalid: {st.spec.key} ({st.detail}) — {st.remediation}",
                )
            )

    ctx.logger.info(
        "configcheck done",
        settings=len(statuses),
        missing=missing_count,
        invalid=invalid_count,
    )
    result.metrics["missing_count"] = float(missing_count)
    result.metrics["invalid_count"] = float(invalid_count)
    result.actions = 0  # read-only
    return result


def _live_config(ctx: RunContext) -> dict[str, object]:
    """Mirror the typed Config tree into a plain dict for dotted-path lookup.

    Only the sections configspec addresses are mirrored; values are primitives
    (Paths become strings). This keeps configspec stdlib-only and decoupled from
    the dataclass schema.
    """
    cfg = ctx.config
    return {
        "safety": {
            "mode": cfg.safety.mode.value,
            "audit": cfg.safety.audit,
            "min_file_age_hours": cfg.safety.min_file_age_hours,
            "stability_check_seconds": cfg.safety.stability_check_seconds,
            "max_size_ratio": cfg.safety.max_size_ratio,
            "quarantine_dir": str(cfg.safety.quarantine_dir),
            "retention_days": cfg.safety.retention_days,
            "auto_purge": cfg.safety.auto_purge,
        },
        "logging": {
            "level": cfg.logging.level,
            "dir": str(cfg.logging.dir),
            "rotate_mb": cfg.logging.rotate_mb,
            "backups": cfg.logging.backups,
        },
        "reporting": {
            "dir": str(cfg.reporting.dir),
            "metrics": cfg.reporting.metrics,
        },
        "notify": {
            "enabled": cfg.notify.enabled,
            "level": cfg.notify.level.value,
            "provider": cfg.notify.provider,
            "token_ref": cfg.notify.token_ref,
            "chat_id_ref": cfg.notify.chat_id_ref,
        },
        "paths": dict(cfg.paths),
        "integrations": dict(cfg.integrations),
    }
