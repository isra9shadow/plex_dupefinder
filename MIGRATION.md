# Migration from Upstream plex_dupefinder

## Why This Fork Exists

The upstream `l3uddz/plex_dupefinder` project has a critical bug that can permanently
delete the wrong file when Plex's internal metadata is stale. This fork fixes that bug
and adds multiple independent safety layers to prevent data loss in edge cases that the
original code never handled.

---

## Critical Bug Fix: The Stale-Metadata Problem

### What the bug was

Plex caches media analysis results (codec, bitrate, duration, file path, and existence
flags) in its own database. When a file is removed from disk or lives on an unmounted
drive, Plex may continue to report it as present (`exists=True`, `accessible=True`) for
an extended period — sometimes indefinitely until a manual library scan.

The original script made all scoring and deletion decisions using only those Plex flags.
The result:

1. Plex reports two duplicates. One is a ghost entry (file is actually gone from disk);
   the other is the real file the user wants to keep.
2. The ghost entry scores well because its Plex metadata still looks valid.
3. The script identifies the ghost as the keeper and the real file as the duplicate.
4. The real file is deleted. The library is left with a broken ghost entry.

### How it is fixed: `check_file_exists()`

`check_file_exists` makes the **local filesystem authoritative**. The logic is:

- If both the local filesystem and Plex are reachable, **both must agree** the file
  exists: `exists = os.path.exists(path) AND part.exists AND part.accessible`.
- Any disagreement is treated as MISSING, regardless of which side disagrees.
- If only the local filesystem is reachable (Plex flags unavailable), the filesystem
  verdict is used alone.
- If only Plex reports (filesystem not reachable from the host), Plex alone is used
  but the limitation is logged.
- If neither source reports, the file is treated as MISSING for safety.

A media candidate whose existence check returns `exists=False` is excluded from
candidacy in `select_keeper`. A group where the highest-scoring candidate is missing
is skipped entirely — no action is taken.

### Why "filesystem-authoritative"

The filesystem is the ground truth for what bytes actually exist on disk. Plex metadata
is advisory: it reflects the last time Plex successfully analyzed or scanned the file.
The two can disagree, and when they do, the only safe interpretation is that the Plex
record is stale.

---

## Architecture Changes

### Single-pass → Two-pass

| | Upstream | This fork |
|---|---|---|
| Discovery | Scores and acts immediately in the same loop | PASS 1: read-only; scores and writes a plan file; no Plex writes |
| Action | Immediate delete after scoring | PASS 2: re-fetches each item from Plex, diffs vs PASS 1 snapshot, then acts |
| Race condition protection | None | `detect_inconsistencies()` diffs files, sizes, existence, duration, bitrate, codec, and optionally partial hashes between the two passes |
| Audit trail | None | `plans/dupefinder_plan_<run_id>_<ts>.json` written after PASS 1 regardless of whether PASS 2 runs |

Any state change detected between PASS 1 and PASS 2 (a file was transcoded, a new
import arrived, a size changed) causes the group to be skipped without action.

### Optional PASS 0: Pre-analyze

When `PRE_ANALYZE_DUPLICATES=True`, an optional PASS 0 runs before scoring. It calls
Plex's `item.analyze()` on each duplicate and polls until the metadata is confirmed
stable. Groups that time out or fail analysis are converted to skipped stubs and never
reach PASS 1 scoring. This is off by default because it is slow on large libraries and
only needed when Plex metadata is known to be stale.

### Direct Delete → Quarantine-First

| | Upstream | This fork |
|---|---|---|
| Removal method | Calls Plex DELETE API; file removed permanently | Moves file to `QUARANTINE_DIR` with `shutil.move`, then calls Plex DELETE |
| Reversibility | None | Every quarantined file gets a `.dupefinder_meta.json` sidecar |
| Restore procedure | N/A | Copy the `restore_command` field from the sidecar and run it in a shell |
| Default behavior | Delete | Quarantine (`QUARANTINE_MODE=True` default) |

The quarantine directory mirrors the logical path of the original file (title directory
and below), so files from different libraries with the same title are kept separate.
If a collision occurs (re-running into the same quarantine), the destination is
disambiguated with a library suffix and then a timestamp suffix.

The `.dupefinder_meta.json` sidecar contains:

- `original_path` and `quarantine_path`
- `run_id`, `media_id`, `reason`
- `keeper.files`, `keeper.score`, `keeper.score_breakdown`
- `restore_command`: a ready-to-run `mv '<quarantine>' '<original>'` shell command

To enable direct Plex DELETE without quarantine, set `QUARANTINE_MODE=False` and
`DRY_RUN=False` explicitly.

### Defaults Changed

| Setting | Upstream default | This fork default | Reason |
|---|---|---|---|
| `DRY_RUN` | `false` (implicit — first run acts) | `true` | A fresh install cannot destroy data without an explicit config change |
| `QUARANTINE_MODE` | Not present | `true` | All removals are recoverable by default |
| `SCORE_FILESIZE` | `true` | `false` | Efficient encodes should not lose to larger, lower-quality files |
| `VIDEO_CODEC_SCORES h264` | ~10000 (highest) | `8000` | H.264 is no longer the quality ceiling |
| `VIDEO_CODEC_SCORES hevc/h265` | ~5000 | `12000` | HEVC is the current standard for quality-efficient encodes |
| Bitrate weight | Full `int(bitrate)` | `int(bitrate * 0.5)` | Half-weight prevents bloated H.264 from outranking efficient HEVC on bitrate alone |

---

## Scoring Modernization

The upstream scoring model was built when H.264 was the dominant codec. This fork
recalibrates the defaults to reflect current encoding practice.

### Codec score changes

| Codec | Upstream score | This fork | Rationale |
|---|---|---|---|
| `av1` | Not scored | 14000 | Most efficient modern codec |
| `hevc` / `h265` | ~5000 | 12000 | Efficient and widely supported |
| `h264` | ~10000 (winner) | 8000 | Still good but no longer the ceiling |
| `mpeg4` | 0 or positive | -3000 | Legacy, penalized |
| `vc1` | 0 | -2000 | Legacy, penalized |
| `mpeg1video` / `mpeg2video` | 0 | -5000 | Obsolete formats |
| `wmv2` / `wmv3` / `msmpeg4*` | 0 | -8000 | Heavily penalized |

### SCORE_FILESIZE default flipped to `false`

File size is a proxy for bitrate, not quality. A large H.264 file can outscore an
efficient HEVC encode purely on size. Codec, resolution, and filename signals are more
reliable quality indicators. File size is retained as an opt-in tiebreaker via
`SCORE_FILESIZE=true`.

### Bitrate weight halved

Bitrate is now weighted at `0.5×` (`int(video_bitrate * 0.5)`) instead of the original
full weight. This keeps bitrate as a tiebreaker while preventing a high-bitrate H.264
rip from outranking an equal-quality HEVC encode.

### Score breakdowns are now auditable

`get_score` returns `(total_score, breakdown_dict)` alongside the integer total. The
breakdown records every scoring component (codec, resolution, filename matches, bitrate,
dimensions, audio channels, HDR, Dolby Vision, subtitle tracks, audio tracks) and is
stored in the plan file, the JSON report, and the quarantine sidecar. Every keeper
decision can be reviewed after the fact.

### New scoring signals (not in upstream)

| Signal | Config key | Default | What triggers it |
|---|---|---|---|
| HDR | `HDR_SCORE` | 3000 | Plex reports `colorTrc=smpte2084` or `colorTrc=arib-std-b67` |
| Dolby Vision | `DOLBY_VISION_SCORE` | 5000 | Plex reports `DOVIPresent` |
| Subtitle tracks | `SUBTITLE_SCORE_PER_TRACK` | 50 per track | Count of embedded subtitle streams |
| Audio tracks | `AUDIO_TRACK_SCORE` | 100 per track | Count of audio streams across all parts |

---

## New Safety Layers

The following layers do not exist in the upstream project. All are active by default
unless noted.

| Layer | Always on? | Config key | What it prevents |
|---|---|---|---|
| PASS 0 metadata refresh | Off by default | `PRE_ANALYZE_DUPLICATES` | Scoring on stale Plex metadata; groups with timeouts or analysis failures are skipped |
| Filesystem validation | Always on | — (`check_file_exists`) | Acting on ghost Plex entries where the file no longer exists on disk |
| File age cooldown | On (24 h default) | `MIN_FILE_AGE_HOURS` | Race conditions with active downloads, mid-import copies, or unsettled Plex scans |
| Metadata sanity | Always on | — (`has_sane_metadata`) | Decisions based on zero-duration, zero-bitrate, or unknown-codec entries that Plex has not yet fully analyzed |
| Score threshold | Off by default (0) | `MIN_SCORE_DIFFERENCE` | Deletions where two candidates score too similarly to be confidently distinguished |
| Size ratio protection | On (5.0× default) | `MAX_SIZE_RATIO` | Deleting a large remux because a smaller, newer copy narrowly won on codec/resolution scoring |
| PASS 2 revalidation | Always on | — (`detect_inconsistencies`) | Acting on state that changed between discovery and action (transcodes, new imports, user file moves) |
| Stability check | On (2 s default) | `STABILITY_CHECK_SECONDS` | Acting on a file that is actively being written at the moment of removal |
| Audit mode | Off by default | `AUDIT_MODE` | Running the full two-pass pipeline and reporting without any mutations, regardless of the `DRY_RUN` setting in config.json |
| Quarantine | On by default | `QUARANTINE_MODE` | Permanent data loss; every removal is reversible via the sidecar `restore_command` |
| Confirm before action | On by default | `CONFIRM_BEFORE_ACTION` | Unattended `AUTO_DELETE` runs acting without a final human confirmation |

---

## New Configuration Keys

The following keys did not exist in the upstream project. All are added automatically
to an existing `config.json` by `upgrade_settings()` on first startup after upgrading
(see Config Auto-Upgrade below).

| Key | Default | Description |
|---|---|---|
| `QUARANTINE_MODE` | `true` | Move files to `QUARANTINE_DIR` instead of deleting permanently |
| `QUARANTINE_DIR` | `""` | Absolute path for the quarantine staging area; required when `QUARANTINE_MODE=true` |
| `QUARANTINE_RETENTION_DAYS` | `30` | Informational reference for manual cleanup; the script does not auto-purge |
| `MIN_SCORE_DIFFERENCE` | `0` | Minimum score gap required to act; `0` disables |
| `MIN_FILE_AGE_HOURS` | `24` | Skip groups where any file is younger than this many hours; `0` disables |
| `MAX_SIZE_RATIO` | `5.0` | Skip groups where any non-keeper is more than this multiple larger than the keeper; `0` disables |
| `REQUIRE_LOCAL_FS_ACCESS` | `false` | Skip any group where no filesystem path is locally reachable |
| `STABILITY_CHECK_SECONDS` | `2.0` | Re-read file sizes after this many seconds and skip if any size changed; `0` disables |
| `AUDIT_MODE` | `false` | Force `DRY_RUN=true` at runtime without persisting to disk; use for scoring validation |
| `PARTIAL_HASH_ENABLED` | `false` | Compute head+tail SHA-256 during both passes and flag any hash change as an inconsistency |
| `PARTIAL_HASH_BYTES` | `1048576` | Bytes read from head and tail for partial hash (default 1 MiB each side) |
| `CONFIRM_BEFORE_ACTION` | `true` | Prompt for `YES` before PASS 2 acts on any group in `AUTO_DELETE` mode |
| `PRE_ANALYZE_DUPLICATES` | `false` | Call `item.analyze()` before PASS 1 scoring (PASS 0); slow on large libraries |
| `ANALYZE_TIMEOUT_SECONDS` | `60` | Maximum seconds to wait for PASS 0 `analyze()` results |
| `JSON_REPORT_DIR` | `""` | Directory for per-run JSON reports; empty string disables reporting |
| `HDR_SCORE` | `3000` | Score bonus when Plex detects HDR |
| `DOLBY_VISION_SCORE` | `5000` | Score bonus when Plex detects Dolby Vision |
| `SUBTITLE_SCORE_PER_TRACK` | `50` | Score bonus per embedded subtitle stream |
| `AUDIO_TRACK_SCORE` | `100` | Score bonus per audio stream |
| `PLEX_REFRESH_AFTER` | `false` | Trigger a Plex library scan on all configured libraries after the run |
| `RADARR_URL` | `""` | Base URL for Radarr (used with `RADARR_RESCAN_AFTER`) |
| `RADARR_API_KEY` | `""` | Radarr API key; redacted in reports and plan files |
| `RADARR_RESCAN_AFTER` | `false` | POST a `RescanMovie` command to Radarr after the run |
| `SONARR_URL` | `""` | Base URL for Sonarr (used with `SONARR_RESCAN_AFTER`) |
| `SONARR_API_KEY` | `""` | Sonarr API key; redacted in reports and plan files |
| `SONARR_RESCAN_AFTER` | `false` | POST a `RescanSeries` command to Sonarr after the run |
| `REQUESTS_TIMEOUT` | `30` | Timeout in seconds for all HTTP requests (Plex DELETE, Radarr, Sonarr) |

---

## Config Auto-Upgrade

`upgrade_settings()` in `config.py` runs automatically at startup. It compares every
key in the built-in `base_config` against the keys present in the user's `config.json`.
Any key that is missing from `config.json` is added with its default value.

**Existing values are never overwritten.** A user who has already set
`MIN_FILE_AGE_HOURS=48` will keep that value after upgrading.

When new keys are added, the script prints each added key to stdout and exits,
prompting the user to review the new defaults before the next run. This means
upgrading from upstream requires no manual config editing — run the script once,
review the printed additions, adjust any defaults as needed, then run again.

---

## Note on `deletefiles.sh`

The repository contains a `deletefiles.sh` script inherited from the upstream project.
It reads `decisions.log` and calls `rm` on matching lines. This script is now obsolete:

- The primary removal path is quarantine (`shutil.move`), not `rm`.
- Direct delete mode uses the Plex DELETE API, not shell `rm`.
- The `decisions.log` format has changed in ways that make the shell parsing fragile.

Users who previously used `deletefiles.sh` should instead use the quarantine directory
and the `restore_command` field in each `.dupefinder_meta.json` sidecar to review and
restore files as needed.

---

## What Is Not Changed

The following behaviors are identical to the upstream project:

- Plex API integration (`plexapi` library, server URL, token authentication)
- Interactive mode: when `AUTO_DELETE=false`, the script displays a table and prompts
  per group
- `SKIP_LIST` substring matching: any file path containing a configured substring is
  never removed
- `FIND_DUPLICATE_FILEPATHS_ONLY` mode: when enabled, only considers items where all
  locations share an identical path, selects the lowest media ID, and performs
  metadata-only removal without touching files
