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

## Nightly AI report → Telegram (push)

The `notifypush` module gathers the freshest `summary.md` of every read-only
module and pushes a single digest to Telegram. Run the health/AI sweep first,
then `notifypush` last. Prereqs (one-time):
- `config.json`: `notify.enabled = true` (gates the cron send) and the DB list
  under `integrations.dbcheck` / `.dbrepair`.
- `.env` (repo root, git-ignored): `IZUMI_TELEGRAM_BOT_TOKEN` + `IZUMI_TELEGRAM_CHAT_ID`.

**Unraid User Script** (Settings → User Scripts → *Add New Script*; schedule
`0 3 * * *`). Adjust `REPO` to where the repo is checked out:

```bash
#!/bin/bash
REPO=/mnt/cache/appdata/izumi/plex_dupefinder   # <-- adjust to your checkout
IMG=izumi-organizer:local
cd "$REPO" || exit 1
run(){ docker run --rm -v "$REPO:/app" -v /mnt/user:/mnt/user -v /mnt/cache:/mnt/cache -w /app "$@"; }
sock=(-v /var/run/docker.sock:/var/run/docker.sock)

# SMART disk health needs a privileged container with /dev (kept separate).
run --privileged -v /dev:/dev "$IMG" python run.py diskwatch
# Optional AI passes (need Ollama reachable) — include in the digest if you run them:
run "${sock[@]}" "$IMG" python run.py logwatch
run "$IMG" python run.py analyst

# The 'nightly' pipeline runs the read-only sweep (uptime, dbcheck, permsdoctor,
# backupaudit, netdoctor) and then notifypush, in one container. IZUMI_MODE=live
# (+ notify.enabled in config.json) makes notifypush actually send.
run -e IZUMI_MODE=live "${sock[@]}" "$IMG" python run.py nightly
```

`notifypush` only reads reports + sends (never moves/deletes); a missing token,
disabled notify, or a Telegram failure is recorded in its report, never fatal.
An on-demand send is also available from the SSH menu ("Enviar informe ahora por
Telegram") and the bot (`/informe`), which force LIVE + notify for that one run.

## Hourly panel refresh (self-updating dashboard + growth control)

The web UI (`webui.py`) never runs modules on a timer — it only serves the report
files and re-runs a module on a button press. To keep every health card fresh
**at least hourly**, schedule the `hourly` pipeline (see `pipelines.yaml`) from a
User Script. It runs the read-only sweep + `configcheck` + the doctors, prunes the
metrics store (`retention`), regenerates `index.html` (`webdashboard`), and pushes
to Telegram **only when something is failing** (`notify.level = fail`).

**Unraid User Script** (Settings → User Scripts; schedule `0 * * * *`):

```bash
#!/bin/bash
REPO=/mnt/cache/appdata/scripts/plex_dupefinder   # <-- your checkout
IMG=izumi-organizer:local
docker run --rm -e IZUMI_MODE=live \
  -v "$REPO:/app" -v /mnt/user:/mnt/user -v /mnt/cache:/mnt/cache \
  -v /var/run/docker.sock:/var/run/docker.sock -w /app \
  "$IMG" python run.py hourly
```

Prereqs in `config.json`: `notify.enabled = true`, `notify.level = "fail"` (only
alert on trouble), and optionally tune `integrations.retention`:

```json
"integrations": {
  "retention": { "metrics_days": 90, "reports_max_mb": 500, "logs_max_mb": 100 }
}
```

`metrics_days` bounds `reports/cache/metrics.db` (the one store that grows one row
per module per run); `retention` also reports the `reports/` and `logs/` footprint
and flags a capacity failure if either exceeds its cap. izumi's own logs are already
size-capped by the rotating handler (`logging.rotate_mb` × `logging.backups`).

## Docker container logs — cap ALL containers (host-level, one-time)

izumi's `retention` bounds *izumi's* footprint, but the biggest disk eater on a
homelab is usually the **json log of every OTHER container** (`/var/lib/docker/
containers/*/*-json.log`), which by default grows unbounded. Fix it once at the
Docker daemon so every container rotates automatically:

`/etc/docker/daemon.json` (Unraid: persists via the Docker settings / `go` file):

```json
{
  "log-driver": "json-file",
  "log-opts": { "max-size": "10m", "max-file": "3" }
}
```

Then restart the Docker service. New/recreated containers inherit a 30 MB ceiling
each. For already-running containers, recreate them (or set the same `--log-opt`
per container) to apply. This is the robust, global control; an izumi read-only
*audit* of per-container log sizes can be added later as its own module.

## Migration note
During shadow (Phase P3–P6), legacy User Scripts and the new pipelines run in
parallel; the new ones are report-only. Legacy entries are **disabled, not
deleted**, at cutover and removed only in Sprint 6 / Phase P7.
