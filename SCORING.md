# Scoring Reference

## Philosophy

Scoring determines which duplicate to **keep** — the candidate with the highest total score
is designated the keeper, and all others are candidates for removal. When two candidates
score within `MIN_SCORE_DIFFERENCE` of each other, the entire group is skipped rather than
guessing which file is truly better. This conservative design means false negatives (skip a
group and leave duplicates in place) are always preferred over false positives (delete the
wrong file). Every component of a score is recorded in a per-component breakdown dictionary
that is stored in the plan file, the JSON report, and the quarantine sidecar — so you can
always audit exactly why a file was kept or removed.

---

## Score Components

### Video Codec

The video codec is the single strongest quality signal in the default configuration. Modern
efficient codecs (AV1, HEVC) are rewarded; legacy or lossy codecs are penalized with negative
scores. HEVC and H265 are aliases for the same codec in Plex and share the same score.

Config key: `VIDEO_CODEC_SCORES` (dict, case-insensitive lookup)

| Codec | Score |
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

Hierarchy: AV1 > HEVC/H265 > H264 > VP9 > Unknown > legacy/proprietary (negative).
Legacy Microsoft codecs (WMV, MSMPEG4) receive the largest penalties to ensure they are never
preferred over any modern encode.

---

### Video Resolution

Config key: `VIDEO_RESOLUTION_SCORES` (dict, case-insensitive lookup)

| Resolution | Score |
|---|---|
| `4k` | 20000 |
| `1080` | 10000 |
| `720` | 5000 |
| `480` | 3000 |
| `sd` | 1000 |
| `Unknown` | 0 |

Hierarchy: 2160p/4K > 1080p > 720p > 480p > SD. The large gap between 4K (20000) and 1080p
(10000) means a 4K file will beat a 1080p file on resolution alone unless other penalties
(codec, filename) reverse the outcome.

---

### Audio Codec

Config key: `AUDIO_CODEC_SCORES` (dict, case-insensitive lookup)

| Codec | Score |
|---|---|
| `truehd` | 4500 |
| `dca-ma` | 4000 |
| `dca` | 2000 |
| `pcm` | 2500 |
| `flac` | 2500 |
| `opus` | 1500 |
| `eac3` | 1250 |
| `aac` | 1000 |
| `ac3` | 1000 |
| `mp3` | 1000 |
| `mp2` | 500 |
| `wmapro` | 200 |
| `Unknown` | 0 |

Lossless/object-audio hierarchy: TrueHD > DTS-MA > FLAC/PCM > DTS > EAC3/Atmos >
AAC/AC3/MP3 > MP2 > WMA Pro. Lossless codecs score noticeably higher than lossy
counterparts, but audio codec scores are smaller in magnitude than video codec and
resolution scores, so audio is a tiebreaker rather than a deciding factor.

---

### HDR and Dolby Vision

Config keys: `HDR_SCORE` (default `3000`), `DOLBY_VISION_SCORE` (default `5000`)

Both are detected by inspecting Plex video stream metadata — no extra I/O beyond what the
Plex API already provides. HDR is identified by `colorTrc` values `smpte2084` or
`arib-std-b67`. Dolby Vision is identified by `DOVIPresent`.

| Signal | Score bonus |
|---|---|
| HDR | +3000 |
| Dolby Vision | +5000 |

Dolby Vision is treated as a superset of HDR — a DV file that also has HDR metadata receives
both bonuses. These bonuses are large enough to tip most 1080p vs 4K HDR decisions in favour
of the HDR version but not so large that they override a remux vs. HDTV rip decision.

---

### Subtitle and Audio Tracks

Config keys: `SUBTITLE_SCORE_PER_TRACK` (default `50`), `AUDIO_TRACK_SCORE` (default `100`)

| Signal | Formula |
|---|---|
| Subtitle tracks | `subtitle_count × 50` |
| Audio tracks | `audio_track_count × 100` |

More tracks indicate a richer, more complete file. A file with 5 subtitle languages and 3
audio tracks contributes `5×50 + 3×100 = 550` to the total — a small but measurable reward
for completeness. These per-track scores are deliberately small so they act as tiebreakers
rather than reversing a clear codec or resolution advantage.

---

### Filename Patterns

Config key: `FILENAME_SCORES` (dict of `fnmatch` glob pattern → score integer)

Patterns are matched case-insensitively against the **basename** of each file path using
`fnmatch`. Multiple patterns can match the same file; their scores are summed. This allows
the filename to encode source and quality signals that are not directly available from Plex
metadata — for example, `*Remux*` reliably identifies lossless remux files, and `*HDTV*`
identifies lower-quality TV captures.

Example: `Movie.2021.1080p.BluRay.Remux.mkv` would match both `*Remux*` (+25000) and
`*1080p*BluRay*` (+15000), contributing +40000 to the total score.

| Pattern | Score |
|---|---|
| `*Remux*` | 25000 |
| `*bluray-2160p` | 22000 |
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
| `*.mp4` | 500 |
| `*EXTENDED*` | 500 |
| `*DVDRip*` | -3000 |
| `*dvd*` | -3000 |
| `*HDTV*` | -5000 |
| `*TS*` | -5000 |
| `*.ts` | -5000 |
| `*.wmv` | -8000 |
| `*.avi` | -10000 |
| `*.vob` | -10000 |
| `*.flv` | -10000 |
| `*CAM*` | -20000 |

Container penalties (`.avi`, `.vob`, `.flv`) are negative and large enough to override most
codec and resolution advantages. CAM recordings receive the largest penalty in the table.

---

### Bitrate

Formula: `int(video_bitrate * 0.5)`

Bitrate is weighted at **0.5×** (half of the raw value) intentionally. Raw bitrate rewards
large, inefficient encodes — a bloated H264 rip at 20 Mbps would accumulate 20000 points from
bitrate alone, potentially outscoring a well-encoded HEVC file at 8 Mbps. Halving the weight
means bitrate acts as a tiebreaker between otherwise equal candidates rather than a primary
signal.

Combined with `MAX_SIZE_RATIO`, this design stops a large H264 encode from outranking an
efficient HEVC encode of the same content.

---

### File Size

Config key: `SCORE_FILESIZE` (default `False`)

Formula when enabled: `int(file_size / 100000)`

**Default is disabled.** File size is a proxy for bitrate, and bitrate is already scored at
0.5×. Enabling `SCORE_FILESIZE` would further reward large files even when their size is a
product of codec inefficiency rather than quality. For modern storage-efficient libraries,
codec, resolution, and filename signals are more reliable quality indicators.

When to enable: if you consistently want to break ties in favour of larger files — for
example, in a library where all files use the same codec and resolution, and larger means
more data was preserved from the source.

---

### Other Minor Contributions

These components produce small scores that act as tiebreakers between otherwise near-equal
candidates.

| Component | Formula | Rationale |
|---|---|---|
| Video dimensions | `video_width × 2 + video_height × 2` | Rewards actual pixel dimensions beyond the resolution label; a 1920×1080 file beats a 1280×720 file within the same `1080` bucket |
| Video duration | `int(video_duration / 300)` | Slight bonus for longer or more complete versions; helps identify truncated or cut files |
| Audio channels | `audio_channels × 1000` | Surround audio (6–8 channels) is strongly preferred over stereo (2 channels); 7.1 audio contributes 8000 points |

---

## Score Breakdown

`get_score()` returns `(int, dict)`. The integer is the total score. The dictionary contains
a per-component breakdown. Keys are only present when their contribution is non-zero (except
`audio_codec`, `video_codec`, `resolution`, `filename`, `bitrate`, `duration`, and
`dimensions`, which are always present).

| Key | When present | Content |
|---|---|---|
| `audio_codec` | Always | Score from `AUDIO_CODEC_SCORES` lookup |
| `video_codec` | Always | Score from `VIDEO_CODEC_SCORES` lookup |
| `resolution` | Always | Score from `VIDEO_RESOLUTION_SCORES` lookup |
| `filename` | Always | Sum of all matching `FILENAME_SCORES` patterns |
| `filename_matches` | When patterns matched | List of `{'pattern': str, 'score': int}` |
| `bitrate` | Always | `int(video_bitrate * 0.5)` |
| `duration` | Always | `int(video_duration / 300)` |
| `dimensions` | Always | `video_width * 2 + video_height * 2` |
| `audio_channels` | Always | `audio_channels * 1000` |
| `hdr` | When `has_hdr=True` | Value of `HDR_SCORE` config key |
| `dolby_vision` | When `has_dv=True` | Value of `DOLBY_VISION_SCORE` config key |
| `subtitle_tracks` | When non-zero | `subtitle_count * SUBTITLE_SCORE_PER_TRACK` |
| `audio_tracks` | When non-zero | `audio_track_count * AUDIO_TRACK_SCORE` |
| `file_size` | When `SCORE_FILESIZE=True` | `int(file_size / 100000)` |

Example breakdown for a 4K HDR Remux file:

```json
{
  "video_codec": 12000,
  "resolution": 20000,
  "audio_codec": 4500,
  "hdr": 3000,
  "filename": 25000,
  "filename_matches": [{"pattern": "*Remux*", "score": 25000}],
  "bitrate": 4200,
  "duration": 1440,
  "dimensions": 8560,
  "audio_channels": 8000,
  "audio_tracks": 100
}
```

---

## Example Keeper Decisions

### Example 1: 1080p H264 WEB-DL vs 1080p HEVC BluRay Remux

| Component | H264 WEB-DL | HEVC Remux |
|---|---|---|
| Video codec | 8000 (h264) | 12000 (hevc) |
| Resolution | 10000 (1080) | 10000 (1080) |
| Audio codec | 1000 (aac) | 4500 (truehd) |
| Filename | 12000 (`*1080p*WEB-DL*`) | 40000 (`*Remux*` + `*1080p*BluRay*`) |
| Bitrate | 3500 | 6000 |
| `*.mkv` | 2000 | 2000 |
| **Total** | **36500** | **74500** |

**Winner: HEVC Remux.** The Remux filename bonus (+25000) combined with the BluRay pattern
(+15000) and the codec advantage produce a decisive margin. Even without the filename signals,
the TrueHD audio and HEVC codec alone add +7500 over the WEB-DL.

---

### Example 2: 720p HDTV vs 1080p HDR WEB-DL

| Component | 720p HDTV | 1080p HDR WEB-DL |
|---|---|---|
| Video codec | 8000 (h264) | 12000 (hevc) |
| Resolution | 5000 (720) | 10000 (1080) |
| Audio codec | 1000 (ac3) | 1250 (eac3) |
| Filename | -5000 (`*HDTV*`) | 12000 (`*1080p*WEB-DL*`) |
| HDR | 0 | 3000 |
| Bitrate | 1500 | 2500 |
| **Total** | **10500** | **40750** |

**Winner: 1080p HDR WEB-DL.** The HDTV penalty (-5000) combined with the resolution gap and
HDR bonus results in a 30250-point margin. The HDR bonus alone (+3000) exceeds the entire
score of many low-quality components.

---

### Example 3: Near-tie (MIN_SCORE_DIFFERENCE protection)

Two 1080p WEB-DL H264 files from different sources with similar bitrates:

| Component | File A | File B |
|---|---|---|
| Video codec | 8000 | 8000 |
| Resolution | 10000 | 10000 |
| Audio codec | 1000 | 1250 |
| Filename | 12000 | 12000 |
| Bitrate | 3200 | 3700 |
| `*.mkv` | 2000 | 2000 |
| **Total** | **36200** | **36950** |

Score delta: **750**

- With `MIN_SCORE_DIFFERENCE=1000`: the delta (750) is below the threshold — the group is
  **skipped entirely**. Both files are left untouched.
- With `MIN_SCORE_DIFFERENCE=0`: File B is kept (marginally higher bitrate and audio codec).
  This is lower-confidence — the difference may be noise from Plex metadata rather than a
  genuine quality gap.

Setting a non-zero `MIN_SCORE_DIFFERENCE` is the recommended way to require a clear winner
before any action is taken.

---

## Tuning Recommendations

| Goal | Recommendation |
|---|---|
| Require a clear winner before acting | Set `MIN_SCORE_DIFFERENCE >= 1000` |
| Protect against removing large remux files | Keep `MAX_SIZE_RATIO` at `5.0` or lower |
| 4K HDR-focused library | Increase `HDR_SCORE` (e.g. 5000) and `DOLBY_VISION_SCORE` (e.g. 8000) |
| MKV-only library | The `*.mkv` (+2000) filename pattern already handles containers; no changes needed |
| Strongly prefer lossless audio | Increase `AUDIO_TRACK_SCORE` or add a custom pattern for audio codec file naming conventions |
| Break ties in favour of larger files | Enable `SCORE_FILESIZE=True` only after verifying your library is codec-consistent |
| Avoid scoring bloated H264 over HEVC | Do not raise the `h264` VIDEO_CODEC_SCORES value above `hevc` |
| Validate scoring without making changes | Run with `AUDIT_MODE=True` — forces `DRY_RUN=True` at runtime without modifying `config.json` |
| Inspect what score each file received | Check the plan file in `plans/` after a run — every candidate's `score_breakdown` is recorded there |
