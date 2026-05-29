# Configuration Reference

**English** | [Español](CONFIGURATION.es.md)

## Overview

All settings live in `config.json`, located in the same directory as the script. On first run without a `config.json`, a setup wizard guides you through creating one interactively. On every subsequent run, `upgrade_settings()` automatically merges any new keys introduced by the current release into your existing `config.json` without overwriting values you have already set — it prints the added keys and exits so you can review before proceeding.

---

## Quick Setup — Minimum Required Config

The only keys the script requires to start are the Plex connection details and the library list. Every other setting has a safe default.

```json
{
  "PLEX_SERVER": "http://192.168.1.100:32400",
  "PLEX_TOKEN": "your-token-here",
  "PLEX_LIBRARIES": ["Movies", "TV Shows"]
}
```

All remaining keys will be read from their built-in defaults. Because `DRY_RUN` defaults to `true`, a fresh install **cannot delete or move any files** until you explicitly change that setting.

---

## Safe Defaults

The script ships with a conservative safety posture. A brand-new `config.json` with only the three required keys above will behave as follows:

| Behaviour | Default | Why |
|---|---|---|
| `DRY_RUN=true` | Nothing is deleted, moved, or written to Plex | Prevents data loss on misconfiguration |
| `QUARANTINE_MODE=true` | When live mode is enabled, files are **moved**, not deleted | Every removal is reversible |
| `AUTO_DELETE=false` | Interactive mode — the script prompts before each group | Gives you control over every decision |
| `CONFIRM_BEFORE_ACTION=true` | Even in auto mode, requires typing `YES` before PASS 2 acts | Final human checkpoint before any mutation |
| `AUDIT_MODE=false` | Set to `true` to run the full two-pass pipeline (including JSON reports) without any side effects | Useful for scoring validation before going live |

---

## Configuration Sections

### 1. Connection

**`PLEX_SERVER`**
- Default: `"https://plex.your-server.com"`
- Type: string
- Description: Base URL of your Plex Media Server. Use `http://` for LAN access (e.g. `http://192.168.1.100:32400`) or your public HTTPS address. The script calls `PlexServer(PLEX_SERVER, PLEX_TOKEN)` at startup and aborts if the connection fails.
- Risk: 🟡 Incorrect URL causes immediate abort at startup — no data is touched.

---

**`PLEX_TOKEN`**
- Default: `""`
- Type: string
- Description: Plex authentication token. Required — `validate_config` aborts if empty. This value is redacted (replaced with `"<redacted>"`) in all plan files and JSON reports so you can share logs safely. See [Finding your Plex token](https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/).
- Risk: 🔴 Treat as a password. See the Security Notes section.

---

**`PLEX_LIBRARIES`**
- Default: `[]`
- Type: list of strings
- Description: Names of the Plex library sections to scan for duplicates. Must match the library names exactly as they appear in Plex. Required — `validate_config` aborts if the list is empty. Example: `["Movies", "TV Shows", "4K Movies"]`. After connecting, `validate_libraries()` checks every configured name against the libraries that actually exist on the server.
- Risk: 🟡 If any configured name does not exist on the Plex server, the script aborts at startup (exit code 2) and prints the list of available libraries, rather than silently doing nothing for the typo'd name.

---

**`REQUESTS_TIMEOUT`**
- Default: `30`
- Type: integer (seconds)
- Description: Timeout in seconds applied to all HTTP requests made via the `requests` library — this covers Plex DELETE calls, Radarr `/api/v3/command`, and Sonarr `/api/v3/command`. Increase on slow networks or for large Plex instances.
- Risk: 🟢 Safe to adjust.

---

### 2. Safety & Behaviour

**`DRY_RUN`**
- Default: `true`
- Type: boolean
- Description: The primary safety gate. When `true`, the full two-pass discovery and scoring pipeline runs but no files are moved or deleted and no Plex metadata is removed. All decisions are logged. Set to `false` only after reviewing a dry-run report. `AUDIT_MODE=true` overrides this back to `true` at runtime without writing to disk.
- Risk: 🔴 Setting to `false` enables live file operations. Ensure `QUARANTINE_MODE=true` and `QUARANTINE_DIR` is set before doing so.

---

**`AUDIT_MODE`**
- Default: `false`
- Type: boolean
- Description: When `true`, forces `DRY_RUN=true` in memory at runtime (without modifying `config.json`) even if `config.json` has `DRY_RUN=false`. The full two-pass pipeline runs including plan files and JSON reports, but no files are touched and no Plex calls are made. Interactivity is controlled by `CONFIRM_BEFORE_ACTION`:
  - `AUDIT_MODE=true` + `CONFIRM_BEFORE_ACTION=false` → **fully unattended**: the scoring-recommended keeper is auto-selected for every group, `_manual_choose_keeper()` is never entered, and it can analyse thousands of duplicate groups without blocking on input. **This is the recommended cron configuration.**
  - `AUDIT_MODE=true` + `CONFIRM_BEFORE_ACTION=true` → **assisted audit**: the candidate table is shown per group and you choose the keeper manually. No file is ever moved or deleted in either case.
- Risk: 🟢 Safe — cannot produce side effects by design.

---

**`QUARANTINE_MODE`**
- Default: `true`
- Type: boolean
- Description: When `true` (and `DRY_RUN=false`), files selected for removal are **moved** to `QUARANTINE_DIR` using `shutil.move` rather than being permanently deleted. A `.dupefinder_meta.json` sidecar is written beside each moved file containing the original path, a ready-to-run `restore_command`, and the full scoring breakdown. Plex metadata is only removed after a successful move. If all quarantine moves fail, the group is aborted and Plex is not touched. Set to `false` only if you are certain you want permanent deletion.
- Risk: 🔴 Setting to `false` makes removal permanent. There is no undo path.

---

**`QUARANTINE_DIR`**
- Default: `""`
- Type: string (absolute path)
- Description: Absolute path of the directory where quarantined files are staged. Created automatically if it does not exist and the parent is writable. Required when `QUARANTINE_MODE=true` and `DRY_RUN=false` — `validate_config` aborts at startup if this combination is active and the value is empty. Choose a path with enough free space to hold the files you expect to remove.
- Risk: 🟡 Must be on the same filesystem as the media files for `shutil.move` to avoid a full cross-device copy. If cross-device, the move still works but is slower and temporarily doubles disk usage.

---

**`QUARANTINE_RETENTION_DAYS`**
- Default: `30`
- Type: integer
- Description: Informational — the script does **not** auto-purge the quarantine directory. This value is for your own reference and operational planning. After reviewing quarantined files, delete them manually (or via a scheduled task) once you are satisfied with the removal decisions.
- Risk: 🟢 Changing this value has no runtime effect.

---

**`AUTO_DELETE`**
- Default: `false`
- Type: boolean
- Description: Governs per-group prompts **on an acting run** (live: not `DRY_RUN`, not `AUDIT_MODE`). When `false` (default) on an acting run, the script pauses at each duplicate group and displays a table of candidates, the recommended keeper, and scoring breakdown; you can accept the recommendation, choose a different keeper, or skip the group. When `true`, it acts on the scoring recommendation without per-group prompts (with `CONFIRM_BEFORE_ACTION` as a final checkpoint). On a non-acting run (`DRY_RUN`/`AUDIT_MODE`) `AUTO_DELETE` is irrelevant — prompting there is governed by `CONFIRM_BEFORE_ACTION` instead.
- Risk: 🟡 Set to `true` only after validating scoring on your library with dry runs.

---

**`CONFIRM_BEFORE_ACTION`**
- Default: `true`
- Type: boolean
- Description: Has two distinct, non-overlapping roles depending on the run:
  - **Acting + auto-delete run** (`DRY_RUN=false`, `AUTO_DELETE=true`): when `true`, the script pauses after PASS 1, shows a summary of all planned actions, and requires you to type `YES` before PASS 2 begins acting — the last human checkpoint before any file move or Plex delete.
  - **Non-acting run** (`DRY_RUN=true` or `AUDIT_MODE=true`): controls per-group keeper prompts. `true` = assisted (candidate table shown per group); `false` = fully unattended (recommended keeper auto-selected, no prompts). **Set to `false` for unattended/cron audits.**
- Risk: 🟡 In an acting auto-delete run, setting to `false` removes the final checkpoint. In a non-acting run it only affects whether you are prompted; no destructive action occurs either way.

---

**`FIND_DUPLICATE_FILEPATHS_ONLY`**
- Default: `false`
- Type: boolean
- Description: When `true`, only considers duplicate groups where all media locations are identical (the same physical file has been scanned into Plex more than once as separate metadata entries). In this mode, the script selects the entry with the lowest media ID to keep and removes only the Plex metadata — **no files are moved or deleted**. Useful for cleaning up Plex's own import errors without touching any files on disk.
- Risk: 🟢 This mode is metadata-only; it cannot affect files on disk.

---

**`PLEX_DELETE_DELAY_SECONDS`**
- Default: `2.0`
- Type: float (seconds)
- Description: Sleep duration between consecutive `remove_item` calls within a group during PASS 2. Prevents hammering the Plex HTTP API when a group has multiple candidates to remove. Increase if you observe Plex rate-limit errors in the activity log.
- Risk: 🟢 Increasing this slows the run; decreasing below 1.0 may cause transient Plex API errors on busy servers.

---

**`SKIP_LIST`**
- Default: `[]`
- Type: list of strings
- Description: Substring list. Any candidate whose file path contains any entry in this list is never removed — it is silently skipped and recorded as `mode='skipped_skip_list'` in the JSON report. Matching is a plain substring containment check (not glob or regex) against the full file path. A match skips only the matched candidate; other candidates in the same group are still processed. Example: `["/mnt/protected/", "remux", ".iso"]`.
- Risk: 🟢 Only prevents removal — never causes it.

---

### 3. Safety Thresholds

**`MIN_FILE_AGE_HOURS`**
- Default: `24`
- Type: float (hours)
- Description: Files younger than this many hours cause the entire group to be skipped. Prevents race conditions with active downloads, mid-import copies, or Plex scans that have not yet settled. The skip reason is logged as: `"cooldown: '<path>' is X.XXh old, below threshold Y.YYh"`. Set to `0` to disable.
- Failure mode prevented: Acting on a file that is still being written or that Plex has not yet fully indexed.
- Risk: 🟢 Increasing this value is always safer. Decreasing below `1.0` risks acting on files that are still changing.

---

**`MAX_SIZE_RATIO`**
- Default: `5.0`
- Type: float
- Description: If any non-keeper candidate is more than this multiple of the keeper's file size, the group is skipped. Guards against scoring-quirk mis-pairings where a large remux or 4K file is outscored by a smaller, newer encode. The check is `other_size / keeper_size > MAX_SIZE_RATIO`. The skip reason is logged as: `"size ratio N.Nx exceeds threshold M.Mx (keeper=X, sibling id=<id>=Y)"`. Set to `0` to disable.
- Failure mode prevented: Accidentally removing a large, high-quality remux because a smaller newer file won on codec or resolution scoring.
- Risk: 🟡 Disabling (`0`) or setting very high removes protection against mis-pairings.

---

**`MIN_SCORE_DIFFERENCE`**
- Default: `0`
- Type: integer
- Description: Minimum score gap required between the top-scoring candidate (the keeper) and the second-best candidate before the script will act. If the gap is smaller than this threshold, the group is skipped with reason: `"score delta N below threshold M"`. `0` disables the check (any non-zero gap is sufficient). Recommended starting value: `1000`.
- Failure mode prevented: Deleting a file when two candidates score almost identically — a sign that the scoring signals are ambiguous and a human should review.
- Risk: 🟡 Setting to `0` allows the script to act on near-ties. Consider at least `500`–`1000` for production use.

---

**`STABILITY_CHECK_SECONDS`**
- Default: `2.0`
- Type: float (seconds)
- Description: Before acting on a group in PASS 2, the script reads all candidate file sizes, waits this many seconds, then reads them again. If any size changed, the group is skipped. Catches files that passed the `MIN_FILE_AGE_HOURS` cooldown but are still actively being written (e.g. a Tdarr transcode that started after the cooldown window). Only active when `DRY_RUN=false` and this value is `> 0`. Set to `0` to disable.
- Failure mode prevented: Acting on a file mid-transcode or mid-copy that appears old enough but is still changing.
- Risk: 🟢 Increasing adds latency. Decreasing below `1.0` may miss fast-changing files.

---

**`REQUIRE_LOCAL_FS_ACCESS`**
- Default: `false`
- Type: boolean
- Description: When `true`, any group where no candidate file path is locally readable on the host running the script is skipped entirely. Use this when running the script on a machine that does not have direct filesystem access to all Plex media paths (e.g. a separate Docker container without all mounts). When `false`, the script falls back to Plex's own `exists`/`accessible` flags for paths it cannot reach locally.
- Failure mode prevented: Making removal decisions based solely on Plex metadata when the filesystem is unavailable — which can lead to acting on stale Plex entries.
- Risk: 🟡 Leaving this `false` on a host with no filesystem access means the stale-metadata safety layer (`check_file_exists`) cannot operate fully.

---

### 4. Scoring

Scoring determines which duplicate is kept. The candidate with the highest total score is the keeper; all others are candidates for removal. Each component adds or subtracts from the total. The full breakdown is stored in plan files, JSON reports, and quarantine sidecars. Real media characteristics (resolution, codec, HDR/DV, audio) dominate; release source is a first-class dimension; filename patterns are bounded tie-breakers. See [SCORING.md](SCORING.md) for the complete model, tables and examples.

---

**`SOURCE_SCORES`**
- Default: `{remux: 8000, bluray: 3000, web-dl: 2000, webrip: 1000, hdtv: -3000, dvd: -3000, cam: -15000}`
- Type: object (source key → integer)
- Description: First-class release-source scoring. Plex has no source field, so the source is parsed from the filename, but scored as a **single value** — the highest-quality source detected wins (never summed). `remux` (8000) is deliberately below the 4K↔1080p resolution gap (10000) so higher resolution still wins across tiers while REMUX wins within a tier.
- Risk: 🟡 Changing values reorders source preference. Keep `remux` below the resolution gap to preserve resolution dominance.

---

**`FILENAME_SCORE_CAP`**
- Default: `2000`
- Type: integer
- Description: Upper bound on the **positive** sum of `FILENAME_SCORES`, so stacking several filename patterns cannot dominate a real media decision. Negative legacy-container penalties are not capped. `0` disables the cap.
- Risk: 🟢 Only limits filename influence.

---

**`BITRATE_SCORE_WEIGHT`**
- Default: `0.1`
- Type: float
- Description: Multiplier applied to video bitrate (kbps). Low by design: bitrate correlates with codec **inefficiency** as much as quality, so a high value lets a bloated AVC outscore an efficient HEVC. Kept as a small tie-breaker.
- Risk: 🟡 Raising it weakens HEVC-first behaviour.

---

**`SCORE_FILESIZE`**
- Default: `false`
- Type: boolean
- Description: When `true`, adds `int(file_size / 100,000)` to a candidate's score. Disabled by default because raw file size rewards bloated encodes — a large H.264 rip can significantly outscore an efficient HEVC encode of equal or better perceived quality. Codec, resolution, and filename signals are more reliable quality indicators. Enable only if your library has a specific reason to prefer larger files (e.g. you exclusively collect lossless remuxes and file size is a proxy for completeness).
- Risk: 🟡 Enabling can cause the script to keep larger, lower-quality files over smaller, more efficient encodes.

---

**`HDR_SCORE`**
- Default: `3000`
- Type: integer
- Description: Bonus added to a candidate's score when Plex reports HDR content (`colorTrc = smpte2084` or `arib-std-b67`). Increase to more strongly prefer HDR versions. Set to `0` to ignore HDR in scoring.
- Risk: 🟢 Adjusting this value only affects which duplicate is kept, not whether action is taken.

---

**`DOLBY_VISION_SCORE`**
- Default: `5000`
- Type: integer
- Description: Bonus added when Plex reports Dolby Vision (`DOVIPresent`). Higher than `HDR_SCORE` by default because DV is an upgrade over plain HDR10. Adjust to match your display capabilities.
- Risk: 🟢 Adjusting this value only affects which duplicate is kept.

---

**`SUBTITLE_SCORE_PER_TRACK`**
- Default: `50`
- Type: integer
- Description: Bonus per subtitle stream across all parts of a media item. Applied as `subtitle_count * SUBTITLE_SCORE_PER_TRACK`. Small by default to act as a tiebreaker rather than a dominant signal.
- Risk: 🟢 Safe to adjust.

---

**`AUDIO_TRACK_SCORE`**
- Default: `100`
- Type: integer
- Description: Bonus per audio stream across all parts of a media item. Applied as `audio_track_count * AUDIO_TRACK_SCORE`. Rewards versions with multiple audio language tracks.
- Risk: 🟢 Safe to adjust.

---

**`FILENAME_SCORES`**
- Default: See table below
- Type: object (glob pattern → integer)
- Description: Maps `fnmatch` glob patterns to score integers. Applied to the **basename** of each candidate's file path, case-insensitively. Multiple patterns can match the same file; their scores are summed. Positive scores reward high-quality sources; negative scores penalise low-quality sources. These are the highest-weight signals in the default scoring model.

| Pattern | Default Score |
|---|---|
| `*Remux*` | 25000 |
| `*2160p*BluRay*` | 20000 |
| `*4K*BluRay*` | 20000 |
| `*1080p*BluRay*` | 15000 |
| `*2160p*WEB-DL*` | 14000 |
| `*4K*WEB-DL*` | 14000 |
| `*1080p*WEB-DL*` | 12000 |
| `*720p*BluRay*` | 8000 |
| `*WEB-DL*` | 6000 |
| `*WEBRip*` | 4000 |
| `*REPACK*` | 1500 |
| `*PROPER*` | 1500 |
| `*.mkv` | 2000 |
| `*EXTENDED*` | 500 |
| `*.mp4` | 500 |
| `*HDTV*` | -5000 |
| `*TS*` | -5000 |
| `*.ts` | -5000 |
| `*DVDRip*` | -3000 |
| `*dvd*` | -3000 |
| `*.wmv` | -8000 |
| `*.avi` | -10000 |
| `*.vob` | -10000 |
| `*.flv` | -10000 |
| `*CAM*` | -20000 |

- Risk: 🟡 Adjust patterns to match your naming conventions. A misconfigured pattern that matches the wrong files can cause the wrong duplicate to be kept.

---

**`VIDEO_CODEC_SCORES`**
- Default: See table below
- Type: object (codec string → integer)
- Description: Maps Plex `videoCodec` strings (lowercase) to score integers. Lookup is case-insensitive. The default scoring strongly prefers efficiency-first codecs and penalises legacy formats.

| Codec | Default Score |
|---|---|
| `av1` | 14000 |
| `hevc` | 12000 |
| `h265` | 12000 |
| `h264` | 8000 |
| `vp9` | 6000 |
| `Unknown` | 0 |
| `mpeg4` | -3000 |
| `vc1` | -2000 |
| `mpeg1video` | -5000 |
| `mpeg2video` | -5000 |
| `wmv2` | -8000 |
| `wmv3` | -8000 |
| `msmpeg4` | -8000 |
| `msmpeg4v2` | -8000 |
| `msmpeg4v3` | -8000 |

- Risk: 🟡 Changing codec scores affects which duplicate is kept. Review with a dry run after any change.

---

**`VIDEO_RESOLUTION_SCORES`**
- Default: See table below
- Type: object (resolution string → integer)
- Description: Maps Plex `videoResolution` strings to score integers. Higher native resolution wins by default.

| Resolution | Default Score |
|---|---|
| `4k` | 20000 |
| `1080` | 10000 |
| `720` | 5000 |
| `480` | 3000 |
| `sd` | 1000 |
| `Unknown` | 0 |

- Risk: 🟡 Modifying these values changes which resolution is preferred. Ensure the values are consistent with your `FILENAME_SCORES` and `VIDEO_CODEC_SCORES`.

---

**`AUDIO_CODEC_SCORES`**
- Default: See table below
- Type: object (codec string → integer)
- Description: Maps Plex `audioCodec` strings to score integers. Lossless and object-audio formats rank highest by default.

| Codec | Default Score |
|---|---|
| `truehd` | 4500 |
| `dca-ma` | 4000 |
| `pcm` | 2500 |
| `flac` | 2500 |
| `dca` | 2000 |
| `opus` | 1500 |
| `eac3` | 1250 |
| `aac` | 1000 |
| `ac3` | 1000 |
| `mp3` | 1000 |
| `mp2` | 500 |
| `wmapro` | 200 |
| `Unknown` | 0 |

- Risk: 🟡 Adjust to match your audio hardware. For example, if your receiver does not decode TrueHD, you may prefer to lower its score relative to EAC3.

---

### 5. PASS 0 — Pre-Analyze

**`PRE_ANALYZE_DUPLICATES`**
- Default: `false`
- Type: boolean
- Description: When `true`, enables PASS 0: before scoring each duplicate group, the script calls `item.analyze()` on every media item in Plex and polls for fresh metadata. This forces Plex to re-examine codec, bitrate, and duration data before the script makes any decisions. Disabled by default because it is sequential and slow on large libraries (one analyze call per item, polling up to `ANALYZE_TIMEOUT_SECONDS`). Groups that time out (`timeout`) or fail (`analyze_failed`) are converted to skipped stubs and never reach PASS 1 scoring.
- Risk: 🟡 Significantly increases runtime on large libraries. Enable when you suspect Plex has stale codec or bitrate metadata (e.g. after a Tdarr batch transcode).

---

**`ANALYZE_TIMEOUT_SECONDS`**
- Default: `60`
- Type: float (seconds)
- Description: Maximum time to wait for `item.analyze()` to produce fresh metadata for a single item in PASS 0. The script polls with `item.reload()` until the metadata changes or this timeout elapses. Items that time out are skipped with status `timeout`. Items that return sane but unchanged metadata (`sane_unchanged`) are accepted with a warning — this status is **ambiguous**: it can mean either the metadata was already correct (safe to proceed) or that Plex has not yet processed the `analyze()` call within the poll window (scoring may still be on stale data). The script cannot distinguish these cases via the Plex API.
- Risk: 🟢 Increase on slow or heavily loaded Plex servers.

---

### 6. Partial Hashing

**`PARTIAL_HASH_ENABLED`**
- Default: `false`
- Type: boolean
- Description: When `true`, computes a SHA-256 hash of the first and last `PARTIAL_HASH_BYTES` bytes of each candidate file during both PASS 1 and PASS 2. If any hash changes between the two passes, the group is treated as inconsistent and skipped. Provides additional protection against a file being modified between discovery and action (e.g. an active Tdarr transcode that was not caught by the stability check). Adds filesystem read overhead proportional to `PARTIAL_HASH_BYTES * 2 * number_of_candidates`.
- Risk: 🟢 Enabling only adds a safety check — it cannot cause incorrect removals. Adds I/O overhead.

---

**`PARTIAL_HASH_BYTES`**
- Default: `1048576` (1 MiB)
- Type: integer (bytes)
- Description: Number of bytes to read from the beginning **and** the end of each file for the partial hash. The total read per file is `PARTIAL_HASH_BYTES * 2`. The default 1 MiB head + 1 MiB tail catches changes to container headers and end-of-stream footers without reading the entire file.
- Risk: 🟢 Increasing provides slightly stronger drift detection at the cost of more I/O. Only relevant when `PARTIAL_HASH_ENABLED=true`.

---

### 7. Reporting

**`JSON_REPORT_DIR`**
- Default: `""` (disabled)
- Type: string (absolute path)
- Description: Directory where per-run JSON reports are written. When empty, no report is written. When set, `validate_config` creates the directory automatically if it does not exist. The report filename is `dupefinder_report_<run_id>_<YYYYMMDDTHHMMSSZ>.json`. Reports include a redacted copy of your config, all phase counters (PASS 0, discovery, revalidation, action), per-group records with scores and decisions, integration results, and a human-readable summary. Sensitive keys (`PLEX_TOKEN`, `RADARR_API_KEY`, `SONARR_API_KEY`) are replaced with `"<redacted>"`.

**Plan files** are always written (regardless of this setting) to `<script_dir>/plans/` after PASS 1 completes. The plan file captures the full PASS 1 snapshot before any action is taken, providing an auditable record even if you abort at the confirmation prompt. Plan filename: `dupefinder_plan_<run_id>_<YYYYMMDDTHHMMSSZ>.json`.

- Risk: 🟢 Enabling creates files on disk but has no effect on removals.

---

**`LOG_LEVEL`**
- Default: `"INFO"`
- Type: string — one of `DEBUG`, `INFO`, `WARNING`, `ERROR` (case-insensitive)
- Description: Verbosity of `activity.log`. `INFO` (default) records phase progress and every decision. `DEBUG` additionally logs a line per media part (existence, age) — useful for diagnosis but large on big libraries. An unrecognised value falls back to `INFO`. Regardless of level, `activity.log` is size-rotated by a `RotatingFileHandler` capped at **10 MiB × 5 backups** (≈60 MiB ceiling), so unattended scheduled runs cannot fill the disk.
- Risk: 🟢 Affects logging only; no effect on removals.

---

**Quarantine summary** — At the end of every run (when `QUARANTINE_DIR` is set), the script reports the **standing** contents of the quarantine directory: file count, total size, oldest file age, and how many files exceed `QUARANTINE_RETENTION_DAYS`. This is read-only visibility only — the script never auto-purges. The same figures are written to the JSON report under the `quarantine` key. Age is derived from each sidecar's `quarantine_timestamp` (not file mtime, which `shutil.move` preserves from the original).

---

### 8. Integrations

**`PLEX_REFRESH_AFTER`**
- Default: `false`
- Type: boolean
- Description: When `true`, calls `section.update()` on every library in `PLEX_LIBRARIES` at the end of the run. This triggers a Plex library scan to detect any changes made by the removal process. Uses the existing `PLEX_TOKEN` — no additional configuration needed.
- Risk: 🟢 Only triggers a scan; does not modify any metadata.

---

**`RADARR_URL`**
- Default: `""`
- Type: string
- Description: Base URL of your Radarr instance (e.g. `http://192.168.1.100:7878`). Required when `RADARR_RESCAN_AFTER=true` — `validate_config` aborts at startup if `RADARR_RESCAN_AFTER` is enabled but this value is empty.
- Risk: 🟢 Only used for the post-run rescan trigger.

---

**`RADARR_API_KEY`**
- Default: `""`
- Type: string
- Description: Radarr API key. Sent as the `X-Api-Key` header. Required when `RADARR_RESCAN_AFTER=true`. Redacted in all plan files and JSON reports.
- Risk: 🔴 Treat as a password. See Security Notes.

---

**`RADARR_RESCAN_AFTER`**
- Default: `false`
- Type: boolean
- Description: When `true`, POSTs a `RescanMovie` command to `<RADARR_URL>/api/v3/command` at the end of the run. Triggered once after all groups are processed, regardless of how many items were actually removed. Requires both `RADARR_URL` and `RADARR_API_KEY` to be set.
- Risk: 🟢 Only triggers a rescan in Radarr.

---

**`SONARR_URL`**
- Default: `""`
- Type: string
- Description: Base URL of your Sonarr instance (e.g. `http://192.168.1.100:8989`). Required when `SONARR_RESCAN_AFTER=true`.
- Risk: 🟢 Only used for the post-run rescan trigger.

---

**`SONARR_API_KEY`**
- Default: `""`
- Type: string
- Description: Sonarr API key. Sent as the `X-Api-Key` header. Required when `SONARR_RESCAN_AFTER=true`. Redacted in all plan files and JSON reports.
- Risk: 🔴 Treat as a password. See Security Notes.

---

**`SONARR_RESCAN_AFTER`**
- Default: `false`
- Type: boolean
- Description: When `true`, POSTs a `RescanSeries` command to `<SONARR_URL>/api/v3/command` at the end of the run. Same behaviour and requirements as `RADARR_RESCAN_AFTER`, applied to Sonarr.
- Risk: 🟢 Only triggers a rescan in Sonarr.

---

## Step-by-Step: Going Live Safely

Follow these steps in order. Each step adds confidence before the next removes a safety layer.

**Step 1 — Run in dry mode (default)**

Leave `DRY_RUN=true` and run the script. Review the console output and `decisions.log`. No files will be touched.

**Step 2 — Enable JSON reporting**

Set `JSON_REPORT_DIR` to a path you can browse. Re-run in dry mode and review the generated report. Check that the keeper selections match your expectations for a sample of groups.

**Step 3 — Tune scoring if needed**

Review the per-group score breakdowns in the JSON report. If the wrong duplicate is being selected as keeper for any group, adjust `VIDEO_CODEC_SCORES`, `FILENAME_SCORES`, or `MIN_SCORE_DIFFERENCE` accordingly. Re-run in dry mode until satisfied.

**Step 4 — Set a score threshold**

Set `MIN_SCORE_DIFFERENCE` to at least `1000`. This prevents the script from acting on near-ties where the scoring is ambiguous.

```json
"MIN_SCORE_DIFFERENCE": 1000
```

**Step 5 — Configure the quarantine directory**

Set `QUARANTINE_DIR` to an absolute path with sufficient free space. Ensure `QUARANTINE_MODE` remains `true`.

```json
"QUARANTINE_DIR": "/mnt/quarantine/plex_dupefinder",
"QUARANTINE_MODE": true
```

**Step 6 — Go live**

Set `DRY_RUN=false`. With `QUARANTINE_MODE=true`, files are moved to `QUARANTINE_DIR` rather than deleted. Plex metadata is removed only after a successful move.

```json
"DRY_RUN": false
```

**Step 7 — Review the quarantine directory**

After the run, browse `QUARANTINE_DIR`. Each moved file has a `.dupefinder_meta.json` sidecar. To restore a file, copy the `restore_command` field from the sidecar and run it in a shell:

```sh
mv '/mnt/quarantine/plex_dupefinder/Breaking Bad/Season 01/ep.mkv' '/mnt/media/TV/Breaking Bad/Season 01/ep.mkv'
```

When satisfied with the removals, delete the quarantine contents manually.

**Step 8 — Optional: enable partial hashing**

For additional PASS 2 confidence (especially on active libraries where Tdarr or other tools may be transcoding), enable partial hashing:

```json
"PARTIAL_HASH_ENABLED": true,
"PARTIAL_HASH_BYTES": 1048576
```

---

## Security Notes

`config.json` contains credentials that grant full access to your Plex server and optionally your Radarr and Sonarr instances.

- **Restrict file permissions.** On Linux/macOS: `chmod 600 config.json`. On Windows: ensure the file is accessible only to your user account.
- **Never commit `config.json` to source control.** The repository's `.gitignore` excludes it — along with local backups like `config.json.factory` / `config.json.bak` and `config.new.json` — by default. Verify with `git status` before any `git add`.
- **Use `PLEX_TOKEN` rotation.** Plex tokens do not expire automatically. Rotate your token periodically via the Plex web interface, especially after any suspected exposure.
- **Sensitive keys are redacted in reports.** `PLEX_TOKEN`, `RADARR_API_KEY`, and `SONARR_API_KEY` are replaced with `"<redacted>"` in all plan files and JSON reports. Do not manually copy these values into reports or logs.
- **Least-privilege tokens.** If your Radarr/Sonarr instances support scoped API keys, use a key that can only trigger rescans rather than a full-admin key.
