# Scoring Reference

**English** | [Español](SCORING.es.md)

## Philosophy

Scoring determines which duplicate to **keep** — the candidate with the highest total score is
the keeper, and all others are candidates for removal. When two candidates score within
`MIN_SCORE_DIFFERENCE` of each other, the entire group is skipped rather than guessing which file
is truly better.

**Real media characteristics dominate; the filename only breaks ties.** Resolution, video codec,
HDR/Dolby Vision, audio and (a small amount of) bitrate decide the winner. Release **source**
(REMUX/BluRay/WEB-DL/…) is a dedicated first-class dimension, and the remaining filename patterns
(container/edition tags) are bounded tie-breakers that cannot override a real quality signal.

Every component is recorded in a per-component breakdown stored in the plan file, the JSON report
and the quarantine sidecar, so you can always audit exactly why a file was kept or removed.

Target preference order this model produces:

```
2160p DV/HDR HEVC  >  2160p HEVC  >  1080p REMUX  >  1080p HEVC  >  1080p AVC  >  720p AVC
```

Resolution dominates across tiers; within a resolution tier, REMUX wins.

---

## Score Components

### Video Resolution (dominant)

Config key: `VIDEO_RESOLUTION_SCORES`

| Resolution | Score |
|---|---|
| `4k` | 20000 |
| `1080` | 10000 |
| `720` | 5000 |
| `480` | 3000 |
| `sd` | 1000 |
| `Unknown` | 0 |

The 10000-point gap between 4K and 1080p is larger than any single non-resolution signal
(REMUX source is 8000), so a higher resolution wins across tiers unless penalties intervene.

---

### Video Codec

Config key: `VIDEO_CODEC_SCORES` (case-insensitive lookup). Aliases are included so encoder names
score identically to the codec they produce.

| Codec | Score | Aliases |
|---|---|---|
| `av1` | 14000 | |
| `hevc` | 12000 | `h265`, `x265` |
| `h264` | 8000 | `x264`, `avc` |
| `vp9` | 6000 | |
| `Unknown` | 0 | |
| `mpeg4` | -3000 | |
| `vc1` | -2000 | |
| `mpeg1video` / `mpeg2video` | -5000 | |
| `wmv2` / `wmv3` / `msmpeg4*` | -8000 | |

The HEVC→H264 gap (4000) combined with the low bitrate weight (below) ensures a **more efficient
HEVC encode beats an equivalent AVC encode**, even when the AVC file uses a higher bitrate.

> Plex reports the *codec* (`hevc`, `h264`) in `videoCodec`, not the encoder (`x265`, `x264`). The
> aliases are belt-and-suspenders; codec scoring always comes from Plex metadata, never the filename.

---

### Source (first-class, single value)

Config key: `SOURCE_SCORES`

Plex exposes no "source" field, so the source is parsed from the filename — but as a **single
value**: the highest-quality source detected wins, scores are **never summed**, and tiers are tried
best-first (so `BluRay.REMUX` scores as REMUX).

| Source | Score | Detected from (tokens / substrings, case-insensitive) |
|---|---|---|
| REMUX | 8000 | `remux`, `bdremux`, `brremux` |
| BluRay | 3000 | `bluray`, `blu ray`, `bdrip`, `brrip` |
| WEB-DL | 2000 | `web-dl`, `webdl` |
| WEBRip | 1000 | `webrip`, `web rip` |
| HDTV | -3000 | `hdtv`, `pdtv`, `hdrip`, `dsr` |
| DVD | -3000 | `dvdrip`, `dvd` |
| CAM | -15000 | `cam`, `hdcam`, `telesync`, `telecine`, `hdts` |
| (none) | 0 | filebot-style clean names with no source tag |

REMUX (8000) is intentionally **below** the resolution gap (10000) so a 2160p HEVC beats a 1080p
REMUX, while a REMUX still beats a non-REMUX of the **same** resolution.

---

### Audio Codec

Config key: `AUDIO_CODEC_SCORES`

| Codec | Score |
|---|---|
| `truehd` | 4500 |
| `dca-ma` | 4000 |
| `pcm` / `flac` | 2500 |
| `dca` | 2000 |
| `opus` | 1500 |
| `eac3` | 1250 |
| `aac` / `ac3` / `mp3` | 1000 |
| `mp2` | 500 |
| `wmapro` | 200 |
| `Unknown` | 0 |

### Audio Channels

`audio_channels × 1000`, where **`audio_channels` is the channel count of the single richest track
(MAX), not the sum of all tracks**. Summing was a bug: a multi-dub release (7.1 + 5.1 + 2.0 = 16ch)
would score far above an equivalent single-track file. The "more tracks" reward is handled
separately by `AUDIO_TRACK_SCORE`.

### HDR and Dolby Vision

Config keys: `HDR_SCORE` (3000), `DOLBY_VISION_SCORE` (5000). Detected from Plex stream metadata
(`colorTrc` = `smpte2084`/`arib-std-b67` for HDR; `DOVIPresent` for DV). A DV file with HDR metadata
receives both. With the bitrate weight reduced, these reliably yield **DV > HDR10 > SDR** at equal
source.

### Subtitle and Audio Tracks

`subtitle_count × 50` and `audio_track_count × 100` — small completeness tie-breakers.

---

### Filename Patterns (tie-breakers only)

Config keys: `FILENAME_SCORES`, `FILENAME_SCORE_CAP`

After moving source and resolution out, `FILENAME_SCORES` keeps only **container and edition**
signals. Patterns are matched (case-insensitive `fnmatch`) against the basename; positive matches
are **summed and then clamped to `FILENAME_SCORE_CAP`** so stacking cannot dominate. Negative
legacy-container penalties are real quality signals and are **not** capped.

| Pattern | Score |
|---|---|
| `*.mkv` | 800 |
| `*.mp4` | 300 |
| `*REPACK*` / `*PROPER*` / `*EXTENDED*` | 500 each |
| `*.wmv` | -8000 |
| `*.ts` | -5000 |
| `*.avi` / `*.vob` / `*.flv` | -10000 |

`FILENAME_SCORE_CAP` default: **2000**. (Resolution and source tags are deliberately absent here —
they are scored by `VIDEO_RESOLUTION_SCORES` and `SOURCE_SCORES`.)

---

### Bitrate (small tie-breaker)

Formula: `int(video_bitrate × BITRATE_SCORE_WEIGHT)`, default weight **0.1**.

Bitrate correlates with codec **inefficiency** as much as with quality — an AVC encode needs far
more bitrate than an HEVC encode for the same quality. A high weight therefore rewards bloated AVC
and lets it beat HEVC, and lets high-bitrate SDR beat HDR. The weight is kept low so bitrate only
separates otherwise-equal candidates. Tune with `BITRATE_SCORE_WEIGHT`.

### File Size

Config key: `SCORE_FILESIZE` (default `False`). When enabled, `int(file_size / 100000)`. Off by
default — size rewards bloat, contrary to "maximum quality per GB".

### Other Minor Contributions

| Component | Formula | Note |
|---|---|---|
| Video dimensions | `(width + height) × 2` | Reinforces resolution (largely redundant); constant within a resolution tier |
| Video duration | `int(video_duration / 300)` | Near-identical for duplicates of the same title, so it cancels out |

---

## Score Breakdown

`get_score()` returns `(int, dict)`. Keys present when non-zero (plus the always-present base
components):

| Key | Content |
|---|---|
| `resolution`, `video_codec`, `audio_codec` | table lookups |
| `source` | `SOURCE_SCORES` value of the detected source (single value) |
| `source_type` | the detected source key (e.g. `remux`) when one matched |
| `filename` | summed `FILENAME_SCORES`, positive part clamped to `FILENAME_SCORE_CAP` |
| `filename_matches` | list of `{pattern, score}` that matched |
| `bitrate` | `int(video_bitrate × BITRATE_SCORE_WEIGHT)` |
| `audio_channels` | `max_track_channels × 1000` |
| `dimensions`, `duration` | as above |
| `hdr` / `dolby_vision` | bonus when detected |
| `subtitle_tracks` / `audio_tracks` | when non-zero |
| `file_size` | when `SCORE_FILESIZE=True` |

---

## Example Keeper Decisions (new model)

### 2160p HEVC HDR WEB-DL  vs  1080p REMUX (AVC, TrueHD 7.1)

| Component | 2160p HEVC HDR | 1080p REMUX |
|---|---|---|
| Resolution | 20000 | 10000 |
| Video codec | 12000 (hevc) | 8000 (h264) |
| Source | 2000 (web-dl) | 8000 (remux) |
| HDR | 3000 | 0 |
| Audio codec | 1250 (eac3) | 4500 (truehd) |
| Audio channels | 6000 (5.1) | 8000 (7.1) |
| Bitrate (×0.1) | 1800 | 3000 |
| **Total** (incl. dims/duration) | **70850** | **60300** |

**Winner: 2160p HEVC HDR.** Resolution dominates; REMUX cannot overcome a full resolution tier.
(Under the old model the 1080p REMUX won on filename points — the behaviour this rework fixes.)

### 1080p HEVC 8 Mbps  vs  1080p AVC 25 Mbps (equivalent otherwise)

HEVC wins by ~3000+ because the codec advantage (4000) is no longer cancelled by the AVC's higher
bitrate (now weighted ×0.1). The old model gave HEVC a fragile ~250-point margin that a slightly
higher AVC bitrate would flip.

### scene release  vs  filebot-renamed file (identical media)

The score gap collapses from ~20000 (old, filename-driven) to the single source-tier difference
(≤ a few thousand). With `MIN_SCORE_DIFFERENCE ≥ 3000` the group is **skipped** — identical media is
left untouched rather than removed on a filename technicality.

---

## Tuning Recommendations

| Goal | Recommendation |
|---|---|
| Don't act on near-ties / identical media | `MIN_SCORE_DIFFERENCE = 3000` (≥ `FILENAME_SCORE_CAP` and the smaller source tiers) |
| HEVC-first / don't reward AVC bloat | Keep `BITRATE_SCORE_WEIGHT` low (0.1); do not raise `h264` above `hevc` |
| Stronger 4K preference | Increase the `4k` value or `HDR_SCORE`/`DOLBY_VISION_SCORE` |
| Prefer REMUX more strongly within a tier | Raise `SOURCE_SCORES["remux"]` (keep it **below** the 4k−1080 gap of 10000 to preserve resolution dominance) |
| Validate before going live | Run `AUDIT_MODE=true` + `CONFIRM_BEFORE_ACTION=false`, then `python tools/compare_plans.py old_plan.json new_plan.json` |
| Inspect a decision | Check the `score_breakdown` (incl. `source`/`source_type`) in the plan file |
