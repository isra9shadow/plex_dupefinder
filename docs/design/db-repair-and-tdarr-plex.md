# Design: DB Auto-Repair (operator-confirmed) and tdarr→Plex False-Duplicate Targeted Refresh

# Design — two future izumi features

Grounded in the current code:
- `aictx/apply.py` (commit `fbc986a`): positive allow-list `classify()` (regex + `aictx.guard.vet_command` + no shell metachars), `ApplyAction`, `extract_actions`/`collect_actions`/`load_actions_from_file`, `apply_action()` (re-classify defense-in-depth, injected `Runner`, `default_runner` → `adapters/command`), `finding_fingerprint()`. Front-ends `menu.py` (`y/N` per command) and `bot.py` (`/apply` inline buttons under the single-execution lock) consume the module `plan.json` `diagnosis.findings[].unraid_commands`.
- `core/fs.py` `Fs`: the ONLY mover. `quarantine(path, reason=)` MOVES to quarantine + writes `.izumi.json` restore sidecar, honours `dry_run`; `relocate()`, `restore(entry)`, `purge()` (sole real delete, retention-gated). INVARIANT I1 = never raw-delete.
- Module pattern: `@register("name")` (`core/registry.py`), `run(ctx: RunContext) -> ModuleResult`, writes `reporting.dir/<module>/plan.json` (+ `summary.md`); read-only modules never import each other; incident memory via `SqliteCache.record_incident`/`recent_incidents` keyed on `finding_fingerprint`.
- `adapters/docker.py` is read-only introspection only (`list_containers`, `logs`, `probe`); container lifecycle (`docker stop/start/restart <name>`) is reachable ONLY through the apply allow-list category `docker-lifecycle`.
- Targeted Plex refresh (branch `feature/IZ-targeted-plex-refresh`, commit `0d50449`): `refresh_plex_targets(targets)` partial-scans only `(section_name, plex_folder)` via `section.update(path=folder)` using Plex `locations` (no PATH_MAPPINGS); `PLEX_REFRESH_SCOPE='item'|'library'`. `refresh_plex_item(item, timeout)` = `item.analyze()` + poll for `sane_and_changed`/`sane_unchanged`/`timeout`/`analyze_failed`.
- Dupefinder safe-skip (`plex_dupefinder.py`): `has_sane_metadata()` → `select_keeper()` sets `skip_reason="video_duration <= 0"` / `"candidate %r has invalid metadata: %s (Plex analysis may be incomplete)"`. These skips are surfaced by `modules/ops/analyst.py` (`aggregate_skips`, normalized buckets).
- `integrations/arr.py` `ArrClient` (v3, injectable fetcher); `integrations/radarr.py`, `sonarr.py`.

---

## DESIGN 1 — DB auto-repair (Plex / Sonarr / Radarr SQLite corruption)

### Goal & invariants
Detect SQLite corruption in app DBs, and — only on explicit operator confirmation — repair it SAFELY: always snapshot to quarantine first, prefer the app's own native backup, fall back to a `.recover` rebuild, never raw-delete, fully idempotent, reuse the existing apply confirmation UX.

Hard rules:
- I1: no raw delete/`os.remove`/`rmtree`. The corrupt DB is **moved to quarantine via `core/fs`** (which itself moves, never deletes) before anything is touched. The "swap in repaired DB" step moves the live file to quarantine and moves the rebuilt file into place — both are `Fs` moves.
- DRY_RUN default True (new module).
- Container stop/start go through the **existing apply allow-list** (`docker-lifecycle`), not new privileged code. No new `subprocess` site — everything routes through `adapters/command`.
- Secrets only via `core/secrets`.

### Two cooperating modules (read-only detector + confirmed executor)
Mirrors the existing analyst→apply split so we don't put destructive logic behind an LLM.

**Module A — `modules/ops/dbcheck.py` (detector, `@register("dbcheck")`, READ-ONLY).**
- Iterates a configured list of app DBs. For each, runs `PRAGMA integrity_check` (and `PRAGMA quick_check` as a fast pre-pass) **against a temporary read-only copy**, never the live file, opened with `sqlite3` URI `file:...?mode=ro&immutable=1`. (Reusing the integrity detector concept; `modules/media/media_integrity.py` is unrelated media-runtime checking, so this is a new dedicated SQLite detector — named `dbcheck` to match the requested "dbcheck detector".)
- Emits `reporting.dir/dbcheck/plan.json` in the SAME shape the apply layer already understands:
  - `diagnosis.findings[]` each with `title` (stable, e.g. `"DB corruption: plex"` — numbers masked so `finding_fingerprint` is stable), `severity`, human `summary`, and a structured `db_repair` block (NOT `unraid_commands`, see below): `{app, db_path, container, integrity_status, native_backup_dir, native_backup_glob, recover_strategy}`.
- Records the finding into the incident cache (same `record_incident`/`finding_fingerprint` path) so a successful repair marks it resolved and the detector stops re-flagging.
- Strictly read-only: opens DBs `mode=ro`, writes only its own report. An unreachable DB/path is a `FailureRecord`, not a crash.

**Module B — `modules/ops/dbrepair.py` (executor, `@register("dbrepair")`, default `DRY_RUN=True`).**
- Reads `dbcheck/plan.json`, takes the operator-selected finding (by `fingerprint`), and runs the safe step sequence below. Writes `reporting.dir/dbrepair/plan.json` with a full per-step audit (`steps[]`: name, ok, detail, quarantine entry, rollback-done) + `summary.md`.
- Container lifecycle uses the apply allow-list: it builds `docker stop <name>` / `docker start <name>` `ApplyAction`s, calls `aictx.apply.classify` to prove they're allow-listed (`docker-lifecycle`), then `apply_action()` with `default_runner`. No new command path.
- All file moves go through `core/fs.Fs` (constructed with the run `dry_run`).

### Why not "just put repair commands in unraid_commands"
The existing allow-list deliberately rejects `rm`/`mv` and anything with metachars or pipes; a `.recover`/`.dump|reload` is a multi-step transaction, not a single literal argv, and must be transactional (snapshot → swap → verify → rollback). So repair is its own typed module with explicit rollback, and only the *container restart* piece reuses the allow-list. This keeps the "model never proposes a destructive shell string" property intact.

### Exact safe step sequence (idempotent, per finding)
For app `X` (db_path `D`, container `C`):
1. **Pre-flight / idempotency guard.** Re-run `integrity_check` (ro copy) on `D`. If `ok`, mark finding resolved and STOP (idempotent: re-running after a successful repair is a no-op). If a `dbrepair` lock or an in-progress marker exists, refuse (single-execution).
2. **Snapshot to quarantine (ALWAYS, before touching anything).** `Fs.quarantine(D, reason="dbrepair: pre-repair snapshot of corrupt <X> DB")` — and the same for `-wal`/`-shm` siblings if present. This MOVES the corrupt files out; the sidecar carries the exact `restore_command`. In `dry_run` this only plans the entry. Record the `QuarantineEntry` for rollback.
   - Subtlety: because quarantine *moves*, after this step the live path is gone; the repaired/restored DB is written to the original path next. (If we needed the original in place for `.recover`, we operate on the quarantined copy path from its sidecar — see step 4.)
3. **Stop the container** via allow-list `docker stop C` (apply_action). Verify stopped via `adapters/docker.list_containers()` (state != running). If it won't stop, ROLLBACK (step R) and abort. (Stopping after snapshot means the snapshot is of a possibly-live DB; we accept that — it is only a safety copy, never the repair source for the WAL-consistent path. For Plex/arr the integrity_check + recover runs on a checkpointed copy; we also issue `PRAGMA wal_checkpoint(TRUNCATE)` read attempt is skipped on corrupt DBs, so we rely on `.recover` which tolerates WAL.)
4. **Attempt repair — strategy order (first that succeeds wins):**
   - **(a) Restore native backup (preferred, lowest risk).** Resolve newest file in the app's own backup dir:
     - Sonarr/Radarr: `<config>/Backups/scheduled/*.zip` (contains `<app>.db`); unzip to a temp dir via `adapters/archive` (already exists, used by extractor) — no new unzip subprocess.
     - Plex: `.../Plug-in Support/Databases/` plus `com.plexapp.plugins.library.db-*` and the DatabaseBackups; pick newest `*.db` backup.
     - Run `integrity_check` on the candidate backup (ro). If `ok`, `Fs`-move it into the original `D` path (move into place; original is already in quarantine from step 2). Strategy = `native_backup`.
   - **(b) `.recover` rebuild.** Run sqlite `.recover` from the quarantined corrupt copy into a fresh temp DB (`sqlite3 <corrupt-copy> .recover | sqlite3 <fresh>` is a pipe — so we do NOT shell it; instead run via Python `sqlite3` module: open corrupt copy `mode=ro`, use the `Connection.iterdump()`/`.recover` equivalent — implemented in-process, no shell metachars, no pipe). On success, `integrity_check` the fresh DB; if `ok`, `Fs`-move fresh DB into `D`. Strategy = `recover`.
   - If both fail, ROLLBACK and abort with a `FailureRecord` (DB left untouched-equivalent: original still safe in quarantine, restorable by the printed `restore_command`).
5. **Restart the container** via allow-list `docker start C` (apply_action). Verify running via `list_containers()` and (optionally) an HTTP health probe through the existing integration client (`ArrClient.system_status()` / Plex reachability).
6. **Verify integrity post-repair.** `integrity_check` on the now-live `D` (ro copy) must be `ok`. If not → ROLLBACK.
7. **Report + close the loop.** Append all steps to `dbrepair/plan.json`, write `summary.md`, mark the incident resolved (`record_incident`/resolve via `finding_fingerprint`) so `dbcheck`/analyst stop re-flagging.

**Rollback (R), used at any failed step after the snapshot:**
- If a repaired/restored DB was already moved into `D`, `Fs.quarantine` it (label `dbrepair-failed`) and `Fs.restore(snapshot_entry)` the original corrupt DB back to `D` (so we end exactly where we started — no data invented, no data lost).
- Restart the container if we had stopped it (`docker start C`), so the app is never left down by a failed repair.
- All rollback moves are `core/fs` moves; never a delete.

### Config keys (`config.json`, under `integrations.dbrepair`)
```
integrations.dbrepair = {
  "dry_run": true,                  # module default true (I2)
  "verify_timeout_s": 30,
  "databases": [
    {"app":"plex",  "container":"plex",  "db_path":"/config/.../com.plexapp.plugins.library.db",
     "native_backup_glob":"/config/.../DatabaseBackups/*.db"},
    {"app":"sonarr","container":"sonarr","db_path":"/config/sonarr.db",
     "native_backup_glob":"/config/Backups/scheduled/*.zip"},
    {"app":"radarr","container":"radarr","db_path":"/config/radarr.db",
     "native_backup_glob":"/config/Backups/scheduled/*.zip"}
  ],
  "repair_strategy_order": ["native_backup","recover"],
  "require_health_after": true      # call ArrClient.system_status / Plex reachability post-start
}
```
No secrets here; any API key uses `core/secrets` refs already wired for arr/plex.

### Failure / rollback handling (summary)
- Snapshot fails → abort, nothing touched.
- Stop fails → rollback, abort, container left running.
- Both repair strategies fail → rollback, restart, `FailureRecord`, original safe in quarantine.
- Post-repair integrity not `ok` → rollback.
- Crash mid-run → idempotent re-run: step 1 detects either a healthy DB (resolved) or a leftover snapshot entry/marker and resumes/rolls back rather than double-snapshotting.

### Menu/bot integration (reuse existing apply confirmation)
- **Detector** runs like any module (`python run.py dbcheck`); analyst can also surface the finding.
- **Confirmation surface:** add a menu entry "Reparar base de datos corrupta (con confirmación)" and a bot `/dbrepair` command that:
  1. `load_actions_from_file(reporting.dir/dbcheck/plan.json)`-style read to list corrupt-DB findings (one row per `finding`, showing `app`, `integrity_status`, chosen strategy, and that the container will stop/restart).
  2. Operator confirms a SPECIFIC finding (`y/N` in menu; inline button → per-finding "Aplicar" in bot, under the same single-execution lock `bot.py` already uses).
  3. On confirm, run `dbrepair` for that fingerprint with `dry_run=False` (the front-end flips dry_run only for the confirmed item, exactly like `set_izumi_organizer` flips the organizer apply flag).
  4. Show the per-step result and the quarantine `restore_command` so the operator can manually revert.
- The `docker stop/start` actions still pass through `aictx.apply.classify` (defense in depth) before execution, so the audited boundary is unchanged.

### Test plan
- Unit (no Docker, no real Plex): build a deliberately corrupt SQLite file (truncate header / flip bytes) in a tmp dir.
  - `dbcheck`: detects corruption on ro copy; healthy DB → no finding; unreadable path → `FailureRecord`, no crash; `plan.json` shape matches apply layer; fingerprint stable across runs.
  - `dbrepair` with injected `Runner` (fake docker that records `stop`/`start` calls) and `Fs(dry_run=True)`: asserts step order — snapshot BEFORE stop, repair, start, verify; dry-run plans quarantine entries without moving.
  - `dbrepair` `dry_run=False` on a tmp tree: native-backup path (good zip/db) restores and verifies `ok`; corrupt-only path takes `.recover` and verifies `ok`; both-fail path rolls back to the ORIGINAL bytes (assert file hash equals pre-run) and restarts container.
  - Rollback: force post-repair integrity to fail → assert original restored, container restarted, `FailureRecord` present.
  - Idempotency: run twice; second run is a no-op (already `ok`).
  - I1 guard test: a security/static test asserts `dbrepair.py`/`dbcheck.py` contain no `os.remove`/`shutil.rmtree`/`unlink`/`rm ` and no `subprocess` import (only `core/fs`, `adapters/command`, `aictx.apply`).
- Integration (opt-in, marked): against a throwaway Sonarr container — detect, repair via native backup, assert container healthy via `ArrClient.system_status()`.

---

## DESIGN 2 — tdarr → Plex false duplicate (targeted refresh resolves stale duration-0 metadata)

### Problem & how it complements dupefinder's safe-skip
When tdarr re-encodes a file **in place**, Plex keeps stale metadata (`duration 0`, stale bitrate/codec). Dupefinder's `has_sane_metadata()` then fails (`"video_duration <= 0"` / `"...Plex analysis may be incomplete"`), so `select_keeper()` SAFELY SKIPS the group — exactly the right call (it refuses to delete the "wrong" copy based on garbage metadata). But the file lingers as a *false* duplicate until Plex re-analyzes. The fix doesn't touch dupefinder's safety logic — it removes the *cause* by triggering a **targeted Plex re-analyze/refresh** of just the affected item, so on the next dupefinder pass the metadata is sane and the group resolves normally (either dedupes correctly or is a true single file, no longer "false dup"). The skip remains the safety net for anything not yet refreshed.

### Module — `modules/ops/tdarr_refresh.py` (`@register("tdarr_refresh")`, READ-ONLY w.r.t. files; default DRY_RUN True for the Plex-write side)
Read-only on the filesystem (INVARIANT I1 N/A — it never moves files); the only side effect is asking Plex to refresh/analyze, which is idempotent and reversible (Plex just re-reads the file).

### Data flow
1. **Detect tdarr-completed items.** Source of truth, in preference order:
   - tdarr API/DB: query tdarr server (new tiny client `integrations/tdarr.py`, stdlib HTTP like `arr.py`, injectable fetcher) for items with status `transcode success`/`completed` whose `file` mtime is newer than `last_processed_ts` we persist. OR
   - Reuse dupefinder's own signal: parse the latest `dupefinder_report_*.json` (same loader pattern as `analyst._load_dupefinder`) for groups whose `skip_reason` matches the incomplete-metadata buckets (`video_duration <= 0`, `...Plex analysis may be incomplete`). This needs no tdarr access at all and directly targets the false-duplicate symptom. Config picks one or both sources.
2. **Resolve the Plex item + section + folder.** From the file path, find the Plex item (via PlexAPI search by path/`locations`), reusing the existing `(section_name, plex_folder)` targeting from `refresh_plex_targets` — Plex's own `locations`, no PATH_MAPPINGS translation.
3. **Targeted refresh / re-analyze.** For each affected item:
   - Call `refresh_plex_item(item, timeout)` (the existing PASS0 helper: `item.analyze()` + poll for `sane_and_changed`/`sane_unchanged`/`timeout`). This is per-ITEM and precise.
   - If the item can't be resolved individually, fall back to `refresh_plex_targets({(section, folder)})` (partial section scan of just that folder) — the existing targeted-refresh path. Full-library refresh is the last resort, gated by `PLEX_REFRESH_SCOPE`.
4. **Verify & record.** After refresh, re-read item metadata; if `duration > 0` and codec sane → mark resolved. Persist `last_processed_ts`/processed item keys (a `SqliteCache` table, same as PASS0 fingerprint cache pattern) so the module is idempotent and doesn't re-analyze the same item every run.
5. **Report.** `reporting.dir/tdarr_refresh/plan.json` + `summary.md`: items detected, refreshed, `sane_and_changed` vs still-stale, fallbacks used. Optionally emit a finding so analyst/notify can report "N false-duplicate items refreshed".

### Files / modules
- New: `modules/ops/tdarr_refresh.py`; optional `integrations/tdarr.py` (thin HTTP client, injectable fetcher, no new deps).
- Reuse: `refresh_plex_item`, `refresh_plex_targets`, `plan_item_refresh_targets` (extract these into a small importable helper if needed — currently they live in `plex_dupefinder.py`; the module imports from there or a shared `integrations/plex.py` wrapper). `analyst._load_dupefinder`-style report parsing. `core/cache.SqliteCache` for the processed-items ledger.
- Wire into `pipelines.yaml` so it can run after tdarr/extractor and BEFORE dupefinder, so dupefinder's next pass sees fresh metadata.

### Config keys (`integrations.tdarr_refresh`)
```
integrations.tdarr_refresh = {
  "dry_run": true,
  "sources": ["dupefinder_report","tdarr_api"],   # either/both
  "dupefinder_reports": "<dir>",                   # reuse analyst key shape
  "tdarr": {"base_url":"http://tdarr:8265", "api_key_ref":"TDARR_API_KEY"},  # secret via core/secrets
  "analyze_timeout_s": 30,
  "refresh_scope": "item",        # mirrors PLEX_REFRESH_SCOPE: item|library fallback
  "max_items_per_run": 200,
  "ledger_db": "cache/tdarr_refresh.db"
}
```

### Safety
- No file moves/deletes whatsoever (I1 trivially satisfied); a static test asserts no `fs`/`subprocess`/destructive calls.
- Plex refresh is idempotent and read-only to media (Plex re-reads the file). `dry_run` lists the items it WOULD analyze without calling `analyze()`.
- Per-item analyze is bounded by `analyze_timeout_s` and `max_items_per_run` so a big backlog can't hammer Plex; `sane_unchanged`/`timeout` are recorded, not retried forever (ledger prevents re-spam, with a TTL so genuinely-still-stale items get one retry next run).
- Secrets only via `core/secrets`; tdarr key never logged (same redaction discipline as arr clients).
- Complements, never overrides, dupefinder: dupefinder keeps skipping incomplete-metadata groups; this module just shrinks that set over time. If a refresh fails, the false dup simply remains safely skipped — no regression.

### Test plan
- Unit with a fake PlexAPI item (stub exposing `analyze()`, `reload()`, `locations`, `type`, media attrs) and an injected tdarr fetcher / canned `dupefinder_report_*.json`:
  - Detection from dupefinder report: only groups with the incomplete-metadata `skip_reason` are selected; other skip reasons ignored.
  - Detection from tdarr API: only `completed`/newer-than-ledger items selected.
  - Targeting: episode → show folder, movie → own folder (reusing `plan_item_refresh_targets` semantics).
  - Refresh path: `refresh_plex_item` returns `sane_and_changed` (duration 0 → >0) → item marked resolved; `timeout`/`analyze_failed` → recorded, not crash; unresolved item → falls back to `refresh_plex_targets` (assert `section.update(path=...)` called with the right folder); ultimate fallback to library scope only when configured.
  - Idempotency: second run skips already-processed items via the ledger; ledger TTL allows one retry for still-stale items.
  - `dry_run=True`: lists candidates, never calls `analyze()`/`section.update`.
  - End-to-end-ish regression: feed a report with a `video_duration <= 0` skipped group → module refreshes → simulate dupefinder re-run now sees sane metadata → group no longer skipped (proves the false duplicate resolves).
- Static/security test: module imports no `core/fs` destructive ops, no `subprocess`.

### Ordering note
Run order in `pipelines.yaml`: tdarr/extract → `tdarr_refresh` (fix stale metadata) → dupefinder (now dedupes correctly) → analyst (reports residual skips) → optional `dbcheck`. This makes the false-duplicate fix proactive instead of relying on the operator noticing the analyst report.

