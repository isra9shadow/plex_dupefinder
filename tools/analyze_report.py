#!/usr/bin/env python3
"""Analyse a real dupefinder plan/report JSON and simulate the proposed scoring.

READ-ONLY. Does not touch Plex, your config, or production scoring. The "new"
scores are recomputed from each candidate's stored attributes using the PROPOSED
constants below — independently of the live config — so you get evidence before
changing anything.

Produces:
  * keeper statistics (AVC/HEVC, HDR, DV, REMUX, source mix)
  * score-difference distribution (groups below 1000 / 2000)
  * questionable-decision detection (AVC>HEVC, filename-driven, source anomalies,
    HDR/DV anomalies, audio-track inflation, near-ties)
  * old-keeper vs new-keeper comparison under the proposed scoring

Usage:
    python tools/analyze_report.py plans/dupefinder_plan_<id>.json
    python tools/analyze_report.py reports/dupefinder_report_<id>.json --top 50
"""
import argparse
import json
import os
import re
import sys
from fnmatch import fnmatch

# --------------------------------------------------------------------------- #
# PROPOSED scoring (simulation only — not written anywhere)
# --------------------------------------------------------------------------- #
RES = {'4k': 20000, '1080': 10000, '720': 5000, '480': 3000, 'sd': 1000, 'unknown': 0}
CODEC = {'av1': 14000, 'hevc': 12000, 'h265': 12000, 'x265': 12000,
         'h264': 8000, 'x264': 8000, 'avc': 8000, 'vp9': 6000, 'unknown': 0,
         'mpeg4': -3000, 'vc1': -2000, 'mpeg1video': -5000, 'mpeg2video': -5000,
         'wmv2': -8000, 'wmv3': -8000, 'msmpeg4': -8000, 'msmpeg4v2': -8000, 'msmpeg4v3': -8000}
SOURCE = {'remux': 8000, 'bluray': 3000, 'web-dl': 2000, 'webrip': 1000,
          'hdtv': -3000, 'dvd': -3000, 'cam': -15000}
SOURCE_DETECT = [
    ('remux',  ('remux', 'bdremux', 'brremux'), ()),
    ('bluray', ('bluray', 'bdrip', 'brrip'), ('blu ray',)),
    ('web-dl', ('webdl',), ('web dl',)),
    ('webrip', ('webrip',), ('web rip',)),
    ('hdtv',   ('hdtv', 'pdtv', 'hdrip', 'dsr'), ()),
    ('dvd',    ('dvdrip', 'dvd'), ()),
    ('cam',    ('cam', 'hdcam', 'telesync', 'telecine', 'hdts'), ()),
]
FILENAME = {'*.mkv': 800, '*.mp4': 300, '*repack*': 500, '*proper*': 500, '*extended*': 500,
            '*.avi': -10000, '*.ts': -5000, '*.vob': -10000, '*.wmv': -8000, '*.flv': -10000}
FILENAME_CAP = 2000
BITRATE_WEIGHT = 0.1
HDR_SCORE, DV_SCORE = 3000, 5000


def detect_source(files):
    text = ' '.join(os.path.basename(str(f)).lower() for f in (files or []))
    norm = re.sub(r'[._\-]+', ' ', text)
    toks = set(norm.split())
    for key, words, phrases in SOURCE_DETECT:
        if any(w in toks for w in words) or any(p in norm for p in phrases):
            return SOURCE.get(key, 0), key
    return 0, None


def is_avc(codec):
    return str(codec or '').lower() in ('h264', 'x264', 'avc')


def is_hevc(codec):
    return str(codec or '').lower() in ('hevc', 'h265', 'x265')


def candidate_files(p):
    return p.get('files') or p.get('file') or []


def new_score(p):
    """Recompute a candidate's score under the PROPOSED model from stored data."""
    b = p.get('score_breakdown') or {}
    files = candidate_files(p)
    res = RES.get(str(p.get('video_resolution', '')).lower(), 0)
    codec = CODEC.get(str(p.get('video_codec', '')).lower(), 0)
    acodec = int(b.get('audio_codec', 0) or 0)          # audio codec scoring unchanged
    src, _ = detect_source(files)
    fn = 0
    for pat, sc in FILENAME.items():
        if any(fnmatch(os.path.basename(str(f).lower()), pat) for f in files):
            fn += sc
    if FILENAME_CAP > 0 and fn > FILENAME_CAP:
        fn = FILENAME_CAP
    br = int((p.get('video_bitrate') or 0) * BITRATE_WEIGHT)
    dur = int(b.get('duration', 0) or 0)
    dims = int(b.get('dimensions', 0) or 0)
    sum_ch = int(b.get('audio_channels', 0) or 0) // 1000    # only the OLD sum is stored
    new_ch = min(sum_ch, 8) * 1000                            # approx MAX (real MAX unknown)
    hdr = HDR_SCORE if p.get('has_hdr') else 0
    dv = DV_SCORE if p.get('has_dv') else 0
    subs = int(b.get('subtitle_tracks', 0) or 0)
    atr = int(b.get('audio_tracks', 0) or 0)
    return res + codec + acodec + src + fn + br + dur + dims + new_ch + hdr + dv + subs + atr


def normalize_groups(doc):
    groups = []
    for g in doc.get('groups', []):
        parts = g.get('parts') or g.get('discovery_candidates') or []
        decision = g.get('decision') or g.get('discovery_decision') or {}
        groups.append({'title': g.get('title'), 'item_key': g.get('item_key'),
                       'parts': parts, 'decision': decision})
    return groups


def existing(parts):
    return [p for p in parts if p.get('exists', True)]


def argmax(parts, keyfn):
    best = None
    for p in parts:
        if best is None or keyfn(p) > keyfn(best):
            best = p
    return best


def main():
    ap = argparse.ArgumentParser(description="Analyse a dupefinder plan/report and simulate proposed scoring.")
    ap.add_argument('report')
    ap.add_argument('--top', type=int, default=50)
    args = ap.parse_args()
    if not os.path.isfile(args.report):
        print("Not found: %s" % args.report)
        sys.exit(2)

    groups = normalize_groups(json.load(open(args.report, encoding='utf-8')))

    stats = dict.fromkeys(('groups', 'multi', 'avc_keep', 'hevc_keep', 'other_keep',
                           'hdr_keep', 'dv_keep', 'remux_keep', 'flips',
                           'below_1000', 'below_2000', 'avc_over_hevc',
                           'audio_inflation', 'source_anomaly'), 0)
    src_keep = {}
    deltas = []
    questionable = []

    for g in groups:
        stats['groups'] += 1
        cands = existing(g['parts'])
        if len(cands) < 2:
            continue
        stats['multi'] += 1

        old_keeper = argmax(cands, lambda p: int(p.get('score') or 0))
        old_sorted = sorted((int(p.get('score') or 0) for p in cands), reverse=True)
        old_delta = old_sorted[0] - old_sorted[1]
        deltas.append(old_delta)
        if old_delta < 1000:
            stats['below_1000'] += 1
        if old_delta < 2000:
            stats['below_2000'] += 1

        new_keeper = argmax(cands, new_score)
        flip = old_keeper.get('media_id') != new_keeper.get('media_id')
        if flip:
            stats['flips'] += 1

        kc = old_keeper.get('video_codec')
        if is_avc(kc):
            stats['avc_keep'] += 1
        elif is_hevc(kc):
            stats['hevc_keep'] += 1
        else:
            stats['other_keep'] += 1
        if old_keeper.get('has_hdr'):
            stats['hdr_keep'] += 1
        if old_keeper.get('has_dv'):
            stats['dv_keep'] += 1
        _, ks = detect_source(candidate_files(old_keeper))
        src_keep[ks or 'none'] = src_keep.get(ks or 'none', 0) + 1
        if ks == 'remux':
            stats['remux_keep'] += 1

        # --- anomaly flags ---
        reasons = []
        # AVC keeper while an HEVC sibling exists at >= same resolution
        if is_avc(kc):
            for p in cands:
                if is_hevc(p.get('video_codec')) and \
                        RES.get(str(p.get('video_resolution', '')).lower(), 0) >= \
                        RES.get(str(old_keeper.get('video_resolution', '')).lower(), 0):
                    stats['avc_over_hevc'] += 1
                    reasons.append("AVC keeper over HEVC sibling")
                    break
        # source anomaly: keeper has a worse source than a sibling
        ks_score, _ = detect_source(candidate_files(old_keeper))
        for p in cands:
            ps, _ = detect_source(candidate_files(p))
            if ps - ks_score >= 3000:
                stats['source_anomaly'] += 1
                reasons.append("sibling has better source")
                break
        # audio inflation: keeper summed channels > 8 (multi-track summing)
        kb = old_keeper.get('score_breakdown') or {}
        if int(kb.get('audio_channels', 0) or 0) // 1000 > 8:
            stats['audio_inflation'] += 1
            reasons.append("audio-channel inflation (sum>8)")
        if old_delta < 1000:
            reasons.append("near-tie (delta<1000)")
        if flip:
            reasons.append("keeper FLIPS under proposed scoring")

        if reasons:
            questionable.append({
                'title': g['title'], 'old_delta': old_delta,
                'old_keeper': old_keeper.get('media_id'),
                'new_keeper': new_keeper.get('media_id'),
                'old_codec': kc, 'reasons': reasons,
                'files': [os.path.basename(str(f)) for f in candidate_files(old_keeper)],
            })

    print("=" * 70)
    print("DUPEFINDER SCORING ANALYSIS - %s" % os.path.basename(args.report))
    print("=" * 70)
    print("Groups total                  : %d" % stats['groups'])
    print("Groups with >=2 candidates    : %d" % stats['multi'])
    print("-" * 70)
    print("Keeper codec   AVC=%d  HEVC=%d  other=%d"
          % (stats['avc_keep'], stats['hevc_keep'], stats['other_keep']))
    print("Keeper HDR=%d   DV=%d   REMUX=%d"
          % (stats['hdr_keep'], stats['dv_keep'], stats['remux_keep']))
    print("Keeper source mix            : %s"
          % ', '.join('%s=%d' % (k, v) for k, v in sorted(src_keep.items())))
    if deltas:
        print("Avg old score difference     : %d" % (sum(deltas) // len(deltas)))
    print("Groups below 1000 delta      : %d" % stats['below_1000'])
    print("Groups below 2000 delta      : %d" % stats['below_2000'])
    print("-" * 70)
    print("ANOMALIES (old scoring):")
    print("  AVC over equivalent HEVC   : %d" % stats['avc_over_hevc'])
    print("  Sibling better source      : %d" % stats['source_anomaly'])
    print("  Audio-channel inflation    : %d" % stats['audio_inflation'])
    print("Keeper FLIPS under proposed  : %d / %d multi-candidate groups"
          % (stats['flips'], stats['multi']))
    print("=" * 70)

    questionable.sort(key=lambda q: (len(q['reasons']), -q['old_delta']), reverse=True)
    print("\nTOP %d QUESTIONABLE DECISIONS:" % args.top)
    for q in questionable[:args.top]:
        print("\n  %s  [delta=%d, codec=%s, keeper %s->%s]"
              % (q['title'], q['old_delta'], q['old_codec'], q['old_keeper'], q['new_keeper']))
        print("    reasons : %s" % '; '.join(q['reasons']))
        print("    keeper  : %s" % (q['files'][0] if q['files'] else '?'))


if __name__ == "__main__":
    main()
