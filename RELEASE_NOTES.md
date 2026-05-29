# Release Notes

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
- Comprehensive structured logging to `activity.log` (DEBUG level) and
  `decisions.log` (human-readable per-group keep/remove record).
- Dead code noted: `_safe_path_segment()` is defined but never called; it
  predates the current `_quarantine_logical_path()` implementation.
