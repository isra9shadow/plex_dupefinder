# plex_dupefinder — Safe Plex Duplicate Manager

**English** | [Español](README.es.md)

<img src="assets/logo.svg" width="600" alt="Plex DupeFinder">

A safety-first, quarantine-by-default duplicate manager for Plex Media Server.

---

## Overview

Plex libraries accumulate duplicate media entries over time — re-imports after drive migrations, Radarr upgrades, Tdarr transcodes landing alongside originals, or metadata mismatches that trick Plex into treating the same file as two separate items. Left unmanaged, these duplicates waste storage and clutter your library. plex_dupefinder automates the cleanup.

The core philosophy is **conservative by design**: a false negative (leaving a duplicate in place) is always preferable to a false positive (destroying the only good copy). Every decision goes through a scoring engine that favors codec efficiency and encode quality over raw file size, a two-pass pipeline that re-validates state before acting, and a quarantine layer that moves files rather than deleting them — so every removal is fully reversible.

The tool is built for modern Plex homelabs running Plex alongside Radarr, Sonarr, Tdarr, or Unraid. It understands MKV-first libraries, HDR and Dolby Vision content, multi-language audio tracks, and the encode-quality signals embedded in filenames by tools like Radarr and Tdarr.

**On a fresh install, `DRY_RUN=true` and `QUARANTINE_MODE=true` are the defaults.** The first run will never delete or move any file. You must explicitly set `DRY_RUN=false` in `config.json` to allow any action, and quarantine mode ensures files are moved — not destroyed — until you verify the results and clear the quarantine directory yourself.

---

## How It Works

plex_dupefinder operates in three sequential passes:

```text
[PASS 0]  optional — trigger Plex analyze() + snapshot-diff
    │     (ensures scoring uses fresh codec/bitrate metadata)
    │
    ▼
[PASS 1]  Discovery  (read-only — no side effects)
    │  ├─ fetch duplicate groups from Plex
    │  ├─ score every candidate file
    │  ├─ apply safety filters (age, sanity, thresholds)
    │  ├─ select tentative keeper
    │  └─ write JSON plan file
    │
    ▼
[PASS 2]  Revalidation & Action
       ├─ re-fetch each group from Plex
       ├─ re-score and diff vs PASS 1 snapshot
       ├─ stability check (detect active writes)
       └─ quarantine  ──or──  DRY_RUN log
```

**PASS 0** (disabled by default, `PRE_ANALYZE_DUPLICATES=true`) calls `analyze()` on each duplicate item and polls Plex until the metadata has settled. Groups that time out or fail analysis are marked as skipped stubs and never proceed to scoring. This prevents decisions based on stale zero-bitrate or unknown-codec metadata.

**PASS 1** is entirely read-only. It fetches duplicate groups from Plex, builds a score for each candidate file, runs all safety filters, selects a tentative keeper, and writes a JSON plan file to `plans/`. No file is touched, no Plex write is issued.

**PASS 2** re-fetches each group from Plex independently, re-scores it, and diffs the fresh state against the PASS 1 snapshot. If anything changed between the two passes — file sizes, codecs, existence flags, or which file Plex now thinks is the keeper — the group is skipped. Only groups that pass this consistency check and a final stability check (file sizes stable across a short read-sleep-read window) proceed to action.

---

## Safety Layers

Ten independent safety layers protect against data loss. Any one of them can abort a group independently of the others:

- **DRY_RUN / AUDIT_MODE** — All mutations are no-ops by default; `AUDIT_MODE` forces dry-run even when `DRY_RUN=false` in config, without writing to disk.
- **PASS 0 pre-analyze** — Groups with timed-out or failed `analyze()` calls are skipped before scoring.
- **Filesystem-authoritative existence check** — Both Plex and the local filesystem must agree a file exists; disagreement is treated as MISSING, preventing removal of stale-metadata ghosts.
- **File age cooldown** — Files younger than `MIN_FILE_AGE_HOURS` (default 24 h) are skipped, protecting active downloads and mid-import copies.
- **Metadata sanity check** — Any candidate with zero duration, zero bitrate, or an unknown codec causes the entire group to be skipped.
- **Score threshold** — If the score gap between the top two candidates is below `MIN_SCORE_DIFFERENCE`, the group is skipped; scoring ambiguity is not acted on.
- **Size ratio protection** — If a non-keeper is more than `MAX_SIZE_RATIO` (default 5×) larger than the keeper, the group is skipped; a wildly larger sibling suggests a mis-pairing.
- **PASS 2 revalidation** — Files, sizes, existence, duration, bitrate, codec, and optional partial hashes are diffed between PASS 1 and PASS 2; any change aborts the group.
- **Stability check** — File sizes are read, a short sleep occurs, then re-read; any size change (active write) skips the group.
- **Quarantine** — Removals move files to `QUARANTINE_DIR` with a `.dupefinder_meta.json` sidecar containing a ready-to-run `restore_command`; nothing is hard-deleted unless `QUARANTINE_MODE=false` is explicitly set.

See [SAFETY_MODEL.md](SAFETY_MODEL.md) for a complete description of each layer.

---

## Quick Start

### Requirements

- Python 3.8 or later
- Plex Media Server with **Allow media deletion** enabled (Settings → Server → Library)
- Dependencies: `pip install -r requirements.txt`

### First Run

```bash
# 1. Copy the sample config
cp config_sample.json config.json

# 2. Set the minimum required fields in config.json:
#    PLEX_SERVER, PLEX_TOKEN, PLEX_LIBRARIES

# 3. Run — DRY_RUN=true by default, nothing will be deleted
python3 plex_dupefinder.py
```

Review the console output and inspect `plans/dupefinder_plan_<run_id>_<timestamp>.json` to see exactly what the tool would do before enabling live mode.

### Finding Your Plex Token

See the official Plex support article: <https://support.plex.tv/articles/204059436>

### Enabling Live Mode (Quarantine)

```json
{
  "DRY_RUN": false,
  "QUARANTINE_MODE": true,
  "QUARANTINE_DIR": "/mnt/user/quarantine"
}
```

Files are **moved** to `QUARANTINE_DIR`, never hard-deleted. Each moved file has a `.dupefinder_meta.json` sidecar written beside it containing the original path and a shell-ready `restore_command`. To restore a file, open its sidecar and run the `restore_command` field.

---

## Quarantine

When `QUARANTINE_MODE=true`, the quarantine directory mirrors the original library structure anchored at the title directory:

```
Original  : /mnt/user/Media/TV/Breaking Bad/Season 01/Episode.mkv
Quarantine: QUARANTINE_DIR/Breaking Bad/Season 01/Episode.mkv
```

A `.dupefinder_meta.json` sidecar is written beside every quarantined file:

```json
{
  "original_path": "/mnt/user/Media/TV/Breaking Bad/Season 01/Episode.mkv",
  "quarantine_path": "/mnt/user/quarantine/Breaking Bad/Season 01/Episode.mkv",
  "quarantine_timestamp": "2024-05-01T12:00:00+00:00",
  "run_id": "a3f9c2d1e4b8",
  "reason": "duplicate (keeper id=12345, highest score (87500) among existing files)",
  "restore_command": "mv '/mnt/user/quarantine/Breaking Bad/Season 01/Episode.mkv' '/mnt/user/Media/TV/Breaking Bad/Season 01/Episode.mkv'",
  "keeper": {
    "files": ["/mnt/user/Media/TV/Breaking Bad/Season 01/Episode.2160p.BluRay.mkv"],
    "score": 87500,
    "score_breakdown": {
      "video_codec": 12000,
      "resolution": 20000,
      "filename": 22000,
      "audio_codec": 4500,
      "bitrate": 8200,
      "audio_channels": 6000,
      "hdr": 3000
    }
  }
}
```

To restore a quarantined file, copy the `restore_command` value and run it in a shell — no script required.

The script does not auto-purge the quarantine directory. After you have verified the results over your retention window (`QUARANTINE_RETENTION_DAYS` is informational), clear the quarantine directory manually. To help you decide when, every run prints a standing-quarantine summary — file count, total size, oldest file age, and how many files exceed `QUARANTINE_RETENTION_DAYS` — and writes the same figures to the JSON report under the `quarantine` key. This is reporting only; nothing is ever deleted automatically.

---

## Scoring

The scoring engine is designed for codec efficiency and encode quality, not raw file size. A compact HEVC remux should always outscore a bloated H.264 rip of the same content. `SCORE_FILESIZE` is `false` by default for exactly this reason.

`get_score()` returns a `(total_score, breakdown_dict)` tuple. The breakdown records every component that contributed to the total — codec, resolution, filename patterns, bitrate, dimensions, audio channels, HDR/Dolby Vision bonuses, and track counts — so you can audit why the tool preferred one file over another. The breakdown is stored in the plan file, the JSON report, and the quarantine sidecar.

The codec hierarchy puts AV1 (14 000) and HEVC/H.265 (12 000) well above H.264 (8 000), penalizes legacy formats (mpeg4, VC-1, WMV, MPEG-2), and gives a strong signal for Dolby Vision (5 000 bonus) and HDR (3 000 bonus). MKV container gets a filename-pattern bonus; AVI and VOB are penalized.

See [SCORING.md](SCORING.md) for the full scoring tables and tuning guide.

---

## Modes

| Mode | DRY_RUN | QUARANTINE_MODE | Effect |
|------|---------|-----------------|--------|
| Safe preview (default) | `true` | any | Simulates everything, logs only — no files touched |
| Quarantine (recommended) | `false` | `true` | Moves files to `QUARANTINE_DIR`; Plex metadata removed after successful move |
| Direct delete | `false` | `false` | Calls the Plex media DELETE API — with **Allow media deletion** enabled (required), Plex removes the file from disk. Irreversible — no quarantine, no sidecar, no restore |
| Audit | `AUDIT_MODE=true` | any | Full two-pass pipeline including reports; `DRY_RUN` forced to `true` at runtime |

Direct delete mode is provided for setups where Plex runs on a remote host and the script cannot reach the filesystem to perform quarantine moves itself. In this mode Plex performs the deletion: with **Allow media deletion** enabled, the underlying file is permanently removed from disk. It is irreversible — use quarantine mode whenever the script has filesystem access. The only exception is `FIND_DUPLICATE_FILEPATHS_ONLY` mode, where all entries share one physical file and only the redundant Plex metadata is cleared.

---

## Configuration

Minimum required `config.json`:

```json
{
  "PLEX_SERVER": "https://plex.your-server.com",
  "PLEX_TOKEN": "your-plex-token",
  "PLEX_LIBRARIES": ["Movies", "TV Shows"],
  "DRY_RUN": true
}
```

All other keys have safe defaults. Run with `DRY_RUN=true` first and review the plan file before enabling live mode.

See [CONFIGURATION.md](CONFIGURATION.md) for all configuration options with defaults, types, and descriptions.

---

## JSON Reports

After every run, two files are written (when configured):

- **Plan file** — Written after PASS 1, before any action. Saved to `plans/dupefinder_plan_<run_id>_<timestamp>.json`. Contains the full PASS 1 snapshot: every duplicate group, scores, score breakdowns, existence checks, and the tentative keeper decision. Written unconditionally — even a run aborted at the confirmation prompt leaves an auditable plan.

- **Execution report** — Written at the end of the run to `JSON_REPORT_DIR/dupefinder_report_<run_id>_<timestamp>.json`. Covers all phase counters (PASS 0 verdicts, groups found/actioned/skipped), per-group records, integration results (Plex refresh, Radarr, Sonarr), a standing-quarantine summary (under the `quarantine` key), a summary, and a redacted copy of the config (tokens and API keys replaced with `<redacted>`).

Set `JSON_REPORT_DIR` in `config.json` to enable execution reports. The directory is created automatically if it does not exist.

---

## Integrations

### Radarr

Set `RADARR_RESCAN_AFTER=true`, `RADARR_URL`, and `RADARR_API_KEY` in `config.json`. After the run completes, plex_dupefinder posts a `RescanMovie` command to Radarr so it can detect and re-import any content affected by the cleanup.

### Sonarr

Set `SONARR_RESCAN_AFTER=true`, `SONARR_URL`, and `SONARR_API_KEY`. After the run, a `RescanSeries` command is posted to Sonarr.

### Plex library refresh

Set `PLEX_REFRESH_AFTER=true`. After the run, plex_dupefinder calls `section.update()` on each scanned library to refresh the Plex metadata index.

---

## Plex Setup

**Allow media deletion** must be enabled in Plex before plex_dupefinder can remove duplicate metadata entries:

1. Open Plex Web → Settings → Server → Library
2. Enable **Allow media deletion**
3. Click **Save Changes**

Without this setting, Plex will reject the HTTP DELETE requests plex_dupefinder issues when removing duplicate metadata entries.

---

## Tests

A minimal safety test suite covers the decision functions that can delete media — `get_score`, `select_keeper`, `check_file_exists`, `_quarantine_logical_path`, and `detect_inconsistencies`:

```bash
pip install -r requirements-dev.txt
pytest tests/ -q
```

The tests stub Plex/`requests`/`tabulate`, so they run with only `pytest` installed — no Plex server or network required.

---

## Documentation

- [CONFIGURATION.md](CONFIGURATION.md) — all configuration options, defaults, and types
- [SCORING.md](SCORING.md) — scoring system, codec tables, and tuning guide
- [SAFETY_MODEL.md](SAFETY_MODEL.md) — all ten safety layers in detail
- [MIGRATION.md](MIGRATION.md) — differences from the upstream l3uddz/plex_dupefinder project
- [RELEASE_NOTES.md](RELEASE_NOTES.md) — changelog
