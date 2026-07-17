# ROADMAP.md — `izumi-ops` homelab platform

> Single roadmap across the **three** repos being consolidated into one:
> `scripts` (legacy automation), `plex_dupefinder` (this repo → the platform
> spine), and `homelab-infra` (declarative IaC + inventory + docs). Target: **one
> repository that fully describes and operates the Unraid homelab.**

## North star
One repo · one config · one logging · one reporting · one pipeline · one safety
framework · one inventory (source of truth) · N modules + N declarative stacks.
GitOps-simple for a **single operator**: everything in git, nothing only in the
Unraid UI; secrets out of git; reproducible deploy.

---

## Phases

### ✅ Phase 0 — Governance & Sprint 0 (DONE)
AGENTS/CLAUDE/AI_CONTEXT/INVARIANTS/ADRs, branch model, `core/{errors,types,secrets}`,
tooling (ruff/mypy-strict/pytest/coverage), CI, security gate. CI green, cov 98%.

### 🔜 Phase A — Consolidation & correction (NEXT — unblocks correct code)
Absorb `homelab-infra` and correct the platform config from the real inventory.
- Rename repo → **`izumi-ops`** (see ARCHITECTURE §Naming; `plex_dupefinder` → a module).
- Import `homelab-infra` into `infra/ inventory/ templates/ docs/` (no rewrites; move).
- **Correct topology:** `config/disk_map.json` derived from `inventory/hardware.md`
  → array = disk1–5, cache pool = `/mnt/cache` (2× NVMe). **Remove disk6/disk7.**
- **Correct paths:** runtime data under `/mnt/cache/appdata/izumi-ops`; repos under
  `/mnt/cache/repos`; quarantine on the array near media.
- `git init` legacy `scripts/` + rotate secrets (Sprint 0 carry-over).
- DoD: one repo builds; inventory is the only place disks/shares/paths are defined;
  `config.json` references inventory; no `/mnt/user/appdata`, `izumi` (only `izumi-ops`), or disk6/7 anywhere.

### Phase B — Sprint 1: Core Platform (FREEZE-2)
`core/{config,logging,locks,safety,fs,notify,metrics,report,health,docker,
pipelines,context}` + `run.py` + `pipelines.yaml` + integrations clients + tests.
FREEZE-2 published; 10-agent parallel build (BACKLOG FASE 4). ~1.5–2 days.

### Phase C — Sprint 2–5: Modules & migration (report-only → parity)
media_integrity, arr_orphans, arr_db (**restore-from-backup, not DB surgery**),
perms, downloads_watchdog, housekeeping (decompress/cleanup/series_blacklist), and
**`disk_monitor`** (read-only fullness alert) which **replaces** the old
disk_balance/share_rebalance — Unraid's mover handles cache↔array (ADR-0008). Each
ships report-only, runs in shadow vs legacy, cuts over after ≥2-week parity.

### Phase D — Sprint 6: Legacy & infra completion
Decommission `scripts/` (incl. 219 MB embedded copy). Complete the **aspirational**
parts of `homelab-infra`: materialise the missing `infra/{infra,media,monitoring}`
compose stacks in git, remove auto-generated `_default` networks (inventory
"Pendientes"). Single Unraid User Script drives the platform.

---

## Critical-path corrections this roadmap introduces
1. **Storage modules must be redesigned** around `disk1–5 array + /mnt/cache pool`
   + Unraid mover — the legacy SSD↔HDD/disk6-7 model is obsolete (data-move risk).
2. **Inventory is the single source of truth** for disks/shares/services; the
   platform reads it, never hardcodes.
3. **Naming**: `plex_dupefinder` no longer describes the scope → rename to `izumi-ops`.

## Milestones (calendar, solo operator, 6–8 agents)
| Milestone | When |
|---|---|
| Phase A consolidation + config correction | ~2–3 days |
| Phase B Sprint 1 (Core, FREEZE-2) | +~2 days |
| Phase C modules report-only + shadow | +~1 week |
| Parity windows (wall-clock, overlapping) | +~2 weeks |
| Phase D cutover + legacy/infra completion | +~3 days |
| **Stable single-repo production** | **~4–5 weeks** |

---

## Release plan (incremental — value over perfection)

**V0.1 — Functional platform on the current dedupe engine (SHIP FIRST).** The pieces
are essentially DONE: new architecture ✅ · core platform ✅ · centralised config ✅ ·
centralised logging ✅ · safety framework (safety + fs/quarantine) ✅ · Plex DupeFinder
integrated behind the new contracts (wrapper, ADR-0011) ✅ · quarantine ✅ · reporting ✅ ·
critical tests ✅. **The only remaining gap is release engineering:** `deploy/run.sh` +
Unraid User Script · platform config sample + pipelines · CHANGELOG/tag `v0.1.0`.

**V0.2 — Performance & observability.** Order is fixed (discovery first): IMP-03 profile →
IMP-06 SQLite cache → IMP-04 parallel discovery → IMP-05 stable fingerprints → IMP-11 Pass0
fast-path → quarantine/confidence/recoverable metrics. **Target: −70% discovery time.**

**V0.3 — Module migration.** Promote the report-only modules (arr_orphans, media_integrity,
downloads_watchdog) to acting after parity + arr_db, disk_monitor, housekeeping, dashboard.

Principle: ship V0.1 on the proven engine behind the new contracts; never rewrite what works.
