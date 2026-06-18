# Changelog

All notable changes to the platform. Dates are when work landed; v0.1.0 is the
first functional release of the new architecture on the proven dedupe engine.

## [0.1.0] — unreleased (functional platform)

### Added — platform core
- Modular Python platform: `core/` (config, logging, locks, safety, fs/quarantine,
  notify, report, secrets, cache, registry, types/errors) frozen in `core/CONTRACT.md`.
- Single CLI `run.py <module | pipeline | health>`; modules self-register; pipelines
  are declared in `config.json` (`monitor` / `daily` / `weekly`).
- Layered, typed configuration (defaults → config.json → ENV); secrets via `.env`
  (`core/secrets`, fail-closed) — no secrets in source.
- Safety framework: DRY_RUN by default, AUDIT/LIVE modes, stability + min-age +
  size-ratio guards. `core/fs` is the ONLY mover/deleter → move-to-quarantine with a
  restore sidecar; the single real delete is the audited retention purge.
- Structured JSON logging (rotated, per-run `run_id`) and one JSON report per run.

### Added — integrated engine & modules
- **Plex DupeFinder integrated behind the new contracts** (subprocess wrapper,
  ADR-0011) — no behaviour change to the proven engine; runnable via `run.py plex_dupefinder`.
- Read-only inventory/health: `docker_inventory`, `disk_inventory` (+ SMART).
- Report-only legacy migrations: `arr_orphans`, `media_integrity`, `downloads_watchdog`.
- Host access through `adapters/` (single audited subprocess boundary); typed
  integration clients (Radarr, Sonarr, qBittorrent, TMDb).
- Persistent cross-run cache for ffprobe results (`core/cache`, IMP-06 v1).

### Added — release & quality
- `deploy/run.sh` (git fast-forward auto-update + run) and an Unraid User Script.
- CI: ruff + `mypy --strict` + pytest + coverage gate + secret scan; ~94% coverage.

### Security
- Legacy `scripts` repo put under git with secrets relocated to `.env` (same
  credentials, scrubbed from history); platform repo history verified clean.

### Notes
- Destructive module actions remain OFF (report-only) until per-module parity.
  V0.2 = performance (SQLite cache, parallel discovery, stable fingerprints, −70%
  discovery time); V0.3 = promote modules to acting + migrate the rest.
