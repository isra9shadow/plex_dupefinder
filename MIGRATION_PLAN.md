# MIGRATION_PLAN.md — legacy ecosystem → `izumi` platform

> How we move from the legacy `scripts/` repo (flat bash + `izumi-orchestrator` +
> `unraid_telegram_bot`) to the consolidated Python platform **without a
> big-bang and with a reversible step at every stage.** Pairs with `BACKLOG.md`
> (EPIC-010) and `ARCHITECTURE.md`.

---

## Contents
1. Principles
2. Migration phases
3. Shadow & parity protocol (per module)
4. Cutover order (lowest blast-radius first)
5. Mapping (legacy → target)
6. Rollback strategy
7. Decommission checklist
8. Third repo: consolidating `homelab-infra` (8.1 parts · 8.2 steps · 8.3 rollback · 8.4 naming DEFERRED · 8.5 simplification)

---

## 1. Principles

- **Nothing destructive is migrated "live".** Each module first runs **report-only
  in shadow**, diffed against the legacy script, before it is allowed to act.
- **Legacy is never deleted until its replacement is ≥ 2 weeks green.**
- **Every step is reversible** (see §6 Rollback).
- **The scheduler is the hidden dependency.** What runs and when lives in Unraid
  User Scripts, not in the repo → we inventory it first (`SCHEDULE.md`, STORY-064).

---

## 2. Migration phases

| Phase | What | Backlog | Reversible by |
|---|---|---|---|
| **P0 · Safety net** | `git init` legacy + `.gitignore` + commit; rotate all exposed secrets; inventory Unraid User Scripts | S005, S006, S064 | n/a (purely additive) |
| **P1 · Platform spine** | Build `core/*`, freeze API (FREEZE-2), wire dupefinder onto it | EPIC-001 | dupefinder keeps working on old path until S017b merged |
| **P2 · Notifier first** | Single `core/notify`; retire 3 Telegram copies | S019, S020 | re-point legacy to its own notifier (still present) |
| **P3 · Read-only modules** | `media_integrity`, `arr_orphans` in **report-only**; diff vs legacy | EPIC-004, S027a | legacy script still the actor; new one only reports |
| **P4 · Storage ops (dangerous)** | `disk_balance`, `share_rebalance`, `decompress`, `cleanup`, `series_blacklist` — all via `core/fs` (quarantine), kill `chmod 777` | EPIC-006 | quarantine restore + legacy still available |
| **P5 · ARR + downloads** | `arr_db`, `perms`, `downloads_watchdog` via safe docker/adapters | EPIC-003, EPIC-005 | legacy `arr_db_repair.sh` kept as fallback one cycle |
| **P6 · Cutover** | Per module: ≥2-week parity → switch actions on; legacy disabled (not deleted) | S065, S066 | flip module back to report-only; re-enable legacy User Script |
| **P7 · Decommission** | Delete 219 MB embedded copy + retire legacy scripts; single Unraid User Script | S067, S063 | git revert; legacy recoverable from its own git history (created in P0) |

---

## 3. Shadow & parity protocol (per module)

1. Module ships **report-only** (no `core/fs` actions, just a report).
2. It runs on the same schedule/inputs as the legacy script.
3. `tools/shadow_diff.py` (S058) compares the new module's intended actions vs
   the legacy script's actual actions and records divergences.
4. **Parity gate (S066):** the module is eligible for cutover only after **≥ 2
   weeks** with zero unexplained divergences on real data.
5. Cutover flips one config flag (`DRY_RUN=false` for that module). The legacy
   User Script for that capability is **disabled**, not deleted.
6. After **2 more weeks green**, the legacy script is decommissioned (P7).

This window is **wall-clock and not compressible** — it is the true floor of the
project calendar (see `BACKLOG.md` critical path).

---

## 4. Cutover order (lowest blast-radius first)

`notify → media_integrity (report) → arr_orphans (report) → decompress → cleanup
→ disk_balance → share_rebalance → series_blacklist → downloads_watchdog →
perms → arr_db`.

Rationale: notifications are harmless; read-only verdict modules next; then
storage moves (recoverable via quarantine); DB repair last (highest blast radius,
keep the proven legacy guardian as fallback the longest).

---

## 5. Mapping (legacy → target)

| Legacy artefact | Target | Action |
|---|---|---|
| `media_triage_daily.sh` | `modules/media_integrity` | rewrite |
| `arr_orphans_auto.sh`, `radarr_noMapeadas.sh` | `modules/arr_orphans` | rewrite + merge (drop radarr_noMapeadas) |
| `arr_db_repair.sh` (DB), `restaurar_db.sh` | `modules/arr_db` | rewrite + merge |
| `arr_db_repair.sh` (perms/symlinks) | `modules/perms` | rewrite |
| `MonitorizarDescargasAtascadas.sh`, `downloads.py` | `modules/downloads_watchdog` | rewrite + merge |
| `move_downloads_balance.sh`, `move_files.py` | `modules/disk_balance` | merge → one impl |
| `share_rebalance_v2.sh`, `rebalance_disks.py` | `modules/share_rebalance` | merge (drop v1) |
| `mover_media.sh` (rar / junk / rm-series / 777) | `decompress` + `cleanup` + `series_blacklist` | decompose; kill 777 |
| `telegram_notify.sh`, izumi `telegram.sh`, bot `telegram.py` | `core/notify` | merge → one notifier |
| `disks_map_manual.json` | `config/disk_map.json` | move |
| `izumi-orchestrator/legacy/**` (incl. 219 MB plex_dupefinder copy) | — | delete (P7) |
| `filebotOrganiza.sh` | `adapters/bash/filebot.sh` | keep (test-mode) |
| `test.sh`, `config_old_plex_dupefinder.json`, `deletefiles.sh`, `cookies.txt`, committed logs | — | delete |

---

## 6. Rollback strategy

Rollback is defined per failure class. Every one is bounded and pre-planned.

| Failure class | Detection | Rollback action | RTO |
|---|---|---|---|
| **Bad commit / regression** | CI red, or smoke/health fail post-deploy | `git revert` the PR; `deploy/run.sh` auto-update picks previous good HEAD | minutes |
| **A module deleted/moved wrongly** | Report / health / user notices missing file | Restore from the **quarantine sidecar** `restore_command`; file is *moved*, never deleted, within `QUARANTINE_RETENTION_DAYS` | minutes |
| **ARR DB repair made it worse** | `arr_db` integrity gate fails post-swap | `modules/arr_db` keeps timestamped backup; swap back the pre-repair DB; container stays stopped on failure | < 1 h |
| **Disk balance/rebalance error** | shadow diff / space anomaly / report | Quarantine restore; `rsync` was move-to-quarantine, not in-place delete; legacy script re-enable if needed | minutes |
| **Config/secret breakage** | startup fail-closed (secrets), schema validation error | Layered config: revert `config.json`/`.env`; defaults still safe (DRY_RUN) | minutes |
| **Cutover regret** | parity diff appears after switch | Flip module flag back to `DRY_RUN=true`; re-enable legacy User Script (still present until P7) | minutes |
| **Whole-platform abort** | systemic issue | Disable the single Unraid User Script; legacy User Scripts re-enabled (kept disabled, not deleted, until P7) | minutes |

**Rollback guarantees that hold by design:**
- Code is always `git`-recoverable (legacy too, after P0).
- Data is always quarantine-recoverable within the retention window (ADR-0002).
- DBs are always backup-recoverable (timestamped pre-swap copy).
- Config/secrets are layered and revertible; defaults are safe.
- Legacy stays runnable until 2 weeks after cutover (P7), so any module can fall
  back to its predecessor without a rebuild.

---

## 7. Decommission checklist (P7, per capability)

- [ ] New module ≥ 2 weeks green in production (acting, not just reporting).
- [ ] Legacy User Script disabled ≥ 2 weeks with no fallback needed.
- [ ] No references to the legacy script remain (grep).
- [ ] Quarantine reviewed; retention purge behaving as expected.
- [ ] `git rm` legacy artefact (recoverable via history); update `SCHEDULE.md`.
- [ ] Finally: delete the 219 MB `izumi-orchestrator/legacy/plex_dupefinder` copy.

---

## 8. Third repo: consolidating `homelab-infra`

`homelab-infra` is the declarative IaC + inventory + docs for the whole server.
It is **absorbed**, not migrated piecemeal — most of it is useful and authoritative.

### 8.1 What happens to each part
| `homelab-infra` part | Action | Lands in |
|---|---|---|
| `inventory/*` (hardware, system, servicios, docker, redis-db-allocations) | **Keep — promote to source of truth** | `inventory/` |
| `docs/` (arquitectura, red, ci-cd, servicios/) | **Keep — merge into platform docs** | `docs/` |
| `docker/jenkins/` (operativo) | **Keep, do not modify** | `infra/jenkins/` |
| `templates/php/` + `scripts/bootstrap-project.sh` | **Keep** (project scaffolding) | `templates/` |
| `docker/{infra,media,monitoring}` (described but NOT yet in git) | **Materialise** during Phase D | `infra/` |
| `_default` auto networks (inventory "Pendientes") | **Remove** (declare proxy/backend/monitoring) | — |
| Server-specific stale notes | **Reconcile** against live `inventory` | — |
| Duplicated `CLAUDE.md`/`README.md` | **Merge** into the single root governance docs | root |

### 8.2 Consolidation steps (low-risk, reversible)
1. `git init` is already present on `homelab-infra`; create a snapshot tag first.
2. Bring its tree in under `infra/ inventory/ templates/ docs/` via `git
   subtree`/copy + commit (history preserved or referenced). No content rewrite.
3. **Derive `config/disk_map.json` from `inventory/hardware.md`** — array disk1–5
   + cache pool `/mnt/cache`. Delete every disk6/disk7 / `/mnt/user/appdata`
   assumption from the platform config.
4. Reconcile conventions: PUID 99/PGID 100, `/mnt/cache/appdata`, `/mnt/cache/repos`.
5. Once green for a week, archive the standalone `homelab-infra` repo (read-only).

### 8.3 Rollback
Each step is a commit; `git revert` restores. `homelab-infra` stays as a tagged
read-only mirror until the consolidated repo has run ≥2 weeks, so nothing is lost.

### 8.5 Simplification supersedes a few §5 rows (ADR-0008)
- `move_downloads_balance.sh`, `move_files.py`, `share_rebalance_v2.sh`,
  `rebalance_disks.py` → **NOT** rewritten as movers. Replaced by read-only
  `modules/disk_monitor` (alert only); Unraid's mover handles cache↔array.
- `arr_db_repair.sh` DB surgery → `modules/arr_db` restores from *arr's scheduled
  backup zip (no `.recover`/swap). `restaurar_db.sh` is the model.
- Dropped clients: Prowlarr, TVmaze (YAGNI — re-add when a module needs them).

### 8.4 Naming transition — DEFERRED (ADR-0007)
**Decision: do NOT rename yet.** plex_dupefinder is deployed, tested, in CI,
documented and in use. Never rename + refactor + migrate at once. Sequence:
`plex_dupefinder → modular platform → homelab ops → rename LAST`.
- When the platform is stable, rename GitHub `plex_dupefinder` → `izumi` (GitHub
  auto-redirects old URLs); update `deploy/run.sh` remote + the User Script path.
- The dedupe logic lives at `modules/media/plex_dupefinder.py` regardless of repo
  name. The rename is a final, isolated, cosmetic step — never bundled with logic.

---

## 9. Technical migration specs (legacy capability → module)

All ship **report-only first**, run in shadow vs the legacy script, and only gain
actions (always via `core/fs` → quarantine) after ≥2 weeks of parity. Each gets
unit + integration tests (mocked clients) + a regression/parity test.

### 9.1 `arr_orphans`  — from `arr_orphans_auto.sh` (+ `radarr_noMapeadas.sh`)
- **Target:** `modules/arr/orphans.py` · **Needs:** `integrations/radarr`, `integrations/sonarr`.
- **Logic:** read mapped top-level folders via ARR API → diff against on-disk
  top-level folders under the roots → orphans = on-disk minus mapped. Report list;
  after parity, move orphans to quarantine via `core/fs`.
- **Parity:** `shadow_diff` of the orphan set vs the legacy script on a fixture tree.
- **Risk:** 🔴 (moves folders) → quarantine only, never delete. Drops the duplicate
  `radarr_noMapeadas.sh` (subsumed).

### 9.2 `media_integrity` — from `media_triage_daily.sh`
- **Target:** `modules/media/media_integrity.py` · **Needs:** `adapters/ffprobe`, `integrations/tmdb`.
- **Logic:** probe each file (video/audio streams, duration, smoke decode) → compare
  actual vs expected runtime (TMDb, cached) → verdict OK/DUDABLE/MALO. Report;
  after parity, move BAD/DOUBTFUL to quarantine via `core/fs`.
- **Parity:** identical verdicts on a fixed sample set vs the legacy V8.5 script.
- **Risk:** 🔴. Secrets (TMDb) via `core/secrets`. Replaces the 1,422-line bash.

### 9.3 `downloads_watchdog` — from `MonitorizarDescargasAtascadas.sh`
- **Target:** `modules/downloads/watchdog.py` · **Needs:** `integrations/qbittorrent`, `integrations/radarr`, `integrations/sonarr`.
- **Logic:** detect stalledDL / inactive ≥48h → classify by category → report; after
  parity, ARR queue blocklist-remove (opt-in) or qBit `failed` tag.
- **Parity:** same candidate set vs legacy on a captured qBit listing.
- **Risk:** 🟡. qBit password via `core/secrets` (rotate the leaked one).

### 9.4 `disk_balance` — from `move_downloads_balance.sh` + `mover_media.sh` (disk part)
- **Target:** `modules/storage/disk_monitor.py` — **READ-ONLY alert, NOT a mover** (ADR-0008).
- **Rationale:** real topology = array disk1–5 + `/mnt/cache` NVMe pool; **Unraid's
  mover handles cache↔array**. The legacy SSD↔HDD/disk6-7 movers are obsolete and
  are **not reimplemented**. We only alert when a disk/pool crosses a fullness
  threshold (from `config/disk_map.json`).
- **Risk:** 🟢 (no moves). Eliminates two of the riskiest legacy scripts.

### 9.5 `share_rebalance` — from `share_rebalance_v2.sh`
- **Target:** folded into `disk_monitor` as an **informational** check (misplaced
  shares vs `/boot/config/shares/*.cfg`); **not a mover** (Unraid handles allocation).
- **Risk:** 🟢. Not reimplemented as an rsync mover.

> Net: 6 legacy scripts → 3 modules (`arr/orphans`, `media/media_integrity`,
> `downloads/watchdog`) + 1 read-only `storage/disk_monitor`. The dangerous movers
> and the duplicate orphan script are retired, not ported.
