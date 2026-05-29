# Safety Model

## Core Principle

**Prefer false negatives over false positives.**

It is better to skip a duplicate group entirely — leaving both files untouched — than to
accidentally remove the wrong file. Every layer in this document exists to detect a specific
real-world failure mode and bail out gracefully rather than guess. A skipped group costs
nothing; an incorrect deletion may be unrecoverable.

---

## Pipeline Overview

```text
Start
  │
  ├─[L0] DRY_RUN / AUDIT_MODE ──────────────── simulate everything, no writes
  │
  ├─[PASS 0] Pre-analyze (optional)
  │   └─[L1] snapshot-diff ─────────────────── skip if metadata not demonstrably fresh
  │
  ├─[PASS 1] Discovery
  │   ├─[L2] Filesystem validation ─────────── skip file if Plex says exists but FS disagrees
  │   ├─[L3] File age cooldown ─────────────── skip group if youngest file too new
  │   ├─[L4] Metadata sanity ──────────────── cannot be keeper if duration/bitrate/codec invalid
  │   ├─[L5] Score threshold ───────────────── skip group if winner margin too small
  │   └─[L6] Size ratio protection ─────────── skip group if size difference too extreme
  │
  └─[PASS 2] Revalidation & Action
      ├─[L7] Revalidation diff ─────────────── skip group if state changed since PASS 1
      ├─[L8] Stability check ───────────────── skip group if files still being written
      └─[L9] Quarantine ───────────────────── move to QUARANTINE_DIR, never hard-delete
```

---

## Layer Details

---

### Layer 0: DRY_RUN and AUDIT_MODE

**Function:** `remove_item()`, main startup block
**Config key(s):** `DRY_RUN` (default: `true`), `AUDIT_MODE` (default: `false`)
**Failure prevented:** Unintended data loss on a fresh install or during scoring validation.
**When triggered:** Always evaluated before any file move or Plex DELETE call.

`DRY_RUN=true` (the default) runs the full two-pass pipeline — sections scanned, scores
computed, keeper decisions made, decisions logged — but `remove_item()` short-circuits
before calling `quarantine_files()` or `remove_plex_metadata()`. No file is moved and no
Plex API DELETE is issued. The plan file and JSON report are still written, so the run
produces a complete auditable record of what *would* have happened.

`AUDIT_MODE=true` achieves the same effect but is set at runtime rather than persisted to
`config.json`. Specifically, the script executes `cfg['DRY_RUN'] = True` in memory
immediately after loading config. This means a config file with `DRY_RUN=false` still
cannot cause deletions while `AUDIT_MODE=true`. Use `AUDIT_MODE` to validate scoring
changes against a live library without touching `DRY_RUN` in your stored config.

**When to disable:** Set `DRY_RUN=false` only after reviewing the plan file from at least
one dry run and confirming the intended keeper/remove decisions are correct. Never disable
`AUDIT_MODE` — it is not "on" by default; disable it only if you intentionally want
`DRY_RUN` to control action mode again.

---

### Layer 1: PASS 0 — Pre-Analyze Snapshot Diff

**Function:** `refresh_plex_item()`, `_snapshot_media_metadata()`, `_snapshot_diff()`
**Config key(s):** `PRE_ANALYZE_DUPLICATES` (default: `false`), `ANALYZE_TIMEOUT_SECONDS` (default: `60`)
**Failure prevented:** Scoring decisions based on stale codec, bitrate, or resolution metadata
cached in Plex from a previous import, transcode, or interrupted analysis.
**When triggered:** Only when `PRE_ANALYZE_DUPLICATES=true`. Groups that fail analysis are
converted to skipped stubs and never reach PASS 1 scoring.

**How it works:**

1. Capture a metadata snapshot before calling `item.analyze()`.
2. Call `item.analyze()` to ask Plex to re-read the file's streams.
3. Poll via `item.reload()` until `updatedAt` changes or `ANALYZE_TIMEOUT_SECONDS` elapses.
4. Capture a second snapshot and diff the two with `_snapshot_diff()`.
5. Emit one of four verdicts:

| Verdict | Meaning | Outcome |
|---|---|---|
| `sane_and_changed` | Metadata changed after analyze — demonstrably fresh | Group proceeds to PASS 1 |
| `sane_unchanged` | Metadata valid but no change detected | Group proceeds with a visible warning |
| `timeout` | `updatedAt` did not change within `ANALYZE_TIMEOUT_SECONDS` | Group is skipped |
| `analyze_failed` | Exception during analyze or reload | Group is skipped |

**Documented ambiguity — `sane_unchanged`:**
Plex's `analyze()` call is asynchronous and provides no completion signal. A
`sane_unchanged` result means `_item_metadata_sane()` passed (positive bitrate and
duration, known codec) but `_snapshot_diff()` returned no changed fields across all polls.
This is irreducibly ambiguous via the Plex API: it could mean the file's metadata was
already correct and analyze found nothing to update (safe to proceed), or it could mean
analyze was queued but not yet processed within the poll window (scoring may be on stale
data). The script accepts `sane_unchanged` rather than rejecting it, because rejecting it
would skip every group in a healthy library. The ambiguity is recorded in
`run_report['phases']['pass0']['note']`.

**When to disable:** Leave `PRE_ANALYZE_DUPLICATES=false` (the default) for most runs.
Enable it when you suspect Plex has stale metadata after a Tdarr transcode or a failed
import — it is slow on large libraries.

---

### Layer 2: Filesystem Validation

**Function:** `check_file_exists()`
**Config key(s):** `REQUIRE_LOCAL_FS_ACCESS` (default: `false`)
**Failure prevented:** Removing a real, accessible file because Plex's metadata pointed to a
phantom entry that happened to score higher.

**The stale-metadata bug this layer was written to fix:**

Plex's metadata store can report `part.exists=True` and `part.accessible=True` long after a
file has been moved, deleted, or is on an unmounted network share. In the original upstream
code, scoring used only Plex's flags. If a stale ghost entry accumulated a higher score than
the real file — possible when Plex had cached high-quality metadata for a file that no longer
existed — the script would issue a Plex DELETE for the real file while leaving the ghost
entry intact. The result was data loss plus a broken library entry.

**The fix:**

`check_file_exists()` makes the local filesystem the authoritative source:

- If both Plex and the local filesystem are reachable, **both must agree** the file exists.
  Any disagreement → file is treated as `MISSING`.
- If only the local filesystem is reachable (Plex flags unavailable), use the filesystem verdict.
- If only Plex reports (filesystem not reachable from this host), use Plex's flags but log the
  limitation explicitly.
- If neither source has information, assume `MISSING` for safety.

Any media item where `check_file_exists()` returns `exists=False` is excluded from keeper
candidacy in `select_keeper()`. If no existing candidate remains, the group is skipped.

`REQUIRE_LOCAL_FS_ACCESS=true` adds a stricter requirement: if no file path in the group is
reachable via `os.path.exists()` on this host, the entire group is skipped. Use this when
the script does not run on the Plex server and you do not want to trust Plex-only existence
reports for any group.

**When to disable:** Do not disable. `REQUIRE_LOCAL_FS_ACCESS` defaults to `false` because
many deployments run the script on the Plex host where all paths are reachable; set it to
`true` only if you run the script on a separate machine and want to enforce local reachability
as a prerequisite.

---

### Layer 3: File Age Cooldown

**Function:** `select_keeper()`
**Config key(s):** `MIN_FILE_AGE_HOURS` (default: `24`)
**Failure prevented:** Acting on a file that is mid-import, mid-copy, or mid-transcode —
scenarios where the file exists in Plex but is not yet complete.

**Real-world scenarios this catches:**

- Radarr or Sonarr has just downloaded a file and is still copying it to the final location.
- Tdarr has started transcoding a file and the output is partially written.
- A Plex library scan discovered a file that appeared in the filesystem milliseconds ago.
- A user manually copied a file and Plex scanned it before the copy finished.

In all of these cases the file may have valid-looking (but incomplete) metadata and a
reasonable score. The age cooldown provides a time buffer: if the youngest file in a group is
less than `MIN_FILE_AGE_HOURS` old (measured via filesystem `mtime`), the entire group is
skipped with a `skip_reason` of the form:

```
cooldown: '<path>' is X.XXh old, below threshold Y.YYh
```

**When to disable:** Set `MIN_FILE_AGE_HOURS=0` only if your library is fully settled and no
import automation is running. On an active Radarr/Sonarr setup, the default 24 hours is a
conservative but safe choice.

---

### Layer 4: Metadata Sanity

**Function:** `has_sane_metadata()`
**Config key(s):** None — always active for all existing candidates.
**Failure prevented:** Selecting a keeper (or scoring a candidate) based on placeholder or
corrupt Plex metadata that was written during or immediately after a scan, before Plex has
finished analyzing the file.

**What constitutes insane metadata:**

| Field | Insane condition |
|---|---|
| `video_duration` | `<= 0` |
| `video_bitrate` | `<= 0` |
| `video_codec` | empty string or `"unknown"` |

Plex sometimes writes zero-bitrate or zero-duration entries as placeholders during an ongoing
analysis. A file that appears to be a 0-second clip with 0 Kbps bitrate and an unknown codec
could still score non-trivially from resolution, filename, and HDR signals. Without this
check, such a placeholder could become the nominated keeper.

A candidate with insane metadata cannot be selected as the keeper. If after exclusion no
sane candidate remains, the group is skipped. The `skip_reason` takes the form:

```
candidate <id> has invalid metadata: <reason> (Plex analysis may be incomplete)
```

**When to disable:** This check is not configurable. It is always active.

---

### Layer 5: Score Threshold

**Function:** `select_keeper()`
**Config key(s):** `MIN_SCORE_DIFFERENCE` (default: `0`, recommended: `>= 1000`)
**Failure prevented:** Removing a file when two candidates are effectively tied in quality —
a situation where the scoring model cannot confidently distinguish them.

The scoring system sums many components (codec, resolution, filename pattern, bitrate,
dimensions, HDR, audio channels, and more). When two candidates are close in quality — for
example, two 1080p H.264 encodes from different sources with similar bitrates — the score gap
may be driven entirely by minor filename differences or rounding effects rather than any
meaningful quality distinction.

`MIN_SCORE_DIFFERENCE` sets a minimum required gap between the highest-scoring candidate and
the second-highest. If `top_score - second_score < MIN_SCORE_DIFFERENCE` (and the threshold
is non-zero), the group is skipped with:

```
score delta N below threshold M
```

**Default is 0 — this is aggressive.** With a threshold of 0, any non-zero score gap is
sufficient to act. For most libraries, a threshold of 1000–5000 provides a meaningful margin.
A threshold around 10000 requires at least a full resolution tier of separation between
candidates (e.g., 720p vs 1080p).

**When to disable:** Setting `MIN_SCORE_DIFFERENCE=0` disables the check. Only do this if
you have reviewed your scoring configuration thoroughly and trust that any non-zero score gap
reflects a real quality difference.

---

### Layer 6: Size Ratio Protection

**Function:** `select_keeper()`
**Config key(s):** `MAX_SIZE_RATIO` (default: `5.0`)
**Failure prevented:** A small, efficient encode winning over a large, high-quality file due to
a scoring edge case — for example, a 2 GB HEVC encode outscoring an 80 GB Remux on codec and
resolution while the Remux is a far higher quality source.

The scoring model strongly prefers efficient modern codecs (HEVC, AV1) and high resolutions.
In some configurations this can cause an efficient HEVC transcode to outscore a Remux of the
same content because the codec and filename bonuses dominate. The size ratio check provides a
final sanity gate: even if the scoring clearly favors one candidate, a sibling that is more
than `MAX_SIZE_RATIO` times larger than the selected keeper is a signal that the pairing may
be a scoring anomaly rather than a genuine quality comparison.

When any non-keeper candidate is more than `MAX_SIZE_RATIO` times larger than the keeper, the
group is skipped with:

```
size ratio N.Nx exceeds threshold M.Mx (keeper=X bytes, sibling id=<id>=Y bytes)
```

The check is only applied when both file sizes are non-zero. `MIN_SCORE_DIFFERENCE` is
evaluated first; `MAX_SIZE_RATIO` is only reached if the score gap is already above the
threshold.

**When to disable:** Set `MAX_SIZE_RATIO=0` to disable. Consider disabling only if your
library contains intentional size disparities — for example, a Remux library paired with a
compressed-copy library — and you have confirmed that your scoring config handles them
correctly.

---

### Layer 7: PASS 2 Revalidation Diff

**Function:** `detect_inconsistencies()`
**Config key(s):** None — always active. `PARTIAL_HASH_ENABLED` (default: `false`) enables
an additional hash-level check within this layer.
**Failure prevented:** Race conditions between PASS 1 (discovery) and PASS 2 (action) — a
file was replaced, moved, re-encoded, or modified in the window between the two passes.

**The problem this addresses:**

PASS 1 and PASS 2 are separated in time. Between them, Tdarr may have finished a transcode
(changing codec and bitrate), Radarr may have upgraded a file (changing path and size), a
user may have manually reorganized their library, or Plex may have re-analyzed a file and
updated its metadata. Acting on the PASS 1 snapshot in any of these situations could remove
the wrong file.

**Fields diffed between PASS 1 snapshot and PASS 2 fresh read:**

| Field | Notes |
|---|---|
| Media set membership | Detects new or disappeared media items |
| File paths | Per media item |
| File size | Per media item |
| `exists` flag | Per media item |
| `video_duration` | Per media item |
| `video_bitrate` | Per media item |
| `video_codec` | Per media item |
| Partial hash | Per part, only when `PARTIAL_HASH_ENABLED=true` |
| Keeper selection | Whether fresh `select_keeper()` chooses a different keeper or now wants to skip |

Any non-empty diff list causes the group to be skipped. Up to six diffs are printed to
stdout; the complete list is recorded in the run report.

`PARTIAL_HASH_ENABLED=true` computes a SHA-256 of the first and last `PARTIAL_HASH_BYTES`
(default 1 MiB) of each file during both passes. Any hash difference — indicating the file
content changed — causes the group to be skipped. This catches in-place transcodes and
partial overwrites that do not change the file path or size visibly within the poll window.

**When to disable:** This layer is not configurable. Enable `PARTIAL_HASH_ENABLED` for
additional protection in environments where in-place file modification is possible.

---

### Layer 8: Stability Check

**Function:** `is_files_stable()`
**Config key(s):** `STABILITY_CHECK_SECONDS` (default: `2.0`)
**Failure prevented:** Tdarr, a copy operation, or any other process actively writing to a
candidate file at the exact moment of action — a window that Layer 7's revalidation diff may
not catch if the modification started after the PASS 2 fetch.

**How it works:**

Immediately before calling `remove_item()`, the script reads the on-disk size of every
candidate file, waits `STABILITY_CHECK_SECONDS`, then reads the sizes again. Any size change
between the two reads causes the entire group to be skipped. Files that cannot be read at all
are left to Layer 2 (filesystem validation) to handle.

This is the last line of defense before any file is moved to quarantine. By the time this
check runs, the group has already passed discovery scoring, revalidation, and the full diff —
this layer catches only the narrow case where a write began in the final seconds before
action.

**When to disable:** Set `STABILITY_CHECK_SECONDS=0` to disable. The 2-second default adds
negligible time to a run. Disabling is appropriate only if all candidate files are on
read-only or immutable storage where in-progress writes are structurally impossible.

---

### Layer 9: Quarantine

**Function:** `quarantine_files()`, `_write_quarantine_sidecar()`
**Config key(s):** `QUARANTINE_MODE` (default: `true`), `QUARANTINE_DIR` (required when active), `QUARANTINE_RETENTION_DAYS` (default: `30`, advisory only)
**Failure prevented:** Permanent, unrecoverable data loss.

This is not a detection layer — it is a **recovery layer**. Every layer above decides whether
to act; this layer determines what "act" means.

When `QUARANTINE_MODE=true`, files are never hard-deleted. Instead:

1. `quarantine_files()` moves each removed file to `QUARANTINE_DIR` using
   `_quarantine_logical_path()` to reconstruct a meaningful directory structure anchored at
   the title's directory component.
2. Three-pass collision handling ensures no file in quarantine is ever overwritten:
   - First attempt: bare logical path under `QUARANTINE_DIR`.
   - If that path exists: append `__<LIBRARY_NAME>` to the top-level folder.
   - If that also exists: append `__<unix_timestamp>` to the filename stem.
3. A `.dupefinder_meta.json` sidecar is written beside every moved file. It contains:
   - `original_path` and `quarantine_path`
   - `run_id`, `media_id`, `reason`
   - `original_size` and `original_mtime` at quarantine time
   - `keeper.files`, `keeper.score`, `keeper.score_breakdown`
   - `restore_command`: a ready-to-run shell command (`mv '<quarantine_path>' '<original_path>'`)

**To restore a quarantined file:** open the `.dupefinder_meta.json` sidecar, copy the
`restore_command` value, and run it in a shell. No script is required.

If all files in a group fail to quarantine (move errors for every file), `remove_item()`
returns without calling `remove_plex_metadata()` — the Plex entry is preserved so the
library remains consistent with the (unmoved) files.

`QUARANTINE_RETENTION_DAYS` is an advisory field recorded in documentation and the sidecar
for operator reference. The script does not enforce automated purging; retention is the
operator's responsibility.

**When to disable:** Set `QUARANTINE_MODE=false` only when operating in
`FIND_DUPLICATE_FILEPATHS_ONLY` mode (identical file paths, metadata-only cleanup) or when
quarantine storage is genuinely unavailable. When disabled, `remove_item()` calls only
`remove_plex_metadata()` — the underlying file is not touched, but no sidecar is written and
recovery requires manual Plex database inspection.

---

## Documented Limitations

The safety model is intentionally conservative, but it has boundaries that operators should
understand.

### Plex async analyze ambiguity

`PRE_ANALYZE_DUPLICATES=true` cannot reliably confirm that Plex has re-analyzed a file.
The `analyze()` API call is asynchronous and Plex provides no completion signal. A
`sane_unchanged` verdict — meaning metadata was valid but did not change after analyze — is
irreducibly ambiguous: Plex may have found nothing to update (correct behavior on a healthy
file) or may not have processed the analyze request within the poll window. The script
proceeds on `sane_unchanged` with a visible warning because rejecting it would skip every
group in a well-maintained library.

### Network paths without local filesystem access

When `REQUIRE_LOCAL_FS_ACCESS=false` (the default) and the script runs on a machine that
cannot reach the Plex media paths via `os.path.exists()`, Layer 2 falls back to Plex's
`exists` and `accessible` flags. These flags can be stale. The stale-metadata bug that Layer
2 was written to fix can resurface in this deployment topology. If you run the script
off-host, set `REQUIRE_LOCAL_FS_ACCESS=true` to skip any group where no file is locally
reachable, or mount the media paths on the script host before running.

### Cross-library duplicates

Plex reports duplicates per library section. A movie present in both a "Movies" library and a
"4K Movies" library will not appear in the same duplicate group — the script will never see
them as duplicates of each other. Use `SKIP_LIST` to protect libraries you do not want
managed, or scan each library independently with appropriate scoring configurations.

### SKIP_LIST maintenance

The script cannot automatically determine which directories or file paths should be protected.
`SKIP_LIST` is a substring-match list against file paths, maintained entirely by the
operator. Files in directories not covered by `SKIP_LIST` are eligible for removal. Review
and update `SKIP_LIST` whenever the library structure changes.

---

## Disabling Layers — Risk Summary

| Layer | Config key to disable | Risk if disabled |
|---|---|---|
| L0: DRY_RUN | `DRY_RUN=false` | Enables real file moves and Plex DELETEs — intended, but must be deliberate |
| L0: AUDIT_MODE | `AUDIT_MODE=false` (default) | `DRY_RUN` in config.json becomes the sole control; runtime override removed |
| L1: Pre-analyze snapshot | `PRE_ANALYZE_DUPLICATES=false` (default) | Scoring proceeds on whatever metadata Plex currently holds; stale metadata is not detected |
| L2: Filesystem validation | Not configurable (core logic) | Cannot be fully disabled; `REQUIRE_LOCAL_FS_ACCESS=false` allows Plex-only existence reports on off-host deployments |
| L3: File age cooldown | `MIN_FILE_AGE_HOURS=0` | Mid-import and mid-transcode files become eligible for action |
| L4: Metadata sanity | Not configurable | Cannot be disabled |
| L5: Score threshold | `MIN_SCORE_DIFFERENCE=0` (default) | Any non-zero score gap is sufficient to act; near-ties are not protected |
| L6: Size ratio | `MAX_SIZE_RATIO=0` | Large-file siblings of high-scoring small files become eligible for removal |
| L7: Revalidation diff | Not configurable (always active); `PARTIAL_HASH_ENABLED=false` disables hash sub-check | Without hash check, in-place content changes that preserve file size and path are not detected |
| L8: Stability check | `STABILITY_CHECK_SECONDS=0` | Files actively being written at action time are not detected |
| L9: Quarantine | `QUARANTINE_MODE=false` | File removal becomes permanent and unrecoverable via this script; no sidecar or restore path |
