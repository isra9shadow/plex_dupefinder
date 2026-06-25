# AI_CONTEXT.md — start here if you are an AI agent

> One-page mental model so a fresh agent can be productive without reading the
> whole codebase. **Read order:** this file → `docs/INVARIANTS.md` → `AGENTS.md`
> → your story in `BACKLOG.md` → `core/CONTRACT.md` (when published).

---

## What this is

`izumi`: a single modular **Python** platform that runs an Unraid homelab
(Plex, Radarr/Sonarr, qBittorrent, disks) 24/7. Built on the mature
`plex_dupefinder` codebase, which is now just one module. We are replacing an old
ecosystem that **triplicated** dangerous logic; the target is **1 repo · 1 config
· 1 logging · 1 reporting · 1 pipeline · 1 safety framework · N modules**.

## The 5 rules you must never break

1. **No direct deletion.** No `rm`, `os.remove`, `shutil.rmtree`, `Path.unlink`,
   `find -delete`, `rsync --remove-source-files` in modules. Everything
   destructive/moving goes through **`core/fs` → quarantine + restore sidecar**.
2. **Safe by default.** New modules default `DRY_RUN = True`.
3. **No secrets in code.** Use `core/secrets` (ENV/`.env`). Never hardcode/commit.
4. **One of each:** `core/config`, `core/logging`, `core/locks`, `core/safety`,
   `core/notify`. Never build a second.
5. **Python-first.** Bash only in `adapters/bash/` (Unraid wrappers).

## Where things are

- `run.py` — single CLI entrypoint. `pipelines.yaml` — job order.
- `core/` — the spine (config, logging, locks, safety, fs, notify, secrets,
  errors, types, metrics, health). Public API frozen in `core/CONTRACT.md`.
- `modules/` — one job each, never import each other.
- `integrations/` — typed API clients (plex, radarr, sonarr, qbittorrent, tmdb,
  **gemini**, **ollama**). `_net.require_http_url` guards every base URL.
- `adapters/` — the audited host-command boundary (the ONLY place that may call
  `subprocess`): `command` (core runner), `ffprobe`, `docker`, `archive` (unar),
  `smart`, `blockdev`.
- `adapters/bash/` — the only bash (newperms, filebot).
- `config/` config.json + disk_map.json · `.env` secrets · `logs/ reports/ quarantine/`.
- `tests/` unit/integration/security/smoke/regression · `docs/adr/` decisions.

## How a module behaves

Receives a `Context` (config, logger, fs, notify, dry_run, run_id). Exposes
`run(ctx) -> ModuleResult`. Is idempotent, single-instance-locked, report-only
capable, emits a JSON report fragment + metrics. Never imports another module.

## Media-AI modules (the current focus — read these before touching them)

The post-dedupe media pipeline, all driven from `menu.py` (operator TUI, runs the
izumi entrypoints inside the `izumi-organizer:local` Docker image — host Python is
3.9, the platform needs 3.11+):

- **`modules/media/organizer.py`** — cleans junk → quarantine, then identifies the
  "Manuales" dump and (opt-in `integrations.gemini.apply`) relocates files to
  Radarr/Sonarr-style paths via `core/fs.relocate`. **Identification cascade**:
  local regex `parse_media_filename` → AI providers in `integrations.ai.providers`
  order (`["ollama","gemini"]` recommended; Ollama = local RTX 4060, free; Gemini
  = quota-limited fallback). An AI answer below `integrations.ai.escalate_below`
  is kept but re-asked to the next provider (low-conf Ollama → Gemini); best wins.
  Files are enriched with ffprobe hints; the prompt de-obfuscates leetspeak names.
  ASCII-only targets, episode titles, tmdbid/tvdbid. Largest files first.
- **`modules/media/extractor.py`** (`adapters/archive.py`, `unar`) — extracts
  finished zip/rar/7z (incl. multi-volume); on success the archive set is
  **quarantined** (never `rm`). Skips incomplete `.part` downloads.
- **`modules/ops/logwatch.py`** (`adapters/docker.py`, `OllamaClient.complete`) —
  reads `docker logs`, extracts error lines, Ollama writes a Spanish summary.
- **`modules/ops/analyst.py`** — reads `reports/organizer/plan.json`, explains
  with Ollama WHY files landed in `needs_review` and what to do. Read-only.

The two AI clients share `identify(paths, *, errors=None)`; `OllamaClient` also has
`complete(prompt)` for free-form text. Both reuse `gemini.build_prompt`. CI is
exact-pinned: **ruff 0.6.9 + mypy 1.11.2** (validate before pushing).

## The safety gauntlet (applied before any action)

DRY_RUN/AUDIT → MIN_FILE_AGE → STABILITY_CHECK (size re-read) → MAX_SIZE_RATIO →
**move to quarantine (never delete)** → audited retention purge later.

## Parallelisation contract (why you can work alone safely)

Work is split so modules don't touch shared files. The synchronisation points are
three freezes: **FREEZE-1** = `AGENTS.md` (conventions), **FREEZE-2** =
`core/CONTRACT.md` (core API — no module starts before it), **FREEZE-3** =
`integrations/*` interfaces (mock against them). Don't renegotiate a freeze in
code — write an ADR.

## Glossary

- **Quarantine** — `quarantine/` dir; files are *moved* here, not deleted, with a
  `.dupefinder_meta.json`/sidecar holding a `restore_command`.
- **Shadow / parity** — running legacy and new side-by-side in report-only and
  diffing (`tools/shadow_diff.py`) before cutover.
- **Context** — the per-run object injected into modules.
- **run_id** — unique id stamped on every log line and report for one execution.

## When unsure

Prefer the rule in `AGENTS.md`. If a rule blocks you, **stop and open an ADR** in
`docs/adr/` rather than working around it. Never delete legacy files or commit/push
unless the human asks.
