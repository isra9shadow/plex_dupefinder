"""Radarr tagger — write computed, izumi-managed tags back to Radarr so its native
filters can use them (the first case: ``saga`` = belongs to any TMDb collection).

Why: Radarr can filter by a *specific* collection but not by "belongs to ANY
collection", so a low-quality cleanup filter wrongly surfaces saga movies you want
to keep. This module tags every movie in a collection with ``<prefix>saga`` (default
``izumi-saga``); you then filter ``Etiqueta NO contiene izumi-saga``.

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
    managed_prefix : label prefix izumi owns (default "izumi-")
    rules          : list of {tag, all:[{predicate: param}, …]} (default: saga)
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass

from core import secrets
from core.cache import Cache
from core.errors import ConfigError, IntegrationError, SecretError
from core.registry import register
from core.types import FailureRecord, ModuleResult, RunContext, SafetyMode
from integrations.radarr import RadarrClient

from modules.arr.tag_rules import (
    DEFAULT_RULES,
    build_cluster_batch_prompt,
    desired_tags,
    franchise_groups,
    parse_rejected,
)

# (prompt) -> answer. Injected so tests never call the LLM (Phase B verification).
LLM = Callable[[str], str]

_DEFAULT_PREFIX = "izumi-"  # Radarr tag labels reject ':' → use a hyphen (izumi-saga)
_BATCH = 200  # movies per PUT /movie/editor call (avoid timeouts on big libraries)
_REFRESH_BATCH = 100  # movies per RefreshMovie command


def _chunks(items: list[int], size: int) -> list[list[int]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


@dataclass(frozen=True)
class _Settings:
    managed_prefix: str
    rules: tuple[dict[str, object], ...]
    cluster: bool  # Phase A: tag movies that share a title stem (franchise heuristic)
    cluster_min: int
    cluster_tag: str
    cluster_verify: bool  # Phase B: have the local LLM confirm each candidate group
    cluster_batch: int  # groups per LLM call (fewer calls = faster)
    cluster_verify_max: int  # cap NEW verifications per run (0 = unlimited); rest cached later
    refresh_untagged: bool  # queue a Radarr metadata refresh for movies without the tag


def _bool(value: object) -> bool:
    return value is True


def _pos_int(value: object, default: int, minimum: int = 0) -> int:
    return (
        value
        if isinstance(value, int) and not isinstance(value, bool) and value >= minimum
        else default
    )


def _settings(ctx: RunContext) -> _Settings:
    cfg = ctx.config.integrations.get("radarr_tagger", {})
    prefix = cfg.get("managed_prefix")
    raw_rules = cfg.get("rules")
    rules = (
        tuple(r for r in raw_rules if isinstance(r, dict))
        if isinstance(raw_rules, list) and raw_rules
        else DEFAULT_RULES
    )
    ctag = cfg.get("cluster_tag")
    return _Settings(
        managed_prefix=prefix if isinstance(prefix, str) and prefix else _DEFAULT_PREFIX,
        rules=rules,
        cluster=_bool(cfg.get("cluster")),
        cluster_min=_pos_int(cfg.get("cluster_min"), 2, minimum=2),
        cluster_tag=ctag if isinstance(ctag, str) and ctag else "saga",
        cluster_verify=_bool(cfg.get("cluster_verify")),
        cluster_batch=_pos_int(cfg.get("cluster_batch"), 20, minimum=1),
        cluster_verify_max=_pos_int(cfg.get("cluster_verify_max"), 0),
        refresh_untagged=_bool(cfg.get("refresh_untagged")),
    )


def _make_llm(ctx: RunContext) -> LLM:
    def _call(prompt: str) -> str:  # pragma: no cover - needs Ollama
        from integrations.ollama import OllamaClient

        cfg = ctx.config.integrations.get("ollama", {})
        kwargs: dict[str, object] = {}
        base, model = cfg.get("base_url"), cfg.get("model")
        if isinstance(base, str) and base:
            kwargs["base_url"] = base
        if isinstance(model, str) and model:
            kwargs["model"] = model
        return OllamaClient(**kwargs).complete(prompt)  # type: ignore[arg-type]

    return _call


def _cluster_ids(
    groups: list[tuple[str, list[int], list[str]]],
    settings: _Settings,
    *,
    llm: LLM,
    cache: Cache | None,
) -> set[int]:
    """Movie ids to tag from clustering.

    With ``cluster_verify``, the LLM confirms candidate groups in BATCHES (few calls),
    cached by stem, fail-OPEN (keep on doubt/LLM-down) since over-tagging only over-
    protects. ``cluster_verify_max`` caps NEW verifications per run so a huge library
    never blocks on the GPU — the rest are kept now and verified on later runs.
    """
    if not settings.cluster_verify:
        return {mid for _stem, ids, _titles in groups for mid in ids}

    out: set[int] = set()
    pending: list[tuple[str, list[int], list[str]]] = []
    for stem, ids, titles in groups:
        cached = cache.get(f"franchise:{stem}") if cache is not None else None
        if isinstance(cached, bool):
            if cached:
                out.update(ids)
        else:
            pending.append((stem, ids, titles))

    if settings.cluster_verify_max > 0 and len(pending) > settings.cluster_verify_max:
        deferred = pending[settings.cluster_verify_max :]  # keep now, verify next run
        pending = pending[: settings.cluster_verify_max]
        for _stem, ids, _titles in deferred:
            out.update(ids)

    for i in range(0, len(pending), settings.cluster_batch):
        chunk = pending[i : i + settings.cluster_batch]
        try:
            rejected = parse_rejected(
                llm(build_cluster_batch_prompt([t for _s, _i, t in chunk])), len(chunk)
            )
        except Exception:  # LLM down/unreachable → keep the whole batch (safe)
            rejected = set()
        for j, (stem, ids, _titles) in enumerate(chunk, 1):
            ok = j not in rejected
            if cache is not None:
                cache.set(f"franchise:{stem}", ok)
            if ok:
                out.update(ids)
    if cache is not None:
        cache.save()
    return out


def _untagged_ids(
    movies: list[dict[str, object]], label_to_id: dict[str, int], saga_label: str
) -> list[int]:
    """Movie ids that do NOT currently carry the managed saga tag (refresh targets)."""
    saga_id = label_to_id.get(saga_label)
    out: list[int] = []
    for movie in movies:
        mid = _int(movie.get("id"))
        if mid is None:
            continue
        raw = movie.get("tags")
        tags = raw if isinstance(raw, list) else []
        if saga_id is None or saga_id not in tags:
            out.append(mid)
    return out


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
    cluster_ids: set[int] | None = None,
) -> _Plan:
    """Pure diff: which managed tags to add/remove on which movies (no I/O)."""
    prefix = settings.managed_prefix
    clusters = cluster_ids or set()
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
        if mid in clusters:  # franchise-by-title-stem (Phase A/B) → same saga tag
            want.add(prefix + settings.cluster_tag)
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


def _write_report(
    ctx: RunContext,
    plan: _Plan,
    *,
    dry_run: bool,
    applied: int,
    apply_error: str = "",
    refreshed: int = 0,
) -> None:
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
                "apply_error": apply_error,
                "refreshed": refreshed,
            },
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    mode_label = "DRY-RUN (no escribe)" if dry_run else "LIVE"
    refresh_note = f" · refrescadas: {refreshed}" if refreshed else ""
    lines = [
        "# Radarr tagger",
        "",
        f"Modo: {mode_label} · películas evaluadas: {plan.evaluated} · "
        f"aplicados: {applied}{refresh_note}",
        "",
    ]
    if apply_error:
        lines += [f"> ERROR al aplicar (tras {applied}): {apply_error}", ""]
    lines += [
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
def run(
    ctx: RunContext, *, client: RadarrClient | None = None, llm: LLM | None = None
) -> ModuleResult:
    """Compute izumi-managed Radarr tags from rules + clustering and apply (LIVE only).

    ``client``/``llm`` are injected in tests; production builds them from config.
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

    # Phase A/B: franchise clustering by shared title stem, optionally LLM-verified.
    cluster_ids: set[int] = set()
    if settings.cluster:
        groups = franchise_groups(movies, settings.cluster_min)
        cache = (
            Cache(ctx.config.reporting.dir / "cache" / "radarr_tagger.db")
            if settings.cluster_verify
            else None
        )
        cluster_ids = _cluster_ids(groups, settings, llm=llm or _make_llm(ctx), cache=cache)

    # Optional: queue a Radarr metadata refresh for movies WITHOUT the saga tag, so
    # collections Radarr hasn't synced land by the NEXT run (fire-and-forget).
    refreshed = 0
    if settings.refresh_untagged:
        saga_label = settings.managed_prefix + settings.cluster_tag
        targets = _untagged_ids(movies, label_to_id, saga_label)
        if dry_run:
            refreshed = len(targets)
        elif targets:
            try:
                for chunk in _chunks(targets, _REFRESH_BATCH):
                    rc.refresh_movies(chunk)
                    refreshed += len(chunk)
            except IntegrationError as exc:
                result.add_failure(FailureRecord(category="integration", message=str(exc)))
                ctx.logger.warning("radarr_tagger refresh failed", error=str(exc))

    plan = _plan_changes(movies, id_to_label, settings, cluster_ids)

    applied = 0
    apply_error = ""
    if not dry_run:
        try:
            for lbl, ids in plan.add.items():
                tid = label_to_id.get(lbl)
                if tid is None:  # create the managed tag on first use
                    tid = _int(rc.create_tag(lbl).get("id"))
                    if tid is None:
                        continue
                    label_to_id[lbl] = tid
                for chunk in _chunks(ids, _BATCH):  # batch so a big library never times out
                    rc.edit_movie_tags(chunk, tid, add=True)
                    applied += len(chunk)
            for lbl, ids in plan.remove.items():
                tid = label_to_id.get(lbl)
                if tid is not None:
                    for chunk in _chunks(ids, _BATCH):
                        rc.edit_movie_tags(chunk, tid, add=False)
                        applied += len(chunk)
        except IntegrationError as exc:
            apply_error = str(exc)
            result.add_failure(FailureRecord(category="integration", message=apply_error))
            ctx.logger.warning("radarr_tagger apply failed", error=apply_error, applied=applied)

    _write_report(
        ctx, plan, dry_run=dry_run, applied=applied, apply_error=apply_error, refreshed=refreshed
    )
    to_add = sum(len(v) for v in plan.add.values())
    to_remove = sum(len(v) for v in plan.remove.values())
    ctx.logger.info(
        "radarr tagger",
        evaluated=plan.evaluated,
        to_add=to_add,
        to_remove=to_remove,
        applied=applied,
        refreshed=refreshed,
        clustered=len(cluster_ids),
        dry_run=dry_run,
    )
    result.metrics["evaluated"] = float(plan.evaluated)
    result.metrics["to_add"] = float(to_add)
    result.metrics["to_remove"] = float(to_remove)
    result.metrics["refreshed"] = float(refreshed)
    result.actions = applied
    return result
