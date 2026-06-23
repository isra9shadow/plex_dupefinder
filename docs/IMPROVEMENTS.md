# IMPROVEMENTS.md — prioritized improvement backlog

> Goal: cut total execution time **≥70%**, improve observability, and evolve from
> a deduper into a homelab automation+observability suite.
> Root-cause analysis is grounded in the **real code** (`plex_dupefinder.py`,
> `config.py`) — the runtime reports referenced were not attached, but these are
> the exact code paths that produce those metrics.
> Sizing: **QUICK WIN** (<1d) · **MEDIUM** (1–3d) · **LARGE** (>3d).

| ID | Improvement | Size | ROI |
|---|---|---|---|
| IMP-01 | Unify AUDIT_MODE / DRY_RUN / mode | QUICK WIN | 🔥 high (safety/clarity) |
| IMP-02 | Fix freed_bytes calc + reporting | QUICK WIN | 🔥 high (correctness) |
| IMP-03 | Profile discovery (per-phase timing) | QUICK WIN | 🔥 high (enables −70%) |
| IMP-04 | Safe parallel discovery + pre-analyze | MEDIUM | 🔥🔥 very high (−70% lever) |
| IMP-05 | Cut groups_skipped_inconsistent | MEDIUM | high |
| IMP-06 | Persistent cross-run cache | MEDIUM | 🔥🔥 very high (−70% lever) |
| IMP-07 | Metric: potentially recoverable space | QUICK WIN | medium-high |
| IMP-08 | Aggregate confidence metrics | QUICK WIN | medium |
| IMP-09 | Quarantine observability | QUICK WIN | high (trust) |
| IMP-10 | HomeLab Health Dashboard | MEDIUM→LARGE | 🔥 high (strategic) |
| IMP-12 | organizer: AI rename + relocate (Gemini) | MEDIUM | DELIVERED (post-dedupe tidy) |

---

## IMP-01 · Unify AUDIT_MODE / DRY_RUN / mode — **QUICK WIN**
- **Análisis:** legacy uses two booleans `DRY_RUN` + `AUDIT_MODE` (+ `QUARANTINE_MODE`); `acting = (not DRY_RUN) and not AUDIT_MODE` (plex_dupefinder.py:2447). The new platform uses one `SafetyMode` enum (DRY_RUN/AUDIT/LIVE).
- **Causa raíz:** dual representation → 8 boolean combos, some contradictory (`DRY_RUN=False, AUDIT_MODE=True` silently behaves as dry). Two sources of truth for "are we acting?".
- **Impacto:** removes a whole class of "why did/didn't it act?" bugs; one safety switch.
- **Complejidad:** baja. **ROI:** alto.
- **Propuesta:** single `resolve_mode(cfg) -> SafetyMode` at the entrypoint; map legacy booleans once (`AUDIT_MODE→AUDIT`, `DRY_RUN→DRY_RUN`, else `LIVE`); replace the ~6 `dry_run/audit_mode` checks with `mode is LIVE`. The wrapped module already carries `ctx.mode` — make it authoritative.

## IMP-02 · Fix freed_bytes calc + reporting — **QUICK WIN**
- **Análisis:** counters carry `freed_bytes` (line 2353) while purge reports `reclaimed_bytes` (1986); `reclaimed += freed or recorded_size` (1973) falls back to the *recorded* (estimated) size when actual free is unknown.
- **Causa raíz:** one metric conflates three different things — *quarantined* (moved, frees nothing on the array yet), *purged* (actually deleted), *estimated* (recorded size). A quarantine MOVE reports "freed" but frees no space until purge.
- **Impacto:** space numbers are currently misleading (over-reports freed space).
- **Complejidad:** baja. **ROI:** alto.
- **Propuesta:** three explicit metrics — `bytes_quarantined`, `bytes_purged` (only real deletes), `bytes_recoverable` (held in quarantine). Never label a move "freed". Surface all three in the report.

## IMP-03 · Profile discovery (per-phase timing) — **QUICK WIN**
- **Análisis:** `discovery_pass` (2262) is sequential; PRE_ANALYZE adds per-item `analyze()`+poll; per-part FS stat + `compute_partial_hashes` (556) + stability re-reads are serial I/O. No timing instrumentation today.
- **Causa raíz:** no data on where the wall-clock goes (Plex API vs ffprobe/analyze vs FS).
- **Impacto:** prerequisite to target the −70%.
- **Complejidad:** baja. **ROI:** alto.
- **Propuesta:** wrap each phase with a timer; add `timings_ms` (per phase + per-item p50/p95) to `RunReport.metrics`; a `--profile` flag logs the slowest items. Expected finding: Plex round-trips + analyze dominate.

## IMP-04 · Safe parallel discovery + pre-analyze — **MEDIUM**
- **Análisis:** the dominant cost is I/O-bound (Plex HTTP, ffprobe, FS stat), all serial. Discovery is **read-only** → safe to parallelize; the act/delete phase must stay serial.
- **Causa raíz:** sequential network/IO.
- **Impacto:** **the main −70% lever** — near-linear speedup up to a concurrency cap on I/O-bound work.
- **Complejidad:** media (bounded `ThreadPoolExecutor`, Plex rate-limit cap, deterministic ordering of results).
- **Propuesta:** parallelize only PASS1 metadata fetch + analyze + per-part probing with a configurable `DISCOVERY_WORKERS` (default 4–8) and a token-bucket for Plex; collect into a stable order; keep PASS2 actions serial with the existing `PLEX_DELETE_DELAY`.

## IMP-05 · Cut groups_skipped_inconsistent — **MEDIUM**
- **Análisis:** revalidation flags a group if any diff exists between snapshot and fresh (`status = inconsistent` line 2419 → `groups_skipped_inconsistent` 2433) via `detect_inconsistencies` (snapshot_parts vs fresh_parts incl. `partial_hash` 1778).
- **Causa raíz (hipótesis a confirmar con datos):** drift comes from **volatile non-decision fields** (addedAt/updatedAt/viewCount) or `analyze()` mutating bitrate/duration/size between passes, or hashing a file still being written.
- **Impacto:** fewer false skips → more groups actioned per run → fewer wasted re-runs.
- **Complejidad:** media (instrument first, then tune).
- **Propuesta:** record *which field* drifted per group, aggregate top drift fields in the report; then compare **only decision-relevant fields** (size, path, codec, resolution, source, partial_hash) — ignore volatile metadata.

## IMP-06 · Persistent cross-run cache — **MEDIUM**
- **Análisis:** every run recomputes ffprobe, metadata runtimes and `partial_hashes`; nothing persists across runs (the legacy media_triage had TSV caches — that capability was lost).
- **Causa raíz:** no persistence layer; steady-state libraries are mostly unchanged yet fully re-probed.
- **Impacto:** **second big −70% lever** — on repeat runs most files are unchanged → skip re-probing entirely.
- **Complejidad:** media.
- **Propuesta:** `core/cache.py` keyed by `(path, mtime, size)` persisting ffprobe results, TMDb/TVmaze runtimes and partial_hashes to JSON/sqlite under `/mnt/cache/appdata/izumi/cache`; invalidate on mtime/size change. Wire into `adapters/ffprobe`, `integrations/tmdb`, and discovery.

## IMP-07 · Metric: potentially recoverable space — **QUICK WIN**
- **Análisis:** no metric for space that *could* be freed (only acted/freed).
- **Causa raíz:** discovery knows non-keeper sizes but never aggregates them.
- **Impacto:** see the prize before acting; plan purges. Works in DRY_RUN/AUDIT (pure read).
- **Complejidad:** baja. **ROI:** medio-alto.
- **Propuesta:** sum non-keeper candidate sizes per actionable group → `bytes_recoverable` aggregate in the report (+ per-library breakdown).

## IMP-08 · Aggregate confidence metrics — **QUICK WIN**
- **Análisis:** the report surfaces `lowest_confidence_groups` but no aggregate distribution.
- **Causa raíz:** per-group score_delta computed but not aggregated.
- **Impacto:** see decision quality at a glance; tune `MIN_SCORE_DIFFERENCE`.
- **Complejidad:** baja. **ROI:** medio.
- **Propuesta:** add `confidence: {min, p10, median, count_below_threshold, histogram}` to the report metrics.

## IMP-09 · Quarantine observability — **QUICK WIN**
- **Análisis:** quarantine writes restore sidecars but there is no aggregate view (held bytes, ages, items past retention).
- **Causa raíz:** sidecars are per-item; never summarized.
- **Impacto:** trust + recoverability at a glance; safe purging.
- **Complejidad:** baja. **ROI:** alto.
- **Propuesta:** a `run.py quarantine` report (reuse `core/fs` + inventory pattern) → `reports/quarantine/status.{json,md}`: count, bytes held, age buckets, items eligible for purge (> retention), restore commands.

## IMP-10 · HomeLab Health Dashboard — **MEDIUM → LARGE**
- **Análisis:** data already exists but scattered (docker/disk/SMART/media/orphans/downloads reports). No single pane.
- **Causa raíz:** per-module reports, no aggregator.
- **Impacto:** the "see everything in <1 min" goal; the strategic evolution dupefinder → homelab suite.
- **Complejidad:** media (v1: aggregate all `reports/*` + health into one `reports/dashboard.md` + Telegram daily summary) → large (Grafana over the metrics textfile / tiny served HTML).
- **Propuesta:** v1 = a `dashboard` module aggregating every module's latest report + `run.py health` into `reports/dashboard.{md,json}`, pushed via `core/notify`. v2 = Grafana datasource over the Prometheus textfile.

---

## Roadmap to −70% execution time
1. **IMP-03** (profile) — measure, confirm bottleneck (likely Plex + analyze).
2. **IMP-06** (cache) + **IMP-04** (parallelize) — the two big levers: cache removes re-probing of unchanged files (steady-state runs are mostly unchanged libraries → 60–80% fewer probes); parallelism cuts the remaining I/O-bound discovery ~3–5×. Combined ≥70% on repeat runs.
3. **IMP-05** (fewer false skips) — recover wasted re-runs.
4. **IMP-01/02/07/08/09** (quick wins) — correctness + observability in parallel, any time.
5. **IMP-10** — dashboard, once the per-module data is stable.

---

## Production findings (real runs) — backlog updates

Real-run data folded into the backlog (no new components; refines existing IMPs).
Each is assigned to the agent that owns the area.

| Finding (real data) | → IMP | Agent | ROI | Size | Release |
|---|---|---|---|---|---|
| `groups_skipped_inconsistent` grows 59 → 144 → 222 | IMP-05 | Plex + Core | 🔥🔥 | MEDIUM | V0.2 |
| Discovery is THE bottleneck: 3082s / 5030s / 5803s | IMP-03 → IMP-04 | Core Platform | 🔥🔥 | QW + MEDIUM | V0.2 |
| Pass0 low ROI: 743 groups → 3 modified | **IMP-11** | Plex | high | MEDIUM | V0.2 |
| Info recomputed every run | IMP-06 → **SQLite** | Core Platform | 🔥🔥 | MEDIUM | V0.2 |
| AUDIT/DRY_RUN/QUARANTINE ambiguous | IMP-01 | Core Platform | 🔥 | QUICK WIN | V0.2 |

### IMP-05 (updated) · Stable fingerprints to stop drift skips
- **Real data:** skipped-inconsistent grows 59 → 144 → 222 across runs (worsening).
- **Root cause (confirmed direction):** the snapshot↔fresh check is sensitive to
  **cosmetic Plex changes** — renames, path changes, metadata refresh — none of which
  change the *file*.
- **Proposal:** decision identity = a **stable fingerprint** of
  `(media_id, size, duration, video_codec, audio_codec, partial_hash)` only.
  EXCLUDE `filename`, `filepath` and cosmetic metadata from the consistency check;
  re-skip only when a decision-relevant field changes.
- **Correction after reading the code:** `detect_inconsistencies` already compares
  only material fields (file, size, exists, duration, bitrate, codec, hash, keeper) —
  no cosmetic fields like `updatedAt`. The real driver is Plex **re-estimating
  `video_bitrate` (and sometimes `video_duration`) run-to-run without the file
  changing**.
- **DELIVERED (partial):** opt-in `INCONSISTENCY_BITRATE_TOLERANCE_PCT` and
  `INCONSISTENCY_DURATION_TOLERANCE_MS` (default 0 = strict). They absorb a
  bitrate/duration-only drift **only when the part `file_size` is identical** — a
  genuine transcode changes size (and partial hash), checked separately, so it still
  trips. The Unraid recommended profile ships `0.5%` / `100 ms`; the plain sample stays
  strict.
- **DELIVERED (full identity):** opt-in `INCONSISTENCY_USE_FINGERPRINT` (default OFF).
  `_media_fingerprint(pi)` = sha256 of `media_id | file_size | duration_s |
  video_codec | partial_hash`, deliberately excluding filename/path and bitrate.
  When ON, `detect_inconsistencies` compares one fingerprint per media instead of
  field-by-field, so renames and bitrate re-estimates never trip while real content
  changes (size/codec/hash) still do. Existence flips and keeper changes remain
  always-material in both modes. Recommended Unraid profile enables it. This is the
  root-cause fix for `groups_skipped_inconsistent` growth (59→144→222).

### IMP-04/03 (updated) · Discovery first — measured
- **Real timings:** 3082 / 5030 / 5803 s; discovery dominates wall-clock.
- **Rule:** do NOT optimise actions or reporting before discovery. Sequence:
  IMP-03 profile (per-phase timing) → IMP-04 parallelise (read-only, I/O-bound,
  bounded `ThreadPoolExecutor` + Plex rate cap) + IMP-06 cache → ≥70% reduction.
- **DELIVERED (IMP-03 for the legacy engine):** `discovery_pass` now records a
  read-only per-phase breakdown — `get_dupes_seconds` (Plex search), `pass0_seconds`
  (analyze+poll) and `gather_seconds` (filesystem stats + scoring) — plus
  `pass0_cache_hits`, into `run_report['phases']['discovery_breakdown']` and the
  console (`[profile] discovery breakdown …`). The PASS0 phase report also gains
  `groups_fast_pathed`. This lets the operator confirm the PASS0 fast-path win and
  locate the *next* bottleneck before any parallelisation (IMP-04). Zero behaviour
  change — pure instrumentation.
- **DELIVERED (IMP-04 parallel gather):** opt-in `DISCOVERY_GATHER_WORKERS` (default
  `1` = serial = current behaviour). When `>1` and PASS0 is off, each section's
  read-only gather (`_build_parts_for_item`: filesystem stats + scoring — "Pure
  observation, no writes/moves") is **pre-computed in a bounded `ThreadPoolExecutor`**
  via `_safe_build_parts` (never raises). The serial loop still owns decisions,
  ordering and per-item error handling — a prefetch miss/failure simply recomputes
  that item inline. Breakdown gains `gather_workers` + `gather_prefetched`.
  - **Benchmark before/after (procedure):** run the same library twice and compare
    `discovery_breakdown.gather_seconds`: `DISCOVERY_GATHER_WORKERS=1` (before) vs
    `=4` (after). PASS0 must be off (or cached) for the prefetch to engage. On
    spin-down arrays keep workers moderate (4) to avoid a spin-up storm.
- **DELIVERED (A2-02 duplicate reads):** `compute_partial_hashes` is memoised within
  the run by `(path, mtime_ns, size, hash_bytes)` (`_partial_hash_memo`, cleared at
  discovery start). PASS 2 revalidation and the parallel pre-gather no longer re-read
  an unchanged file's head+tail. Result-identical (pure function of the key); a
  changed file gets a new key → fresh read, so the PASS1↔PASS2 drift check stays
  correct. Thread-safe (deterministic values; no SQLite in worker threads).
  - **Not done (with rationale):** caching the existence/age stats across runs is
    *unsafe* — they must be fresh every run or the engine could act on a stale "file
    present" belief. Cross-run hash persistence in SQLite was skipped: it would put
    the single-thread `SqliteCache` connection inside the worker pool (A6 hazard) for
    marginal gain over the in-run memo.

### IMP-01 (DELIVERED) · Unified ExecutionMode
- **Problem:** three overlapping legacy flags (`DRY_RUN` / `AUDIT_MODE` /
  `QUARANTINE_MODE`) made "what will this run actually do?" ambiguous.
- **Delivered:** `resolve_execution_mode(cfg)` collapses them into ONE explicit mode
  — **AUDIT** (observe only), **QUARANTINE** (move loser, reversible), **DELETE**
  (irreversible). `apply_execution_mode(cfg)` makes an optional `EXECUTION_MODE` key
  the single source of truth: when set it normalizes the legacy flags (so every
  downstream guard keeps working unchanged); when absent the legacy flags are honoured
  as-is (**fully backward compatible** with existing `config.json`); an unrecognised
  value **fails safe to AUDIT**. The resolved mode is printed at startup and stored in
  `run_report['execution_mode']`, killing the ambiguity. Default samples ship
  `EXECUTION_MODE: ""` (no behaviour change).

### IMP-06 (updated) · Persistent cache → SQLite
- The shipped JSON `core/cache.py` is the V0.2 quick win. **Upgrade to SQLite** for a
  queryable schema: `media_id, score, bitrate, duration, fingerprint, last_seen`
  (+ ffprobe/runtime). SQLite chosen for concurrent reads, partial updates and
  `last_seen` queries; no clearly-better embedded single-host alternative.
- **DELIVERED:** `core.cache.SqliteCache` + `MediaRecord` added **additively** beside
  the JSON `Cache` (FREEZE-2 preserved). Schema exactly as above; `get()` is a pure,
  fingerprint-validated read, `put()` buffers writes (single commit per run),
  `prune(older_than_days=)` evicts files no longer seen, `query(where, params)` exposes
  the columns. **Wired into `media_integrity`**: ffprobe results now persist in
  `reports/cache/media.db`, vanished files age out after 30 days. The
  `media_id/score/bitrate` columns are filled by the dupefinder engine when it migrates
  (V0.3) — they also back the full IMP-05 stable-fingerprint identity.

### IMP-11 (new) · Pass0 (PRE_ANALYZE) fast-paths — MEDIUM
- **Real data:** 743 groups analysed, only 3 modified → ~0.4 % yield for full
  per-item `analyze()` + poll.
- **Cost/benefit:** Pass0 is expensive for near-zero gain on most groups.
- **Proposal (fast-paths, skip Pass0 when):** (a) single-candidate group (nothing to
  decide), (b) metadata fingerprint unchanged since last run (cache hit), (c) score
  gap already far exceeds `MIN_SCORE_DIFFERENCE`. Analyse only the small set where
  fresh metadata could flip the decision.
- **DELIVERED (fast-path b):** opt-in `PASS0_FINGERPRINT_CACHE` (default OFF).
  When on, an item whose files are byte-identical (`path|size|mtime_ns` sha256)
  to the last sane analyze skips the analyze+poll entirely — provably a no-op,
  since metadata is derived deterministically from those bytes. Cache lives at
  `plans/pass0_fingerprints.json` (delete to invalidate, e.g. after a Plex agent
  upgrade). Any cache error falls back to a live analyze. Expected impact on the
  743-group production run: ~740 fast-pathed → discovery drops from ~74 min of
  pure Pass0 polling to seconds. Zero behaviour change while the flag is off.

### Hardening batch (post-audit, DELIVERED — disjoint core files)
- **CTO-13 · dead/expired lock breaking** (`core/locks.py`): the lock records its
  owner (pid+ts) in `owner.json`. A later run breaks a lock held by a dead PID
  (POSIX liveness) or one past `ttl_seconds` (default 6 h), so a SIGKILL/OOM no
  longer wedges every future cron run into a silent "already running" no-op.
- **CTO-15 · failures persisted** (`run.py`): `RunReport.failures = result.failures`
  so a failing module's structured failures land in the report JSON, not just the log.
- **CTO-16 · path containment** (`core/fs.py`): `_ensure_within` asserts quarantine
  destinations and restore sources resolve inside `quarantine_dir` (rejects tampered
  sidecars / traversal); plus a uuid suffix removes same-millisecond name collisions.
- **CTO-17 · safe query API** (`core/cache.py`): `query_by(column, op, value)` validates
  column/op against allowlists and always parametrises the value; `query()` documented
  as literals-only.

### Hardening batch 2 (low-ROI safe items, DELIVERED)
- **A3-04 · HTTP URL validation** (`integrations/_net.require_http_url`): Arr/Qbit/Tmdb
  clients reject non-`http(s)` or malformed base URLs at construction (fails closed
  against `file://`/typos). Defense-in-depth; URLs still come from operator config.
- **CTO-19 · probe isolation** (`media_integrity._safe_probe`): a single unreadable
  file can no longer abort the whole scan or lose cache progress — it is reported as a
  non-decoding probe and flagged by the verdict engine.
- **CTO-18 · report/plan retention** (`plex_dupefinder._prune_old_files`): opt-in
  `PLAN_REPORT_RETENTION` (default 0 = keep all) caps `dupefinder_report_*`/`plan_*`
  JSON to the newest N. These are the tool's own outputs (outside INVARIANT I1).
- **CTO-20 · docs** : README now leads with the `izumi` platform entrypoint (`run.py`)
  and how the dedupe engine sits behind it; notes that doc-listed future items may not
  exist yet (source of truth = registered modules).

### IMP-12 (DELIVERED) · organizer — AI rename + relocate (post-dedupe tidy)
- **Problem:** after dedupe, the unsorted "manuales" dump still holds hand-downloaded
  media with arbitrary names plus junk sidecars/zero-byte files/empty dirs. Filename
  regex is too brittle to identify these.
- **Delivered:** new module `modules/media/organizer.py`, run via `run.py organizer`,
  with three safe-by-default stages:
  - **CLEANUP** (acts, honours `DRY_RUN`): `.nfo`/`.txt`/`.url` sidecars, zero-byte files
    and empty directories are **quarantined** via `core/fs` (INVARIANT I1 — never deleted,
    auto-purged after retention).
  - **IDENTIFY** (read-only): each media file is sent to Google Gemini
    (`gemini-2.0-flash`, batched `batch_size`); the model returns structured
    `type`/`title`/`year`/`season`/`episode`/`confidence`. Results are turned into
    canonical target paths under `paths.movies_root` / `paths.series_root` and written
    to `reports/organizer/plan.{json,md}`, split `confident` (>= `confidence_threshold`)
    vs `needs_review`.
  - **APPLY** (opt-in, report-only by default): with `integrations.gemini.apply=true`,
    confident + resolvable media is moved via the new sanctioned mover
    `core/fs.relocate(src, dest, *, reason)` — never overwrites (raises `SafetyError`),
    never deletes. Off until a parity window (like `modules/arr/orphans.py`).
- **Decision:** Gemini free-tier identification with model self-reported confidence as
  the gate (no external metadata-verification step), and `core/fs.relocate` as the
  second sanctioned mover — see [ADR-0012](adr/0012-organizer-gemini-identify-and-relocate.md).
- **Config + secret:** `paths.organizer_source` / `paths.movies_root` /
  `paths.series_root`; `integrations.gemini.{api_key_ref,model,batch_size,confidence_threshold,apply}`;
  `GEMINI_API_KEY` secret via `core/secrets` (ENV > `.env`). Full reference + example in
  [CONFIGURATION.md](../CONFIGURATION.md#organizer-module-runpy-organizer). Example
  pipeline `organize-manuales` in `pipelines.yaml`.
- **Follow-ups:** see `BACKLOG.md` (TMDb/TVmaze verification of Gemini titles; flip
  `apply` after parity).
