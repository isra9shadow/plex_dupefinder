# SCHEDULE.md — what runs, when (the hidden dependency)

> The real scheduler lives in **Unraid User Scripts**, not in any repo. This
> document inventories the legacy schedule and defines the unified target one.
> **Action (STORY IZ-010):** confirm the "current" rows against your box — the
> cadences below are *inferred from script design* and marked ⚠️ until verified.

## How to inventory the current schedule (run on Unraid)
- User Scripts UI: **Settings → User Scripts** (schedule shown per script).
- On disk: `ls /boot/config/plugins/user.scripts/scripts/*/` and read each
  `script` + its `schedule` file.
- Cron (if used): `crontab -l` and `ls /etc/cron.*`.

## Current (legacy) — ⚠️ TO BE CONFIRMED
| Capability | Legacy artefact | Inferred cadence | Confirmed? |
|---|---|---|---|
| Media integrity / triage | `media_triage_daily.sh` | daily | ⚠️ |
| ARR DB guardian + dupefinder | `arr_db_repair.sh` (calls `run.sh`) | daily | ⚠️ |
| ARR orphan folders | `arr_orphans_auto.sh` | weekly | ⚠️ |
| Stalled downloads | `MonitorizarDescargasAtascadas.sh` | hourly | ⚠️ |
| Disk move / balance | `mover_media.sh`, `move_downloads_balance.sh` | daily | ⚠️ |
| Share rebalance | `share_rebalance_v2.sh` | manual / weekly | ⚠️ |
| Disk-full daemon (decompress/cleanup/rebalance) | `unraid_telegram_bot` daemon | continuous | ⚠️ |
| Plex dedupe | `plex_dupefinder` via `run.sh` | weekly (within guardian) | ⚠️ |

> Note: the izumi orchestrator and the bot daemon overlap several of these — part
> of why consolidation matters. Capture the *actual* triggers before cutover.

## Target (unified) — after migration
A **single Unraid User Script** calls `deploy/run.sh <pipeline>`; ordering is
declarative in `pipelines.yaml`. Proposed cadence (tune later):

| Pipeline | Modules (order) | Cadence |
|---|---|---|
| `monitor` | `downloads_watchdog` → `disk_monitor` → `health` | hourly |
| `daily` | `media_integrity` → `arr_orphans` → `housekeeping` | daily (off-peak) |
| `weekly` | `plex_dupefinder` → `arr_db` | weekly |

`run.py health` can be wired to a lightweight check between pipelines. All
destructive modules start in report-only and are switched on per
`MIGRATION_PLAN.md` only after parity.

## Migration note
During shadow (Phase P3–P6), legacy User Scripts and the new pipelines run in
parallel; the new ones are report-only. Legacy entries are **disabled, not
deleted**, at cutover and removed only in Sprint 6 / Phase P7.
