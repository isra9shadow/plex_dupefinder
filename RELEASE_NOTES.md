# Release Notes

**English** | [Español](RELEASE_NOTES.es.md)

## v2.2.0-rc1 — Test release (2026)

Release candidate for validation on a real library. Bundles the audit,
testability, tooling and scoring work. The **stability / config / logging /
reporting** block is ready to validate in production; the **scoring rework** is
included but should be validated against real audit data before non-dry runs.

### Audit & operational safety
- `AUDIT_MODE` has two sub-modes via `CONFIRM_BEFORE_ACTION`: `false` = fully
  unattended (cron), `true` = assisted manual selection. Per-group prompts only
  occur on an acting run; audits never block on input.
- Startup banner prints `INTERACTIVE_MODE / AUDIT_MODE / CONFIRM_BEFORE_ACTION`.
- Quarantine of a multi-part item removes the Plex entry only if **every** part
  moved (no disk orphans); partial failures preserve the entry and log it.

### Testability & tests
- The module is now import-safe: Plex connection, config validation and
  log-file creation are deferred to the entrypoint; `config.py` falls back to
  built-in defaults when run non-interactively.
- `tests/` (pytest, third-party libs stubbed) covering `get_score`,
  `select_keeper`, `check_file_exists`, `_quarantine_logical_path`,
  `detect_inconsistencies`, source ranking, codec aliases, audio MAX and the
  full preference order. `requirements-dev.txt` added.

### Tooling (read-only, no production impact)
- `tools/analyze_report.py` — simulate the proposed scoring over a real
  plan/report and report keeper changes, anomalies and questionable decisions.
- `tools/compare_plans.py` — diff keeper decisions between two plan files.

### Scoring rework (validate before non-dry production)
- Release **source** is a first-class single-value dimension (`SOURCE_SCORES`),
  parsed from the filename; highest-quality source wins, never summed, bounded
  below the resolution gap.
- `BITRATE_SCORE_WEIGHT` (0.1) — bitrate reduced to a tie-breaker so a bloated
  AVC no longer beats an efficient HEVC.
- `FILENAME_SCORES` reduced to container/edition tie-breakers; positive sum
  clamped by `FILENAME_SCORE_CAP`.
- Audio channels scored from the richest single track (MAX), not the sum.
- Codec aliases: `hevc=h265=x265`, `h264=x264=avc`.
- Target order: 2160p DV/HDR HEVC > 2160p HEVC > 1080p REMUX > 1080p HEVC >
  1080p AVC > 720p AVC.

> **Upgrade note:** on first run after pulling, `upgrade_settings()` adds the new
> keys to your `config.json` and exits for review. Your existing `FILENAME_SCORES`
> are preserved, so the source-type filename patterns may coexist with the new
> `SOURCE_SCORES` (double-counting) until you trim them — validate scoring with
> an audit + `tools/analyze_report.py` before going non-dry.

---

## v2.1.0 — Operational Hardening (2026)

Incremental release. No architecture changes, no breaking changes.

### Fixes

- **Direct-delete messaging corrected.** The runtime banner previously claimed
  "files remain on disk, untracked" in direct-delete mode. This was wrong:
  with Plex's **Allow media deletion** enabled (required), the Plex media
  DELETE removes the file from disk. The banner, `README.md`, and
  `SAFETY_MODEL.md` now state plainly that direct delete is irreversible and
  deletes the file (except in `FIND_DUPLICATE_FILEPATHS_ONLY` mode, which is
  metadata-only because all entries share one physical file).
- **`build_config()` wizard bug.** Answering the "Auto Delete duplicates?"
  prompt correctly on the first try left `AUTO_DELETE` at `false` regardless of
  the answer (the assignment lived inside the invalid-input loop). Fixed.
- **`PLEX_DELETE_DELAY_SECONDS` added to `base_config`.** It was documented and
  used but missing from the defaults, so `upgrade_settings()` never added it to
  existing configs. It is now a first-class default (`2.0`).

### Operational features

- **Rotating logs + `LOG_LEVEL`.** `activity.log` now uses a
  `RotatingFileHandler` capped at 10 MiB × 5 backups, so scheduled unattended
  runs on large libraries cannot fill the disk. New `LOG_LEVEL` config key
  (default `INFO`; set `DEBUG` for per-part tracing).
- **Library-name validation.** At startup, every name in `PLEX_LIBRARIES` is
  checked against the libraries that exist on the server. A typo now aborts
  (exit code 2) with the list of available libraries, instead of silently
  doing nothing for that library.
- **Quarantine summary.** Every run reports the standing quarantine contents —
  file count, total size, oldest file age, and count exceeding
  `QUARANTINE_RETENTION_DAYS` — to stdout and the JSON report (`quarantine`
  key). Read-only; nothing is ever auto-purged.

---

## v2.0.0 — Safety-First Rewrite (2026)

Complete rewrite focused on operational safety for production Plex homelabs.

### Critical Bug Fix

**Stale-metadata deletion bug**

The upstream script could delete the wrong file when Plex's metadata cache was
stale: a phantom entry (file already removed from disk) could outscore the real
file, causing the real file to be deleted.

Fix: `check_file_exists()` now requires both Plex's `exists`/`accessible` flags
AND `os.path.exists()` to agree. Any disagreement treats the file as missing and
skips the entire group.

---

### Architecture: Two-Pass Validation

**PASS 1 — Discovery (read-only)**

- Fetches duplicate groups, scores every candidate, selects tentative keeper.
- Applies all safety filters (existence, age, sanity, thresholds).
- Writes a JSON plan file — no side effects in this pass.

**PASS 2 — Revalidation and Action**

- Re-fetches each group from Plex.
- Compares current state against the PASS 1 snapshot (`detect_inconsistencies`).
- Runs a stability check just before acting.
- Only acts if state is fully consistent with PASS 1.

**PASS 0 — Optional Pre-Analyze**

- Triggers Plex `analyze()` on each duplicate item before scoring.
- Snapshot-diff approach: captures metadata before and after, compares to detect
  whether `analyze()` produced fresh data.
- Statuses: `sane_and_changed` (demonstrably fresh), `sane_unchanged` (ambiguous
  — documented Plex API limitation, noted in `run_report`).

---

### Quarantine System

Files are never hard-deleted by default — they are moved to `QUARANTINE_DIR`.

- Logical path structure preserved: `QUARANTINE/Show Name/Season/ep.mkv`.
- A `.dupefinder_meta.json` sidecar is written alongside every quarantined file:
  - `original_path`, `quarantine_path`, `restore_command` (copy and run in a
    shell to restore the file — no script required).
  - `keeper.files`, `keeper.score`, `keeper.score_breakdown`.
  - `original_size`, `original_mtime` (integrity verification fields).
  - `library`, `title`, `year` (optional Plex context, when available).
- Collision handling: `__LIBRARY` suffix for cross-library same-title collisions;
  `__<unix_timestamp>` fallback for repeat runs into the same quarantine.
- `DRY_RUN=true` and `QUARANTINE_MODE=true` are the defaults — safe out of the
  box on a fresh install.

---

### Safety Layers Added

| Layer | Config Key | Protects Against |
|---|---|---|
| Filesystem validation | always-on | Phantom file scores (stale Plex metadata) |
| File age cooldown | `MIN_FILE_AGE_HOURS` | Active imports and mid-transcode files |
| Metadata sanity | always-on | Zero-duration or placeholder codec metadata |
| Score threshold | `MIN_SCORE_DIFFERENCE` | Near-tie wrong-choice deletions |
| Size ratio protection | `MAX_SIZE_RATIO` | Large file losing to a smaller sibling |
| PASS 2 revalidation | always-on | State change between discovery and action |
| Stability check | `STABILITY_CHECK_SECONDS` | Active writes at the moment of action |
| Audit mode | `AUDIT_MODE` | Forces `DRY_RUN=true` in memory without persisting to disk |

---

### Scoring Improvements

**Modern codec hierarchy**

Upstream scored H264 highest (10000) and penalised HEVC (5000). This release
inverts that priority to reward storage-efficient formats:

| Codec | Score |
|---|---|
| AV1 | 14000 |
| HEVC / H265 | 12000 |
| H264 | 8000 |
| VP9 | 6000 |
| MPEG-4 | -3000 |
| VC-1 | -2000 |
| MPEG-1 / MPEG-2 | -5000 |
| WMV / MS-MPEG-4 variants | -8000 |

**Score breakdown dict**

`get_score()` now returns `(int, dict)`. Every keeper decision is fully auditable
— the breakdown is stored in the plan file, the JSON report, and the quarantine
sidecar.

**`SCORE_FILESIZE=false` default**

File size does not inherently indicate quality. Efficient HEVC encodes must not
lose to bloated H264 rips. File size is available as an opt-in tiebreaker only.

**Bitrate weighted at 0.5x**

Raw bitrate is not a quality proxy. The weight is halved to prevent
large-but-inefficient files from dominating codec and resolution signals.

**HDR, Dolby Vision, subtitle, and audio track bonuses**

| Feature | Config Key | Default Score |
|---|---|---|
| HDR (smpte2084 / arib-std-b67) | `HDR_SCORE` | 3000 |
| Dolby Vision (`DOVIPresent`) | `DOLBY_VISION_SCORE` | 5000 |
| Subtitle tracks | `SUBTITLE_SCORE_PER_TRACK` | 50 per track |
| Audio tracks | `AUDIO_TRACK_SCORE` | 100 per track |

---

### Operational Features

**JSON execution reports**

Written to `JSON_REPORT_DIR` per run. Includes all group decisions, scores,
per-group records, removed items, integration results, and errors. Sensitive keys
(`PLEX_TOKEN`, `RADARR_API_KEY`, `SONARR_API_KEY`) are always redacted.

Filename format: `dupefinder_report_<run_id>_<YYYYMMDDTHHMMSSZ>.json`

**JSON plan file**

Written after PASS 1 to `plans/dupefinder_plan_<run_id>_<ts>.json` before PASS 2
acts. A run aborted at the confirmation gate still leaves a full auditable plan.

**`AUDIT_MODE`**

Runs the full two-pass pipeline including plan file and JSON report but forces
`DRY_RUN=true` in memory without modifying `config.json`. Use for scoring
validation and regression testing.

**Config auto-upgrade**

`upgrade_settings()` merges any new key from `base_config` into an existing
`config.json` on every startup without overwriting user values. Added keys are
printed and the user is prompted to review before the run proceeds.

**Per-run identifier**

Every log line, plan file, JSON report, and quarantine sidecar is stamped with a
12-character hex `run_id` (`uuid4().hex[:12]`) for reliable cross-artifact
correlation.

---

### Integration Improvements

- **Radarr rescan**: `RADARR_RESCAN_AFTER=true` posts a `RescanMovie` command to
  `/api/v3/command` at the end of every run.
- **Sonarr rescan**: `SONARR_RESCAN_AFTER=true` posts a `RescanSeries` command.
- **Plex library refresh**: `PLEX_REFRESH_AFTER=true` calls `section.update()` on
  all scanned libraries after cleanup.
- **Partial hash consistency**: `PARTIAL_HASH_ENABLED=true` computes a SHA-256 of
  the first and last N bytes of each file in both passes; any hash change between
  PASS 1 and PASS 2 causes the group to be skipped.

---

### Configuration

- `config_sample.json`: complete reference configuration documenting all 35+ keys
  with inline comments.
- All sensitive keys are redacted in every JSON output artifact.
- `PLEX_DELETE_DELAY_SECONDS` (default `2.0`): sleep between consecutive
  `remove_item` calls within a group to avoid hammering the Plex API.

---

### Breaking Changes vs Upstream

| Area | Upstream (`l3uddz/plex_dupefinder`) | This Fork |
|---|---|---|
| Pipeline | Single pass: discover and act in one loop | Two-pass: PASS 1 read-only, PASS 2 revalidate and act |
| File removal | Plex DELETE API only (permanent) | Quarantine by default (`shutil.move`); Plex DELETE only after successful move |
| `DRY_RUN` default | `false` (first run would act immediately) | `true` — a fresh install cannot destroy data without an explicit config change |
| `SCORE_FILESIZE` default | `true` | `false` |
| Codec scoring | H264-biased | HEVC/AV1-preferred; legacy codecs penalised |
| `deletefiles.sh` | Active and relevant | Obsolete — replaced by quarantine sidecar `restore_command` workflow |

---

### Code Quality

- All bare `except:` clauses replaced with `except Exception:`.
- Input validation throughout (`isdigit()` checked before `int()` conversion).
- `REDACTED_KEYS` tuple ensures `PLEX_TOKEN`, `RADARR_API_KEY`, and
  `SONARR_API_KEY` are never written to plan files or JSON reports.
- Comprehensive structured logging to `activity.log` and `decisions.log`
  (human-readable per-group keep/remove record). See v2.1.0 for log rotation
  and the configurable `LOG_LEVEL`.
