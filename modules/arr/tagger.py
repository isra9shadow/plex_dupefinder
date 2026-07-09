"""Radarr tagger — write computed, izumi-managed tags back to Radarr so its native
filters can use them (the first case: ``saga`` = belongs to any TMDb collection).

Why: Radarr can filter by a *specific* collection but not by "belongs to ANY
collection", so a low-quality cleanup filter wrongly surfaces saga movies you want
to keep. This module tags every movie in a collection with ``<prefix>saga`` (default
``izumi:saga``); you then filter ``Etiqueta NO contiene izumi:saga``.

It is rule-driven but deliberately small (see ``tag_rules``): a flat list of
``{tag, all:[conditions]}`` over the movie object Radarr already returns — no TMDb,
no own database. Idempotent: it reads Radarr's current tags each run and applies
only the diff.

Safety:
  * DRY_RUN by default (I2): it reports the diff but writes nothing; LIVE applies it.
  * It ONLY ever adds/removes tags whose label starts with ``managed_prefix`` — it
    never touches your manual tags, and it never deletes movies.

Config (config.json):
  integrations.radarr : {url, api_key_ref}                  # reused (see arr/orphans)
  integrations.radarr_tagger :
    managed_prefix : label prefix izumi owns (default "izumi:")
    rules          : list of {tag, all:[{predicate: param}, …]} (default: saga)
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass

from core import secrets
from core.errors import ConfigError, IntegrationError, SecretError
from core.registry import register
from core.types import FailureRecord, ModuleResult, RunContext, SafetyMode
from integrations.radarr import RadarrClient

from modules.arr.tag_rules import DEFAULT_RULES, desired_tags

_DEFAULT_PREFIX = "izumi:"


@dataclass(frozen=True)
class _Settings:
    managed_prefix: str
    rules: tuple[dict[str, object], ...]


def _settings(ctx: RunContext) -> _Settings:
    cfg = ctx.config.integrations.get("radarr_tagger", {})
    prefix = cfg.get("managed_prefix")
    raw_rules = cfg.get("rules")
    rules = (
        tuple(r for r in raw_rules if isinstance(r, dict))
        if isinstance(raw_rules, list) and raw_rules
        else DEFAULT_RULES
    )
    return _Settings(
        managed_prefix=prefix if isinstance(prefix, str) and prefix else _DEFAULT_PREFIX,
        rules=rules,
    )


def _radarr(ctx: RunContext) -> RadarrClient:
    cfg = ctx.config.integrations.get("radarr", {})
    url, ref = cfg.get("url"), cfg.get("api_key_ref")
    if not isinstance(url, str) or not isinstance(ref, str):
        raise ConfigError("integrations.radarr needs string 'url' and 'api_key_ref'")
    return RadarrClient(url, secrets.require(ref))


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


@dataclass
class _Plan:
    evaluated: int
    add: dict[str, list[int]]  # managed label -> movie ids to add it to
    remove: dict[str, list[int]]  # managed label -> movie ids to remove it from


def _plan_changes(
    movies: list[dict[str, object]],
    id_to_label: dict[int, str],
    settings: _Settings,
) -> _Plan:
    """Pure diff: which managed tags to add/remove on which movies (no I/O)."""
    prefix = settings.managed_prefix
    managed = {lbl for lbl in id_to_label.values() if lbl.startswith(prefix)}
    add: dict[str, list[int]] = defaultdict(list)
    remove: dict[str, list[int]] = defaultdict(list)
    evaluated = 0
    for movie in movies:
        mid = _int(movie.get("id"))
        if mid is None:
            continue
        evaluated += 1
        want = {prefix + t for t in desired_tags(movie, settings.rules)}
        raw_tags = movie.get("tags")
        have = {
            id_to_label[tid]
            for tid in (raw_tags if isinstance(raw_tags, list) else [])
            if isinstance(tid, int) and tid in id_to_label and id_to_label[tid] in managed
        }
        for lbl in want - have:
            add[lbl].append(mid)
        for lbl in have - want:
            remove[lbl].append(mid)
    return _Plan(evaluated=evaluated, add=dict(add), remove=dict(remove))


def _write_report(ctx: RunContext, plan: _Plan, *, dry_run: bool, applied: int) -> None:
    out_dir = ctx.config.reporting.dir / "radarr_tagger"
    out_dir.mkdir(parents=True, exist_ok=True)
    add_counts = {lbl: len(ids) for lbl, ids in sorted(plan.add.items())}
    rem_counts = {lbl: len(ids) for lbl, ids in sorted(plan.remove.items())}
    (out_dir / "plan.json").write_text(
        json.dumps(
            {
                "mode": "dry_run" if dry_run else "live",
                "evaluated": plan.evaluated,
                "to_add": add_counts,
                "to_remove": rem_counts,
                "applied": applied,
            },
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    mode_label = "DRY-RUN (no escribe)" if dry_run else "LIVE"
    lines = [
        "# Radarr tagger",
        "",
        f"Modo: {mode_label} · películas evaluadas: {plan.evaluated}",
        "",
        f"## A AÑADIR ({sum(add_counts.values())})",
        *([f"- {lbl}: {n}" for lbl, n in add_counts.items()] or ["(ninguno)"]),
        "",
        f"## A QUITAR ({sum(rem_counts.values())})",
        *([f"- {lbl}: {n}" for lbl, n in rem_counts.items()] or ["(ninguno)"]),
        "",
    ]
    (out_dir / "summary.md").write_text("\n".join(lines), encoding="utf-8")


def _fail(ctx: RunContext, result: ModuleResult, category: str, msg: str) -> ModuleResult:
    """Record + log the failure AND write an error report (so the widget/panel show why)."""
    result.add_failure(FailureRecord(category=category, message=msg))
    ctx.logger.warning("radarr_tagger failed", error=msg)
    out_dir = ctx.config.reporting.dir / "radarr_tagger"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "plan.json").write_text(
        json.dumps({"error": msg, "evaluated": 0}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    (out_dir / "summary.md").write_text(
        f"# Radarr tagger\n\n> ERROR ({category}): {msg}\n\n"
        "Revisa `integrations.radarr` en config.json: necesita `url` y "
        "`api_key_ref` (p. ej. `RADARR_API_KEY`), y que Radarr sea accesible.\n",
        encoding="utf-8",
    )
    return result


@register("radarr_tagger")
def run(ctx: RunContext, *, client: RadarrClient | None = None) -> ModuleResult:
    """Compute izumi-managed Radarr tags from rules and apply the diff (LIVE only).

    ``client`` is injected in tests; production builds it from config via ``_radarr``.
    """
    result = ModuleResult(module="radarr_tagger", run_id=ctx.run_id, mode=ctx.mode)
    settings = _settings(ctx)
    dry_run = ctx.mode != SafetyMode.LIVE

    try:
        rc = client or _radarr(ctx)
        movies = rc.movies()
        tags = rc.tags()
    except (ConfigError, SecretError) as exc:
        return _fail(ctx, result, "config", str(exc))
    except IntegrationError as exc:
        return _fail(ctx, result, "integration", str(exc))

    label_to_id: dict[str, int] = {}
    id_to_label: dict[int, str] = {}
    for tag in tags:
        tid, lbl = _int(tag.get("id")), tag.get("label")
        if tid is not None and isinstance(lbl, str):
            label_to_id[lbl] = tid
            id_to_label[tid] = lbl

    plan = _plan_changes(movies, id_to_label, settings)

    applied = 0
    if not dry_run:
        try:
            for lbl, ids in plan.add.items():
                tid = label_to_id.get(lbl)
                if tid is None:  # create the managed tag on first use
                    tid = _int(rc.create_tag(lbl).get("id"))
                    if tid is None:
                        continue
                    label_to_id[lbl] = tid
                rc.edit_movie_tags(ids, tid, add=True)
                applied += len(ids)
            for lbl, ids in plan.remove.items():
                tid = label_to_id.get(lbl)
                if tid is not None:
                    rc.edit_movie_tags(ids, tid, add=False)
                    applied += len(ids)
        except IntegrationError as exc:
            result.add_failure(FailureRecord(category="integration", message=str(exc)))

    _write_report(ctx, plan, dry_run=dry_run, applied=applied)
    to_add = sum(len(v) for v in plan.add.values())
    to_remove = sum(len(v) for v in plan.remove.values())
    ctx.logger.info(
        "radarr tagger",
        evaluated=plan.evaluated,
        to_add=to_add,
        to_remove=to_remove,
        applied=applied,
        dry_run=dry_run,
    )
    result.metrics["evaluated"] = float(plan.evaluated)
    result.metrics["to_add"] = float(to_add)
    result.metrics["to_remove"] = float(to_remove)
    result.actions = applied
    return result
