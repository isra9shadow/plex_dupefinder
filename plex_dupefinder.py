#!/usr/bin/env python3
"""
plex_dupefinder — safety-first duplicate finder/cleaner for Plex.

Two-pass design (the core safety guarantee):

    PASS 1 — DISCOVERY
        gather duplicate groups, score, decide a tentative keeper, and
        snapshot every group into a JSON plan file. No file or Plex
        write is performed in this pass.

    PASS 2 — REVALIDATION & ACTION
        for each planned group, re-fetch from Plex, recompute scores,
        re-check filesystem, re-run the decision, and ONLY act if the
        fresh state matches the snapshot. Any drift skips the group.

Modes:
    DRY_RUN=True             (default) → simulate everything
    QUARANTINE_MODE=True     (default) → move to QUARANTINE_DIR
    DRY_RUN=False, QUARANTINE_MODE=False → direct Plex DELETE: with Plex's
        "Allow media deletion" enabled (required), Plex removes the file
        from disk. Irreversible — no quarantine, no sidecar, no restore.

Filesystem is authoritative; Plex metadata is informational.
Prefer false negatives over false positives.
"""
import hashlib
import json
import logging
import os
import re
import shlex
import shutil
import sys
import time
import uuid
from datetime import datetime, timezone
from fnmatch import fnmatch
from logging.handlers import RotatingFileHandler

from tabulate import tabulate

from config import cfg

try:
    from urlparse import urljoin
except ImportError:
    from urllib.parse import urljoin

from plexapi.server import PlexServer
import requests


############################################################
# INIT
############################################################

SCRIPT_DIR = os.path.dirname(os.path.realpath(sys.argv[0]))
log_filename = os.path.join(SCRIPT_DIR, 'activity.log')
decision_filename = os.path.join(SCRIPT_DIR, 'decisions.log')
default_plans_dir = os.path.join(SCRIPT_DIR, 'plans')

# Rotating log so unattended/scheduled runs on large libraries cannot grow
# activity.log without bound. 10 MiB × 5 backups = 60 MiB ceiling. Level is
# configurable via LOG_LEVEL (default INFO); set DEBUG for per-part tracing.
LOG_MAX_BYTES = 10 * 1024 * 1024
LOG_BACKUP_COUNT = 5

log = logging.getLogger("Plex_Dupefinder")
# NullHandler keeps importers (tests/tooling) quiet until setup_logging() runs.
log.addHandler(logging.NullHandler())


def setup_logging():
    """Attach the size-rotated file handler for activity.log.

    Called from the entrypoint (not at import) so importing this module — e.g.
    from the test suite — has no side effects and creates no log file.
    """
    level = getattr(logging, str(cfg.get('LOG_LEVEL', 'INFO')).upper(), logging.INFO)
    handler = RotatingFileHandler(
        log_filename, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT, encoding='utf-8'
    )
    handler.setFormatter(logging.Formatter(
        fmt='[%(asctime)s] %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
    ))
    logging.basicConfig(level=level, handlers=[handler])
    logging.getLogger('urllib3.connectionpool').disabled = True

REQUESTS_TIMEOUT = int(cfg.get('REQUESTS_TIMEOUT', 30))
REDACTED_KEYS = ('PLEX_TOKEN', 'RADARR_API_KEY', 'SONARR_API_KEY')

run_id = uuid.uuid4().hex[:12]
run_started_at = datetime.now(timezone.utc).isoformat()
run_report = {
    'run_id': run_id,
    'started_at': run_started_at,
    'finished_at': None,
    'config': {k: ('<redacted>' if k in REDACTED_KEYS else v) for k, v in cfg.items()},
    'phases': {
        'pass0': None,
        'discovery': None,
        'revalidation': None,
        'action': None,
    },
    'groups': [],
    'integrations': {},
    'summary': {},
    'quarantine': None,
    'errors': [],
}


############################################################
# CONFIG VALIDATION
############################################################


def validate_config():
    """Abort with clear messages when config would lead to unsafe behaviour."""
    errors = []
    notices = []

    if not cfg.get('PLEX_SERVER'):
        errors.append("PLEX_SERVER is not set")
    if not cfg.get('PLEX_TOKEN'):
        errors.append("PLEX_TOKEN is not set")
    if not cfg.get('PLEX_LIBRARIES'):
        errors.append("PLEX_LIBRARIES is empty")

    dry_run = bool(cfg.get('DRY_RUN', True))
    audit_mode = bool(cfg.get('AUDIT_MODE', False))
    quarantine_mode = bool(cfg.get('QUARANTINE_MODE', True))
    quarantine_dir = (cfg.get('QUARANTINE_DIR') or '').strip()

    # AUDIT_MODE behaves like DRY_RUN — no actions, no quarantine setup needed.
    if not dry_run and not audit_mode and quarantine_mode:
        if not quarantine_dir:
            errors.append("QUARANTINE_MODE is True but QUARANTINE_DIR is empty")
        elif not os.path.isdir(quarantine_dir):
            try:
                os.makedirs(quarantine_dir, exist_ok=True)
                notices.append("Created QUARANTINE_DIR: %s" % quarantine_dir)
            except OSError as e:
                errors.append("QUARANTINE_DIR cannot be created (%s): %s" % (quarantine_dir, e))
        elif not os.access(quarantine_dir, os.W_OK):
            errors.append("QUARANTINE_DIR is not writable: %s" % quarantine_dir)

    if cfg.get('RADARR_RESCAN_AFTER') and (not cfg.get('RADARR_URL') or not cfg.get('RADARR_API_KEY')):
        errors.append("RADARR_RESCAN_AFTER is True but RADARR_URL or RADARR_API_KEY is missing")
    if cfg.get('SONARR_RESCAN_AFTER') and (not cfg.get('SONARR_URL') or not cfg.get('SONARR_API_KEY')):
        errors.append("SONARR_RESCAN_AFTER is True but SONARR_URL or SONARR_API_KEY is missing")

    json_report_dir = (cfg.get('JSON_REPORT_DIR') or '').strip()
    if json_report_dir and not os.path.isdir(json_report_dir):
        try:
            os.makedirs(json_report_dir, exist_ok=True)
            notices.append("Created JSON_REPORT_DIR: %s" % json_report_dir)
        except OSError as e:
            notices.append("JSON_REPORT_DIR cannot be created (%s); reports will be skipped" % e)

    try:
        os.makedirs(default_plans_dir, exist_ok=True)
    except OSError as e:
        notices.append("Plans dir cannot be created (%s); discovery plans will be skipped" % e)

    for n in notices:
        log.info(n)
        print("[*] %s" % n)
    if errors:
        for e in errors:
            log.error("Config error: %s", e)
            print("[X] Config error: %s" % e)
        print("\nAborting due to config errors.")
        sys.exit(2)


# Plex client — connected at runtime by connect_plex() from the entrypoint, so
# importing this module never opens a network connection. None until then.
plex = None


def connect_plex():
    """Connect to the configured Plex server, or exit with a clear message."""
    try:
        return PlexServer(cfg['PLEX_SERVER'], cfg['PLEX_TOKEN'])
    except Exception:
        log.exception("Exception connecting to Plex server %r", cfg['PLEX_SERVER'])
        print("Exception connecting to %s — see activity.log" % cfg['PLEX_SERVER'])
        sys.exit(1)


def validate_libraries(plex_server):
    """Abort if any configured PLEX_LIBRARIES name does not exist on the server.

    A typo'd library name would otherwise be swallowed in the discovery loop
    and the run would silently do nothing for it — a common, hard-to-notice
    mistake on unattended schedules. Fail fast with the list of valid names.
    """
    configured = cfg.get('PLEX_LIBRARIES') or []
    try:
        available = {section.title for section in plex_server.library.sections()}
    except Exception:
        log.exception("Failed to enumerate Plex library sections")
        print("Exception listing Plex libraries — see activity.log")
        sys.exit(1)

    missing = [name for name in configured if name not in available]
    if missing:
        log.error("Configured libraries not found on Plex: %s (available: %s)",
                  missing, sorted(available))
        print("[X] Configured libraries not found on Plex server: %s"
              % ", ".join(repr(m) for m in missing))
        print("    Available libraries: %s"
              % (", ".join(repr(a) for a in sorted(available)) or "(none)"))
        print("\nAborting — fix PLEX_LIBRARIES in config.json.")
        sys.exit(2)

    log.info("Validated %d configured libraries against Plex: %s",
             len(configured), configured)


############################################################
# FORMATTING HELPERS
############################################################


def millis_to_string(millis):
    try:
        seconds = int((millis / 1000) % 60)
        minutes = int((millis / (1000 * 60)) % 60)
        hours = (millis / (1000 * 60 * 60)) % 24
        return "%02d:%02d:%02d" % (hours, minutes, seconds)
    except Exception:
        log.exception("Exception converting %s millis to string", millis)
    return "%d ms" % millis


def bytes_to_string(size_bytes):
    try:
        if size_bytes == 1:
            return "1 byte"
        suffixes_table = [('bytes', 0), ('KB', 0), ('MB', 1), ('GB', 2), ('TB', 2), ('PB', 2)]
        num = float(size_bytes)
        for suffix, precision in suffixes_table:
            if num < 1024.0:
                break
            num /= 1024.0
        formatted = "%d" % num if precision == 0 else str(round(num, ndigits=precision))
        return "%s %s" % (formatted, suffix)
    except Exception:
        log.exception("Exception converting %s bytes to string", size_bytes)
    return "%d bytes" % size_bytes


def kbps_to_string(size_kbps):
    try:
        if size_kbps < 1024:
            return "%d Kbps" % size_kbps
        return "{:.2f} Mbps".format(size_kbps / 1024.)
    except Exception:
        log.exception("Exception converting %s Kbps to string", size_kbps)
    return "%d Kbps" % size_kbps


############################################################
# EXISTENCE & HASHING
############################################################


def check_file_exists(file_path, plex_exists=None, plex_accessible=None):
    """
    Determine whether a media part is actually present.

    Filesystem is authoritative. When both Plex and the filesystem report,
    BOTH must agree the file exists. Any disagreement is treated as MISSING.
    """
    plex_claim = None
    if plex_exists is not None:
        plex_claim = bool(plex_exists)
    elif plex_accessible is not None:
        plex_claim = bool(plex_accessible)

    local_claim = None
    local_fs_accessible = False
    try:
        parent = os.path.dirname(file_path) if file_path else None
        if parent and os.path.isdir(parent):
            local_fs_accessible = True
            local_claim = os.path.exists(file_path)
        elif file_path and os.path.exists(file_path):
            local_fs_accessible = True
            local_claim = True
    except (OSError, ValueError):
        pass

    if local_fs_accessible and plex_claim is not None:
        exists = bool(local_claim and plex_claim)
        if local_claim != plex_claim:
            reason = ("DISAGREEMENT local=%s plex=%s — treating as MISSING for safety"
                      % (local_claim, plex_claim))
        else:
            reason = "local=%s, plex=%s" % (local_claim, plex_claim)
    elif local_fs_accessible:
        exists = bool(local_claim)
        reason = "local-only check: %s (Plex did not report)" % local_claim
    elif plex_claim is not None:
        exists = plex_claim
        reason = "plex-only check: %s (filesystem not reachable from this host)" % plex_claim
    else:
        exists = True
        reason = "UNKNOWN — no info from Plex or filesystem, assuming exists"

    return {
        'exists': exists,
        'local_check': local_claim,
        'plex_check': plex_claim,
        'reason': reason,
    }


def get_file_age_hours(file_path):
    """Return age in hours via mtime, or None if not locally readable."""
    try:
        if file_path and os.path.exists(file_path):
            return (time.time() - os.path.getmtime(file_path)) / 3600.0
    except OSError:
        pass
    return None


def is_files_stable(file_paths, wait_seconds):
    """
    Verify that no file in `file_paths` changes size during a brief wait.

    Returns (stable: bool, changes: list of dicts). A file we cannot read
    at all is left to the existence/sanity layers; only files whose size
    moved are reported as unstable.
    """
    if wait_seconds <= 0 or not file_paths:
        return True, []

    before = {}
    for p in file_paths:
        try:
            before[p] = os.path.getsize(p)
        except OSError:
            before[p] = None

    time.sleep(wait_seconds)

    changes = []
    for p in file_paths:
        try:
            after_size = os.path.getsize(p)
        except OSError:
            after_size = None
        if before[p] is not None and before[p] != after_size:
            changes.append({
                'file': p,
                'size_before': before[p],
                'size_after': after_size,
            })
    return (not changes), changes


def compute_partial_hashes(file_path, hash_bytes=None):
    """
    Cheap consistency hash: SHA-256 of the first N and last N bytes of the
    file plus its size. Used to detect mid-write/transcode between passes.

    Returns dict or None if unreadable.
    """
    if hash_bytes is None:
        hash_bytes = int(cfg.get('PARTIAL_HASH_BYTES', 1024 * 1024))
    if not file_path:
        return None
    try:
        size = os.path.getsize(file_path)
        with open(file_path, 'rb') as f:
            head = f.read(hash_bytes)
            tail = b''
            if size > hash_bytes:
                f.seek(max(size - hash_bytes, hash_bytes))
                tail = f.read(hash_bytes)
        return {
            'size': size,
            'head_sha256': hashlib.sha256(head).hexdigest() if head else None,
            'tail_sha256': hashlib.sha256(tail).hexdigest() if tail else None,
        }
    except OSError as e:
        log.warning("Partial hash failed for %r: %s", file_path, e)
        return None


############################################################
# PLEX METHODS
############################################################


def get_dupes(plex_section_name):
    sec_type = get_section_type(plex_section_name)
    dupe_search_results = plex.library.section(plex_section_name).search(duplicate=True, libtype=sec_type)

    filtered = dupe_search_results.copy()
    if cfg['FIND_DUPLICATE_FILEPATHS_ONLY']:
        for dupe in dupe_search_results:
            if any(x != dupe.locations[0] for x in dupe.locations):
                filtered.remove(dupe)
    return filtered


def get_section_type(plex_section_name):
    try:
        plex_section_type = plex.library.section(plex_section_name).type
    except Exception:
        log.exception("Exception looking up section type for library %r", plex_section_name)
        print("Exception looking up section type for %r — see activity.log" % plex_section_name)
        sys.exit(1)
    return 'episode' if plex_section_type == 'show' else 'movie'


############################################################
# PASS 0 — OPTIONAL METADATA REFRESH
############################################################


def _item_metadata_sane(item):
    """
    Quick sanity check on a plexapi Item's media metadata.

    Returns True only if every Media has parts, valid bitrate, duration,
    codec and a non-empty file path. Used to decide when PASS 0 polling
    can stop.
    """
    media_list = getattr(item, 'media', None) or []
    if not media_list:
        return False
    for media in media_list:
        parts = getattr(media, 'parts', None) or []
        if not parts:
            return False
        bitrate = getattr(media, 'bitrate', None) or 0
        duration = getattr(media, 'duration', None) or 0
        codec = (getattr(media, 'videoCodec', '') or '').strip().lower()
        if bitrate <= 0 or duration <= 0 or not codec or codec == 'unknown':
            return False
        for part in parts:
            if not getattr(part, 'file', None):
                return False
    return True


def _snapshot_media_metadata(item):
    """
    Capture a compact fingerprint of an item's media metadata.

    Used as the "before" reference for detecting whether analyze()
    actually mutated the data on the Plex side. Stored only ephemerally
    in memory — not written to disk.
    """
    snapshot = {
        'updatedAt': str(getattr(item, 'updatedAt', None)),
        'media': [],
    }
    for media in getattr(item, 'media', None) or []:
        snapshot['media'].append({
            'id': getattr(media, 'id', None),
            'bitrate': getattr(media, 'bitrate', None),
            'duration': getattr(media, 'duration', None),
            'videoCodec': getattr(media, 'videoCodec', None),
            'audioCodec': getattr(media, 'audioCodec', None),
            'videoResolution': getattr(media, 'videoResolution', None),
            'width': getattr(media, 'width', None),
            'height': getattr(media, 'height', None),
            'audioChannels': getattr(media, 'audioChannels', None),
            'parts': [
                {
                    'id': getattr(p, 'id', None),
                    'size': getattr(p, 'size', None),
                    'file': getattr(p, 'file', None),
                }
                for p in (getattr(media, 'parts', None) or [])
            ],
        })
    return snapshot


def _snapshot_diff(before, after):
    """Return a list of dotted field paths that changed between snapshots."""
    diffs = []
    if before.get('updatedAt') != after.get('updatedAt'):
        diffs.append('updatedAt')

    b_media = before.get('media') or []
    a_media = after.get('media') or []
    if len(b_media) != len(a_media):
        diffs.append('media_count(%d->%d)' % (len(b_media), len(a_media)))
        return diffs

    media_fields = ('id', 'bitrate', 'duration', 'videoCodec', 'audioCodec',
                    'videoResolution', 'width', 'height', 'audioChannels')
    part_fields = ('id', 'size', 'file')

    for i, (b, a) in enumerate(zip(b_media, a_media)):
        for k in media_fields:
            if b.get(k) != a.get(k):
                diffs.append('media[%d].%s' % (i, k))
        b_parts = b.get('parts') or []
        a_parts = a.get('parts') or []
        if len(b_parts) != len(a_parts):
            diffs.append('media[%d].parts_count(%d->%d)' % (i, len(b_parts), len(a_parts)))
            continue
        for j, (bp, ap) in enumerate(zip(b_parts, a_parts)):
            for k in part_fields:
                if bp.get(k) != ap.get(k):
                    diffs.append('media[%d].parts[%d].%s' % (i, j, k))
    return diffs


def refresh_plex_item(item, timeout_seconds, poll_interval=2.0, max_stable_polls=3):
    """
    Trigger item.analyze() and poll for fresh metadata.

    Returns (item, status_dict). status_dict fields:
        attempted        : True
        success          : True iff caller should proceed to PASS 1
        status           : 'sane_and_changed' | 'sane_unchanged'
                           | 'timeout' | 'analyze_failed'
        reason           : human-readable
        changed_fields   : dotted field paths that differ from the
                           pre-analyze snapshot (empty for 'sane_unchanged')
        timeout_seconds  : the configured cap

    LIMITATIONS — Plex's analyze() is asynchronous and exposes no
    completion signal. We do our best with snapshot-diff detection:

      * 'sane_and_changed' is the only verdict that DEMONSTRABLY proves
        Plex actually re-analysed the file and updated something.
      * 'sane_unchanged' is AMBIGUOUS. It can mean either:
          (a) the metadata was already correct and analyze() found
              nothing to update — fine, scoring will be accurate; or
          (b) the analyze() task was queued but Plex has not yet
              processed it within our poll window — we may still be
              scoring on stale data.
        We cannot distinguish (a) from (b) via the Plex API. We accept
        'sane_unchanged' as success because rejecting it would skip
        every group in a healthy library. Users who need a hard guarantee
        of fresh metadata should run an offline analyser (MediaInfo /
        ffprobe) before invoking this script.
      * 'timeout' / 'analyze_failed' → caller must skip the group.

    To bound runtime, we accept 'sane_unchanged' after `max_stable_polls`
    consecutive polls without observed change.
    """
    status = {
        'attempted': True,
        'success': False,
        'status': None,
        'reason': None,
        'changed_fields': [],
        'timeout_seconds': timeout_seconds,
        'poll_interval_seconds': poll_interval,
    }

    before = _snapshot_media_metadata(item)

    try:
        item.analyze()
    except Exception as e:
        log.exception("PASS0 analyze() failed for key=%r", getattr(item, 'key', None))
        status['status'] = 'analyze_failed'
        status['reason'] = "analyze() raised: %s" % e
        return item, status

    deadline = time.time() + max(timeout_seconds, 0)
    stable_sane_polls = 0

    while time.time() < deadline:
        time.sleep(poll_interval)
        try:
            item.reload()
        except Exception as e:
            log.warning("PASS0 reload() failed key=%r: %s", getattr(item, 'key', None), e)
            continue

        if not _item_metadata_sane(item):
            stable_sane_polls = 0
            continue

        after = _snapshot_media_metadata(item)
        diff = _snapshot_diff(before, after)
        if diff:
            status.update({
                'success': True,
                'status': 'sane_and_changed',
                'reason': 'analyze() updated metadata',
                'changed_fields': diff,
            })
            return item, status

        stable_sane_polls += 1
        if stable_sane_polls >= max_stable_polls:
            status.update({
                'success': True,
                'status': 'sane_unchanged',
                'reason': ('metadata sane and unchanged after %d polls — '
                           'either already current OR analyze() not yet '
                           'processed by Plex (see docstring)'
                           % stable_sane_polls),
                'changed_fields': [],
            })
            return item, status

    status['status'] = 'timeout'
    status['reason'] = "timeout after %.1fs without sane metadata" % timeout_seconds
    return item, status


def _detect_hdr_dv(item):
    """Inspect video streams for HDR / Dolby Vision flags (Plex metadata only)."""
    has_hdr = False
    has_dv = False
    try:
        for part in item.parts:
            for stream in part.videoStreams():
                if getattr(stream, 'DOVIPresent', False):
                    has_dv = True
                ctrc = (getattr(stream, 'colorTrc', '') or '').lower()
                if ctrc in ('smpte2084', 'arib-std-b67'):
                    has_hdr = True
    except Exception:
        log.debug("HDR/DV detection failed", exc_info=True)
    return has_hdr, has_dv


def _count_streams(item):
    """Return (audio_track_count, subtitle_count) across all parts."""
    audio_tracks = 0
    subtitles = 0
    try:
        for part in item.parts:
            audio_tracks += len(part.audioStreams())
            subtitles += len(part.subtitleStreams())
    except Exception:
        log.debug("Stream count failed", exc_info=True)
    return audio_tracks, subtitles


def _max_audio_channels(item):
    """Channel count of the single richest audio track.

    NOT the sum across tracks — summing would inflate a multi-dub release
    (e.g. 7.1 + 5.1 + 2.0 = 16ch) far above an equivalent single-track file.
    """
    max_ch = 0
    try:
        for part in item.parts:
            for stream in part.audioStreams():
                ch = getattr(stream, 'channels', 0) or 0
                if ch > max_ch:
                    max_ch = ch
    except Exception:
        log.debug("Audio channel detection failed", exc_info=True)
    return max_ch


# Source-type detection. Plex exposes no "source" field, so it is parsed from
# the filename — but as a SINGLE first-class value (the highest-quality match
# wins, never summed), unlike the FILENAME_SCORES tie-breakers. Tiers are tried
# best-first; the first match wins (so "BluRay REMUX" scores as remux).
_SOURCE_DETECTORS = (
    # (SOURCE_SCORES key, exact word tokens, multi-word substrings)
    ('remux',  ('remux', 'bdremux', 'brremux'), ()),
    ('bluray', ('bluray', 'bdrip', 'brrip'), ('blu ray',)),
    ('web-dl', ('webdl',), ('web dl',)),
    ('webrip', ('webrip',), ('web rip',)),
    ('hdtv',   ('hdtv', 'pdtv', 'hdrip', 'dsr'), ()),
    ('dvd',    ('dvdrip', 'dvd'), ()),
    ('cam',    ('cam', 'hdcam', 'telesync', 'telecine', 'hdts'), ()),
)


def _source_score(files):
    """Return (score, source_key) for the best source detected in the filenames.

    A single value, never summed. (0, None) when no source is recognised — the
    decision then rests on the real media signals (codec/resolution/HDR/audio).
    """
    source_scores = cfg.get('SOURCE_SCORES', {}) or {}
    text = ' '.join(os.path.basename(str(f)).lower() for f in (files or []))
    norm = re.sub(r'[._\-]+', ' ', text)
    tokens = set(norm.split())
    for key, words, phrases in _SOURCE_DETECTORS:
        if any(w in tokens for w in words) or any(p in norm for p in phrases):
            return int(source_scores.get(key, 0)), key
    return 0, None


def get_score(media_info):
    """
    Return (total_score: int, breakdown: dict).

    The breakdown records every contribution so future audits can explain
    why a file won. Bitrate weight is intentionally 0.5 to keep it as a
    tiebreaker — combined with MAX_SIZE_RATIO it stops bloated H.264
    encodes from outranking efficient HEVC.
    """
    breakdown = {}

    # All score-dict keys are lowercase by convention; lower() on the media
    # value ensures a match even if Plex returns mixed-case codec names.
    audio_codec_pts = int(cfg['AUDIO_CODEC_SCORES'].get(
        media_info['audio_codec'].lower(), 0))
    breakdown['audio_codec'] = audio_codec_pts

    video_codec_pts = int(cfg['VIDEO_CODEC_SCORES'].get(
        media_info['video_codec'].lower(), 0))
    breakdown['video_codec'] = video_codec_pts

    resolution_pts = int(cfg['VIDEO_RESOLUTION_SCORES'].get(
        media_info['video_resolution'].lower(), 0))
    breakdown['resolution'] = resolution_pts

    # Source type — a SINGLE first-class value (highest-quality source wins),
    # parsed from the filename because Plex exposes no source field.
    source_pts, source_key = _source_score(media_info.get('file', []))
    breakdown['source'] = source_pts
    if source_key:
        breakdown['source_type'] = source_key

    # FILENAME_SCORES are tie-breakers only (container + edition tags). The
    # positive sum is clamped by FILENAME_SCORE_CAP so stacking patterns cannot
    # dominate a real media decision; negative legacy-container penalties pass
    # through uncapped (a genuine quality signal).
    filename_pts = 0
    filename_matches = []
    for filename_keyword, keyword_score in cfg['FILENAME_SCORES'].items():
        for filename in media_info['file']:
            if fnmatch(os.path.basename(filename.lower()), filename_keyword.lower()):
                filename_pts += int(keyword_score)
                filename_matches.append({'pattern': filename_keyword, 'score': int(keyword_score)})
    filename_cap = int(cfg.get('FILENAME_SCORE_CAP', 0) or 0)
    if filename_cap > 0 and filename_pts > filename_cap:
        filename_pts = filename_cap
    breakdown['filename'] = filename_pts
    if filename_matches:
        breakdown['filename_matches'] = filename_matches

    # Bitrate is a small tie-breaker: it correlates with codec INEFFICIENCY as
    # much as with quality, so a low weight keeps a bloated AVC from outscoring
    # an efficient HEVC. Tunable via BITRATE_SCORE_WEIGHT.
    bitrate_pts = int(media_info['video_bitrate'] * float(cfg.get('BITRATE_SCORE_WEIGHT', 0.1)))
    breakdown['bitrate'] = bitrate_pts

    duration_pts = int(media_info['video_duration'] / 300)
    breakdown['duration'] = duration_pts

    dimensions_pts = media_info['video_width'] * 2 + media_info['video_height'] * 2
    breakdown['dimensions'] = dimensions_pts

    audio_channel_pts = media_info['audio_channels'] * 1000
    breakdown['audio_channels'] = audio_channel_pts

    total = (audio_codec_pts + video_codec_pts + resolution_pts + source_pts
             + filename_pts + bitrate_pts + duration_pts + dimensions_pts
             + audio_channel_pts)

    if media_info.get('has_hdr'):
        hdr_pts = int(cfg.get('HDR_SCORE', 0))
        breakdown['hdr'] = hdr_pts
        total += hdr_pts
    if media_info.get('has_dv'):
        dv_pts = int(cfg.get('DOLBY_VISION_SCORE', 0))
        breakdown['dolby_vision'] = dv_pts
        total += dv_pts

    subtitle_pts = int(media_info.get('subtitle_count', 0)) * int(cfg.get('SUBTITLE_SCORE_PER_TRACK', 0))
    if subtitle_pts:
        breakdown['subtitle_tracks'] = subtitle_pts
        total += subtitle_pts

    audio_track_pts = int(media_info.get('audio_track_count', 0)) * int(cfg.get('AUDIO_TRACK_SCORE', 0))
    if audio_track_pts:
        breakdown['audio_tracks'] = audio_track_pts
        total += audio_track_pts

    if cfg['SCORE_FILESIZE']:
        size_pts = int(media_info['file_size'] / 100000)
        breakdown['file_size'] = size_pts
        total += size_pts

    return int(total), breakdown


def get_media_info(item, compute_hashes=False):
    info = {
        'id': 'Unknown',
        'video_bitrate': 0,
        'audio_codec': 'Unknown',
        'audio_channels': 0,
        'video_codec': 'Unknown',
        'video_resolution': 'Unknown',
        'video_width': 0,
        'video_height': 0,
        'video_duration': 0,
        'file': [],
        'multipart': False,
        'file_size': 0,
        'has_hdr': False,
        'has_dv': False,
        'subtitle_count': 0,
        'audio_track_count': 0,
    }
    for plex_attr, info_key, default in (
        ('id', 'id', 'Unknown'),
        ('bitrate', 'video_bitrate', 0),
        ('videoCodec', 'video_codec', 'Unknown'),
        ('videoResolution', 'video_resolution', 'Unknown'),
        ('height', 'video_height', 0),
        ('width', 'video_width', 0),
        ('duration', 'video_duration', 0),
        ('audioCodec', 'audio_codec', 'Unknown'),
    ):
        try:
            info[info_key] = getattr(item, plex_attr) or default
        except AttributeError:
            pass

    # Richest single audio track, NOT the sum across tracks (which would
    # artificially inflate multi-dub releases).
    info['audio_channels'] = _max_audio_channels(item) or (getattr(item, 'audioChannels', 0) or 0)

    if len(item.parts) > 1:
        info['multipart'] = True

    info['has_hdr'], info['has_dv'] = _detect_hdr_dv(item)
    info['audio_track_count'], info['subtitle_count'] = _count_streams(item)

    info['parts_existence'] = []
    all_parts_exist = True
    for part in item.parts:
        info['file'].append(part.file)
        info['file_size'] += part.size if part.size else 0

        plex_exists = getattr(part, 'exists', None)
        plex_accessible = getattr(part, 'accessible', None)
        existence = check_file_exists(part.file, plex_exists, plex_accessible)
        existence['file'] = part.file
        existence['age_hours'] = get_file_age_hours(part.file)
        if compute_hashes and cfg.get('PARTIAL_HASH_ENABLED'):
            existence['partial_hash'] = compute_partial_hashes(part.file)
        info['parts_existence'].append(existence)
        if not existence['exists']:
            all_parts_exist = False
            log.warning("Part not present: %r — %s", part.file, existence['reason'])
        else:
            log.debug("Part present: %r — %s (age=%sh)",
                      part.file, existence['reason'], existence['age_hours'])

    info['exists'] = all_parts_exist
    return info


############################################################
# QUARANTINE / DELETE
############################################################


def _quarantine_logical_path(src, title):
    """Return the logical relative path for a quarantined file.

    Strips everything up to (and including) the library-root prefix by
    locating the directory component that matches the Plex *title*.  Only the
    title-level folder and everything below it are kept, giving a short,
    human-readable sub-tree inside QUARANTINE_DIR.

    Examples
    --------
    src = /mnt/user/Media/TV/Breaking Bad/Season 01/ep.mkv , title = "Breaking Bad"
    →     Breaking Bad/Season 01/ep.mkv

    src = /mnt/user/Media/Movies/Dune (2021)/Dune.2021.mkv , title = "Dune"
    →     Dune (2021)/Dune.2021.mkv

    Falls back to (last-two-dirs + filename) when no title match is found.
    """
    # Normalise separators and split, discarding empty segments from leading /
    parts = [p for p in src.replace('\\', '/').split('/') if p]
    if not parts:
        return os.path.basename(src)

    filename = parts[-1]
    dirs = parts[:-1]
    if not dirs:
        return filename

    def _key(s):
        """Lowercase, strip non-alphanumerics — for fuzzy comparison."""
        return re.sub(r'[^a-z0-9]', '', s.lower())

    title_key = _key(title) if title else ''
    anchor = None

    if title_key:
        for i, d in enumerate(dirs):
            dk = _key(d)
            # Match when title is wholly contained in dir name or vice-versa.
            # This handles "Dune (2021)" matching title "Dune", and exact hits
            # like "Breaking Bad" → "Breaking Bad".
            if title_key == dk or title_key in dk or dk in title_key:
                anchor = i
                break

    if anchor is not None:
        logical = dirs[anchor:] + [filename]
    else:
        # Fallback: last two directory levels + filename.
        # Covers show/season/ep and movie-folder/file without needing the title.
        logical = dirs[-2:] + [filename]

    return os.path.join(*logical)


def _write_quarantine_sidecar(original_path, quarantine_path, part_info,
                               keeper_info, reason, original_size, original_mtime,
                               title=None, library_name=None, year=None):
    """Write a ``<quarantine_path>.dupefinder_meta.json`` sidecar file.

    The sidecar is completely self-contained: to restore the file, copy
    the ``restore_command`` field into a shell and run it — no script needed.

    Core fields (always present)
    ----------------------------
    original_path       — absolute path before quarantine
    quarantine_path     — absolute path after quarantine (current location)
    quarantine_timestamp — ISO-8601 UTC
    run_id              — identifies the script run that quarantined the file
    media_id            — Plex media-item ID
    reason              — why this file was selected for removal
    original_size       — bytes on disk at quarantine time (integrity check)
    original_mtime      — file mtime unix timestamp (integrity / restore check)
    keeper.files        — path(s) of the file that was kept
    keeper.score        — total numeric score of the kept file
    keeper.score_breakdown — per-component score breakdown dict
    restore_command     — ready-to-run shell command: mv quarantine → original

    Optional fields (omitted when not available)
    --------------------------------------------
    library             — Plex library name
    title               — media title as reported by Plex
    year                — release year (movies/shows)
    """
    sidecar_path = quarantine_path + '.dupefinder_meta.json'

    keeper_files = list(keeper_info.get('file', [])) if keeper_info else []
    keeper_score = keeper_info.get('score') if keeper_info else None
    keeper_score_breakdown = keeper_info.get('score_breakdown') if keeper_info else None

    meta = {
        'original_path': original_path,
        'quarantine_path': quarantine_path,
        'quarantine_timestamp': datetime.now(timezone.utc).isoformat(),
        'run_id': run_id,
        'media_id': part_info.get('id'),
        'reason': reason,
        'original_size': original_size,
        'original_mtime': original_mtime,
        'keeper': {
            'files': keeper_files,
            'score': keeper_score,
            'score_breakdown': keeper_score_breakdown,
        },
        'restore_command': 'mv %s %s' % (
            shlex.quote(quarantine_path), shlex.quote(original_path)),
    }

    # Optional Plex context — helps humans browsing the quarantine folder
    if library_name is not None:
        meta['library'] = library_name
    if title is not None:
        meta['title'] = title
    if year is not None:
        meta['year'] = year

    try:
        with open(sidecar_path, 'w', encoding='utf-8') as fp:
            json.dump(meta, fp, indent=2, default=str)
        log.info("QUARANTINE sidecar written: %r", sidecar_path)
    except OSError as e:
        # Sidecar failure is non-fatal — the file was already moved; warn only.
        log.warning("QUARANTINE sidecar write failed %r: %s", sidecar_path, e)


def quarantine_files(part_info, keeper_info=None, reason=None,
                     title=None, library_name=None, year=None):
    """Move files to QUARANTINE_DIR, preserving the logical media hierarchy.

    The quarantine path mirrors only the show/movie sub-tree — not the full
    absolute filesystem prefix.  This keeps the quarantine folder short and
    human-readable.

    Examples
    --------
    QUARANTINE_DIR  = /mnt/user/appdata/.../quarantine
    original file   = /mnt/user/Media/TV/Breaking Bad/Season 01/ep.mkv
    quarantined to  = QUARANTINE_DIR/Breaking Bad/Season 01/ep.mkv

    original file   = /mnt/user/Media/Movies/Dune (2021)/Dune.2021.mkv
    quarantined to  = QUARANTINE_DIR/Dune (2021)/Dune.2021.mkv

    Collision handling
    ------------------
    1. Try the bare logical path.
    2. If it already exists, suffix the top-level folder with the library
       name: ``Batman`` → ``Batman__MOVIES``.  This keeps cross-library
       same-title entries separate without touching unrelated files.
    3. If that also exists (re-run into the same quarantine), append a
       unix timestamp to the *filename* as a last resort.

    A ``.dupefinder_meta.json`` sidecar is written alongside every moved file
    with full provenance so the restore command is a single ``mv`` call.
    """
    quarantine_root = cfg['QUARANTINE_DIR']
    if not quarantine_root:
        raise RuntimeError("QUARANTINE_DIR is not configured")

    moved = []
    errors = []
    for src in part_info['file']:
        try:
            if not os.path.exists(src):
                errors.append({'file': src, 'error': 'source missing'})
                continue

            # Capture file stats BEFORE the move (they are unavailable after)
            try:
                original_size = os.path.getsize(src)
                original_mtime = os.path.getmtime(src)
            except OSError:
                original_size = None
                original_mtime = None

            # --- Build logical destination path ---
            logical_rel = _quarantine_logical_path(src, title or '')
            # Split so we can target the top-level folder for collision suffix
            logical_parts = logical_rel.replace('\\', '/').split('/')
            dest = os.path.join(quarantine_root, logical_rel)

            # Collision pass 1: suffix top-level folder with library name
            if os.path.exists(dest) and len(logical_parts) >= 1:
                safe_lib = re.sub(r'[^a-zA-Z0-9]', '_',
                                  (library_name or 'UNKNOWN')).strip('_').upper()
                suffixed_top = logical_parts[0] + '__' + safe_lib
                dest = os.path.join(quarantine_root,
                                    os.path.join(suffixed_top, *logical_parts[1:]))

            # Collision pass 2: append timestamp to filename
            if os.path.exists(dest):
                base, ext = os.path.splitext(dest)
                dest = '%s__%d%s' % (base, int(time.time()), ext)

            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.move(src, dest)
            moved.append(dest)
            log.info("QUARANTINE moved src=%r dest=%r media_id=%r",
                     src, dest, part_info.get('id'))

            _write_quarantine_sidecar(
                src, dest, part_info, keeper_info, reason,
                original_size, original_mtime,
                title=title, library_name=library_name, year=year,
            )

        except (OSError, shutil.Error) as e:
            log.exception("Failed to quarantine %r", src)
            errors.append({'file': src, 'error': str(e)})

    return {'moved': moved, 'errors': errors}


def remove_plex_metadata(show_key, media_id):
    """Call Plex DELETE on the media item. Returns (success, detail)."""
    delete_url = urljoin(cfg['PLEX_SERVER'], '%s/media/%d' % (show_key, media_id))
    try:
        response = requests.delete(
            delete_url,
            headers={'X-Plex-Token': cfg['PLEX_TOKEN']},
            timeout=REQUESTS_TIMEOUT,
        )
    except requests.RequestException as e:
        log.exception("DELETE request error for media %r", media_id)
        return False, "request_error: %s" % e

    if response.status_code in (200, 204):
        log.info("Plex metadata deleted for media %r", media_id)
        return True, "http_%d" % response.status_code

    log.error("Plex DELETE failed media=%r status=%s body=%s",
              media_id, response.status_code, response.text[:200])
    return False, "http_%d: %s" % (response.status_code, response.text[:200])


def remove_item(part_info, reason, keeper_info=None,
                title=None, library_name=None, year=None):
    """Apply DRY_RUN / QUARANTINE_MODE / direct semantics.

    Parameters
    ----------
    part_info    : dict  — the candidate to remove (from _build_parts_for_item)
    reason       : str   — human-readable removal reason (logged + sidecar)
    keeper_info  : dict  — the kept duplicate's part_info (score, files,
                           score_breakdown); written into the quarantine sidecar
                           so manual restoration is self-contained
    title        : str   — Plex media title; used to build the quarantine path
                           and stored in the sidecar
    library_name : str   — Plex library name; used as collision-suffix and
                           stored in the sidecar
    year         : int   — release year (optional, stored in sidecar)
    """
    media_id = part_info.get('id')
    show_key = part_info.get('show_key')
    files = list(part_info.get('file', []))
    result = {
        'media_id': media_id, 'files': files,
        'file_size': part_info.get('file_size', 0), 'reason': reason,
        'mode': None, 'success': False,
        'quarantine': None, 'plex_delete': None, 'error': None,
    }

    if cfg.get('DRY_RUN', True):
        result['mode'] = 'dry_run'
        result['success'] = True
        print("\t\t[DRY-RUN] Would remove media %r" % media_id)
        log.info("DRY-RUN remove media=%r files=%r reason=%s", media_id, files, reason)
        return result

    # In paths-only mode the duplicates share the same file — moving it would
    # break the keeper too, so only ever clear the redundant metadata.
    paths_only_mode = bool(cfg.get('FIND_DUPLICATE_FILEPATHS_ONLY'))

    if cfg.get('QUARANTINE_MODE', True) and not paths_only_mode:
        result['mode'] = 'quarantine'
        try:
            qresult = quarantine_files(
                part_info,
                keeper_info=keeper_info,
                reason=reason,
                title=title,
                library_name=library_name,
                year=year,
            )
        except RuntimeError as e:
            log.exception("Quarantine config error")
            result['error'] = str(e)
            return result
        result['quarantine'] = qresult
        # Only remove the Plex entry if EVERY backing file was quarantined. On a
        # partial failure (e.g. a multi-part item where one part moved and another
        # did not), deleting the Plex entry would orphan the unmoved part on disk
        # (no Plex reference, no sidecar mapping). Preserve the Plex entry so the
        # library stays consistent; the already-moved parts remain restorable via
        # their sidecars. No data is lost — only manual review is needed.
        if qresult['errors']:
            result['error'] = ('quarantine incomplete: %d moved, %d failed — '
                               'Plex entry preserved, manual review needed'
                               % (len(qresult['moved']), len(qresult['errors'])))
            log.error("QUARANTINE INCOMPLETE media_id=%r moved=%d errors=%d — "
                      "Plex entry preserved", media_id,
                      len(qresult['moved']), len(qresult['errors']))
            return result
        ok, detail = remove_plex_metadata(show_key, media_id)
        result['plex_delete'] = {'success': ok, 'detail': detail}
        result['success'] = ok and bool(qresult['moved'])
        if not ok:
            result['error'] = 'plex_metadata_removal_failed: %s' % detail
        return result

    result['mode'] = 'metadata_only' if paths_only_mode else 'direct'
    ok, detail = remove_plex_metadata(show_key, media_id)
    result['plex_delete'] = {'success': ok, 'detail': detail}
    result['success'] = ok
    if not ok:
        result['error'] = 'plex_delete_failed: %s' % detail
    return result


############################################################
# INTEGRATIONS
############################################################


def refresh_plex_libraries(libraries):
    result = {'attempted': False, 'libraries': [], 'errors': []}
    if not cfg.get('PLEX_REFRESH_AFTER') or not libraries:
        return result
    result['attempted'] = True
    for name in libraries:
        try:
            section = plex.library.section(name)
            section.update()
            log.info("Plex refresh triggered for library %r", name)
            print("Triggered Plex refresh for %r" % name)
            result['libraries'].append(name)
        except Exception as e:
            log.exception("Failed to refresh Plex library %r", name)
            print("Failed to refresh Plex library %r: %s" % (name, e))
            result['errors'].append({'library': name, 'error': str(e)})
    return result


def _arr_post_command(tool, url, api_key, command_name):
    result = {'attempted': True, 'success': False, 'detail': None, 'command': command_name}
    try:
        response = requests.post(
            "%s/api/v3/command" % url.rstrip('/'),
            headers={'X-Api-Key': api_key, 'Content-Type': 'application/json'},
            json={'name': command_name},
            timeout=REQUESTS_TIMEOUT,
        )
        if response.status_code in (200, 201):
            result['success'] = True
            try:
                result['detail'] = response.json()
            except ValueError:
                result['detail'] = response.text[:200]
            log.info("%s %s triggered", tool, command_name)
            print("Triggered %s %s" % (tool, command_name))
        else:
            result['detail'] = "HTTP %d: %s" % (response.status_code, response.text[:200])
            log.error("%s %s failed: %s", tool, command_name, result['detail'])
            print("%s %s failed: HTTP %d" % (tool, command_name, response.status_code))
    except requests.RequestException as e:
        result['detail'] = str(e)
        log.exception("%s command failed", tool)
        print("%s command failed: %s" % (tool, e))
    return result


def trigger_radarr_rescan():
    if not cfg.get('RADARR_RESCAN_AFTER'):
        return {'attempted': False}
    return _arr_post_command('Radarr', cfg['RADARR_URL'], cfg['RADARR_API_KEY'], 'RescanMovie')


def trigger_sonarr_rescan():
    if not cfg.get('SONARR_RESCAN_AFTER'):
        return {'attempted': False}
    return _arr_post_command('Sonarr', cfg['SONARR_URL'], cfg['SONARR_API_KEY'], 'RescanSeries')


############################################################
# DECISION
############################################################


def has_sane_metadata(part_info):
    """
    Reject parts where Plex metadata is obviously incomplete or invalid.

    Returns (ok: bool, reason: str). A failure means Plex has not finished
    analysing the file (or it is broken); decisions made on this data
    would be unreliable.
    """
    files = part_info.get('file') or []
    if not files:
        return False, "empty file list"
    for f in files:
        if not f or not str(f).strip():
            return False, "empty file path"
    if int(part_info.get('video_duration') or 0) <= 0:
        return False, "video_duration <= 0"
    if int(part_info.get('video_bitrate') or 0) <= 0:
        return False, "video_bitrate <= 0"
    codec = (part_info.get('video_codec') or '').strip().lower()
    if not codec or codec == 'unknown':
        return False, "video_codec missing or Unknown"
    return True, ""


def select_keeper(parts):
    """
    Apply all safety checks and return a decision dict.

    Fields: keeper_id, reason, skip, skip_reason, candidates_existing,
    top_score, second_score, score_delta, youngest_age_hours, max_size_ratio.
    """
    decision = {
        'keeper_id': None,
        'reason': None,
        'skip': False,
        'skip_reason': None,
        'candidates_existing': [],
        'top_score': None,
        'second_score': None,
        'score_delta': None,
        'youngest_age_hours': None,
        'max_size_ratio': None,
    }

    existing_ids = [mid for mid, pi in parts.items() if pi.get('exists', True)]
    decision['candidates_existing'] = existing_ids

    if not existing_ids:
        decision['skip'] = True
        decision['skip_reason'] = 'no candidate exists on disk; Plex metadata may be stale'
        decision['reason'] = 'all candidates missing'
        return decision

    if cfg.get('REQUIRE_LOCAL_FS_ACCESS'):
        any_local = any(
            any(ex.get('local_check') is not None for ex in pi.get('parts_existence', []))
            for pi in parts.values()
        )
        if not any_local:
            decision['skip'] = True
            decision['skip_reason'] = ('REQUIRE_LOCAL_FS_ACCESS=True but filesystem is not '
                                       'reachable from this host')
            decision['reason'] = 'fs not reachable'
            return decision

    # Metadata sanity: any existing candidate with obviously broken Plex
    # metadata means analysis is incomplete — too risky to decide.
    for mid in existing_ids:
        sane, why = has_sane_metadata(parts[mid])
        if not sane:
            decision['skip'] = True
            decision['skip_reason'] = ('candidate %r has invalid metadata: %s '
                                       '(Plex analysis may be incomplete)' % (mid, why))
            decision['reason'] = 'metadata sanity check failed'
            return decision

    # Cooldown
    min_age_hours = float(cfg.get('MIN_FILE_AGE_HOURS', 0))
    if min_age_hours > 0:
        youngest = None
        offender = None
        for mid, pi in parts.items():
            for ex in pi.get('parts_existence', []):
                age = ex.get('age_hours')
                if age is None:
                    continue
                if youngest is None or age < youngest:
                    youngest = age
                    offender = (mid, ex.get('file'))
        decision['youngest_age_hours'] = youngest
        if youngest is not None and youngest < min_age_hours:
            decision['skip'] = True
            decision['skip_reason'] = ("cooldown: %r is %.2fh old, below threshold %.2fh"
                                       % (offender[1], youngest, min_age_hours))
            decision['reason'] = 'cooldown protection'
            return decision

    if cfg['FIND_DUPLICATE_FILEPATHS_ONLY']:
        scored = sorted(((int(parts[mid]['id']), mid) for mid in existing_ids), key=lambda x: x[0])
        decision['keeper_id'] = scored[0][1]
        decision['reason'] = 'lowest media id among existing entries (paths-only mode)'
        return decision

    scored = sorted(((int(parts[mid]['score']), mid) for mid in existing_ids),
                    key=lambda x: x[0], reverse=True)
    best_score, keeper_id = scored[0]
    decision['top_score'] = best_score

    if len(scored) > 1:
        second_score = scored[1][0]
        delta = best_score - second_score
        decision['second_score'] = second_score
        decision['score_delta'] = delta
        threshold = int(cfg.get('MIN_SCORE_DIFFERENCE', 0))
        if threshold > 0 and delta < threshold:
            decision['skip'] = True
            decision['skip_reason'] = 'score delta %d below threshold %d' % (delta, threshold)
            decision['reason'] = 'score delta too small'
            return decision

    # Size-ratio sanity check
    max_ratio = float(cfg.get('MAX_SIZE_RATIO', 0))
    if max_ratio > 0:
        keeper_size = parts[keeper_id].get('file_size', 0) or 0
        if keeper_size > 0:
            worst_ratio = 0.0
            offender = None
            for mid in existing_ids:
                if mid == keeper_id:
                    continue
                other_size = parts[mid].get('file_size', 0) or 0
                if other_size <= 0:
                    continue
                ratio = other_size / keeper_size
                if ratio > worst_ratio:
                    worst_ratio = ratio
                    offender = (mid, other_size)
            decision['max_size_ratio'] = worst_ratio or None
            if worst_ratio > max_ratio:
                decision['skip'] = True
                decision['skip_reason'] = (
                    "size ratio %.1fx exceeds threshold %.1fx "
                    "(keeper=%s, sibling id=%r=%s)"
                    % (worst_ratio, max_ratio,
                       bytes_to_string(keeper_size),
                       offender[0], bytes_to_string(offender[1]))
                )
                decision['reason'] = 'size ratio protection'
                return decision

    decision['keeper_id'] = keeper_id
    decision['reason'] = 'highest score (%d) among existing files' % best_score
    return decision


def detect_inconsistencies(snapshot_parts, fresh_parts, snapshot_decision, fresh_decision):
    """
    Compare a Pass 1 snapshot to a Pass 2 fresh read.

    Returns a list of human-readable diffs. Empty list = consistent.
    """
    diffs = []
    snap_ids = set(snapshot_parts.keys())
    fresh_ids = set(fresh_parts.keys())

    only_in_snap = snap_ids - fresh_ids
    only_in_fresh = fresh_ids - snap_ids
    if only_in_snap:
        diffs.append("media missing now: %s" % sorted(only_in_snap))
    if only_in_fresh:
        diffs.append("new media appeared: %s" % sorted(only_in_fresh))

    for mid in snap_ids & fresh_ids:
        s = snapshot_parts[mid]
        f = fresh_parts[mid]
        if s.get('file') != f.get('file'):
            diffs.append("media %r files changed: %r → %r" % (mid, s.get('file'), f.get('file')))
        if s.get('file_size') != f.get('file_size'):
            diffs.append("media %r size changed: %s → %s"
                         % (mid, s.get('file_size'), f.get('file_size')))
        if s.get('exists') != f.get('exists'):
            diffs.append("media %r existence changed: %s → %s"
                         % (mid, s.get('exists'), f.get('exists')))
        # Duration / bitrate / codec catch Tdarr transcodes, partial writes,
        # and Plex re-analyses that happen between passes.
        if s.get('video_duration') != f.get('video_duration'):
            diffs.append("media %r duration changed: %s → %s ms"
                         % (mid, s.get('video_duration'), f.get('video_duration')))
        if s.get('video_bitrate') != f.get('video_bitrate'):
            diffs.append("media %r bitrate changed: %s → %s kbps"
                         % (mid, s.get('video_bitrate'), f.get('video_bitrate')))
        if (s.get('video_codec') or '').lower() != (f.get('video_codec') or '').lower():
            diffs.append("media %r video codec changed: %r → %r"
                         % (mid, s.get('video_codec'), f.get('video_codec')))

        if cfg.get('PARTIAL_HASH_ENABLED'):
            snap_hashes = [ex.get('partial_hash') for ex in s.get('parts_existence', [])]
            fresh_hashes = [ex.get('partial_hash') for ex in f.get('parts_existence', [])]
            if any(sh and fh and sh != fh for sh, fh in zip(snap_hashes, fresh_hashes)):
                diffs.append("media %r partial-hash changed (file modified between passes)" % mid)

    if fresh_decision['skip']:
        diffs.append("revalidation now wants to skip: %s" % fresh_decision['skip_reason'])
    elif fresh_decision['keeper_id'] != snapshot_decision['keeper_id']:
        diffs.append("keeper changed: %s → %s"
                     % (snapshot_decision['keeper_id'], fresh_decision['keeper_id']))

    return diffs


############################################################
# REPORTING
############################################################


def summarize_quarantine():
    """Read-only summary of the current QUARANTINE_DIR contents.

    Walks the quarantine tree for ``.dupefinder_meta.json`` sidecars and
    aggregates count, total size, and age. This is the standing quarantine
    (everything moved by prior runs too), giving operators visibility into
    growth so they can decide when to purge manually. It NEVER deletes or
    modifies anything.

    Age uses the sidecar's ``quarantine_timestamp`` — not file mtime, which
    shutil.move preserves from the original and would misreport time-in-quarantine.
    """
    qdir = (cfg.get('QUARANTINE_DIR') or '').strip()
    summary = {
        'enabled': bool(qdir),
        'dir': qdir or None,
        'file_count': 0,
        'total_bytes': 0,
        'total_human': bytes_to_string(0),
        'oldest_age_days': None,
        'retention_days': cfg.get('QUARANTINE_RETENTION_DAYS'),
        'files_over_retention': 0,
        'sidecars_unreadable': 0,
    }
    if not qdir or not os.path.isdir(qdir):
        return summary

    retention_days = float(cfg.get('QUARANTINE_RETENTION_DAYS') or 0)
    now = datetime.now(timezone.utc)
    oldest_age = None

    for root, _dirs, files in os.walk(qdir):
        for name in files:
            if not name.endswith('.dupefinder_meta.json'):
                continue
            sidecar = os.path.join(root, name)
            try:
                with open(sidecar, 'r', encoding='utf-8') as fp:
                    meta = json.load(fp)
            except (OSError, ValueError):
                summary['sidecars_unreadable'] += 1
                continue

            summary['file_count'] += 1

            size = meta.get('original_size')
            if isinstance(size, (int, float)) and size > 0:
                summary['total_bytes'] += int(size)

            ts = meta.get('quarantine_timestamp')
            if ts:
                try:
                    moved_at = datetime.fromisoformat(ts)
                    if moved_at.tzinfo is None:
                        moved_at = moved_at.replace(tzinfo=timezone.utc)
                    age_days = (now - moved_at).total_seconds() / 86400.0
                except ValueError:
                    age_days = None
                if age_days is not None:
                    if oldest_age is None or age_days > oldest_age:
                        oldest_age = age_days
                    if retention_days > 0 and age_days > retention_days:
                        summary['files_over_retention'] += 1

    summary['total_human'] = bytes_to_string(summary['total_bytes'])
    summary['oldest_age_days'] = round(oldest_age, 1) if oldest_age is not None else None
    return summary


def write_decision(title=None, keeping=None, removed=None, note=None):
    lines = []
    if title:
        lines.append('\nTitle    : %s\n' % title)
    if keeping:
        lines.append('\tKeeping  : %r\n' % keeping)
    if removed:
        lines.append('\tRemoving : %r\n' % removed)
    if note:
        lines.append('\tNote     : %s\n' % note)
    try:
        with open(decision_filename, 'a', encoding='utf-8') as fp:
            fp.writelines(lines)
    except OSError:
        log.exception("Failed to write decisions.log")


def should_skip(files):
    return any(
        skip_item in str(f)
        for f in files
        for skip_item in cfg['SKIP_LIST']
    )


def build_tabulated(parts, items):
    headers = ['choice', 'score', 'exists', 'id', 'file', 'size', 'duration', 'bitrate', 'resolution', 'codecs']
    if cfg['FIND_DUPLICATE_FILEPATHS_ONLY']:
        headers.remove('score')

    data = []
    for choice, item_id in items.items():
        row = []
        for k in headers:
            if k == 'choice':
                row.append(choice)
            elif k == 'score':
                row.append(str(format(parts[item_id][k], ',d')))
            elif k == 'exists':
                row.append('Yes' if parts[item_id].get('exists', True) else 'MISSING')
            elif k == 'id':
                row.append(parts[item_id][k])
            elif k == 'file':
                row.append(parts[item_id][k])
            elif k == 'size':
                row.append(bytes_to_string(parts[item_id]['file_size']))
            elif k == 'duration':
                row.append(millis_to_string(parts[item_id]['video_duration']))
            elif k == 'bitrate':
                row.append(kbps_to_string(parts[item_id]['video_bitrate']))
            elif k == 'resolution':
                row.append("%s (%d x %d)" % (parts[item_id]['video_resolution'],
                                             parts[item_id]['video_width'],
                                             parts[item_id]['video_height']))
            elif k == 'codecs':
                tags = []
                if parts[item_id].get('has_dv'):
                    tags.append('DV')
                if parts[item_id].get('has_hdr'):
                    tags.append('HDR')
                tag_str = (' [' + '/'.join(tags) + ']') if tags else ''
                row.append("%s, %s x %d%s" % (parts[item_id]['video_codec'],
                                              parts[item_id]['audio_codec'],
                                              parts[item_id]['audio_channels'],
                                              tag_str))
        data.append(row)
    return headers, data


def write_json_report():
    out_dir = (cfg.get('JSON_REPORT_DIR') or '').strip()
    if not out_dir or not os.path.isdir(out_dir):
        return None
    timestamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    path = os.path.join(out_dir, "dupefinder_report_%s_%s.json" % (run_id, timestamp))
    run_report['finished_at'] = datetime.now(timezone.utc).isoformat()
    try:
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(run_report, f, indent=2, default=str)
        log.info("JSON report written to %s", path)
        print("JSON report: %s" % path)
        return path
    except OSError:
        log.exception("Failed to write JSON report")
        print("Failed to write JSON report — see activity.log")
        return None


def write_plan_file(groups):
    """Persist Pass 1 discovery snapshot. Always written for audit."""
    if not os.path.isdir(default_plans_dir):
        return None
    timestamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    path = os.path.join(default_plans_dir, "dupefinder_plan_%s_%s.json" % (run_id, timestamp))
    plan = {
        'run_id': run_id,
        'discovered_at': datetime.now(timezone.utc).isoformat(),
        'config': {k: ('<redacted>' if k in REDACTED_KEYS else v) for k, v in cfg.items()},
        'groups': [
            {
                'title': g['title'],
                'library': g['library'],
                'item_key': g['item_key'],
                'pass0_status': g.get('pass0_status'),
                'decision': g['decision'],
                'parts': [
                    {
                        'media_id': mid,
                        'score': pi.get('score'),
                        'score_breakdown': pi.get('score_breakdown'),
                        'exists': pi.get('exists'),
                        'file_size': pi.get('file_size'),
                        'files': pi.get('file'),
                        'video_codec': pi.get('video_codec'),
                        'audio_codec': pi.get('audio_codec'),
                        'video_resolution': pi.get('video_resolution'),
                        'video_bitrate': pi.get('video_bitrate'),
                        'video_duration': pi.get('video_duration'),
                        'has_hdr': pi.get('has_hdr'),
                        'has_dv': pi.get('has_dv'),
                        'parts_existence': pi.get('parts_existence'),
                    }
                    for mid, pi in g['parts'].items()
                ],
            }
            for g in groups
        ],
    }
    try:
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(plan, f, indent=2, default=str)
        log.info("Discovery plan written to %s", path)
        print("Discovery plan: %s" % path)
        return path
    except OSError:
        log.exception("Failed to write discovery plan")
        return None


############################################################
# MANUAL CONSOLE
############################################################


def _manual_choose_keeper(title, parts, recommended_keeper_id, decision_reason):
    """Show the table and prompt user. Returns (keeper_id, skipped_by_user)."""
    print("\nWhich media item do you wish to keep for %r ?" % title)
    print("(Recommended keeper id=%r — %s)\n" % (recommended_keeper_id, decision_reason))

    sort_key = 'id' if cfg['FIND_DUPLICATE_FILEPATHS_ONLY'] else 'score'
    reverse = not cfg['FIND_DUPLICATE_FILEPATHS_ONLY']

    # dict maintains insertion order in Python 3.7+; OrderedDict not needed.
    media_items = {
        pos: mid
        for pos, (mid, _) in enumerate(
            sorted(parts.items(), key=lambda x: x[1][sort_key], reverse=reverse),
            start=1,
        )
    }

    headers, data = build_tabulated(parts, media_items)
    print(tabulate(data, headers=headers))

    raw = input("\nChoose item to keep (0 or s=skip, 1..N=row, b=recommended best): ").strip().lower()
    if raw in ('s', '0', ''):
        print("Skipping deletion(s) for %r" % title)
        return None, True
    if raw == 'b':
        return recommended_keeper_id, False
    if raw.isdigit() and 0 < int(raw) <= len(media_items):
        chosen = media_items[int(raw)]
        if not parts[chosen].get('exists', True):
            confirm = input(
                "[!] The chosen item (id=%r) is NOT present on disk.\n"
                "    Keeping it could delete your only good copy.\n"
                "    Type 'YES' (uppercase) to confirm: " % chosen
            ).strip()
            if confirm != 'YES':
                print("Skipping by safety.")
                return None, True
        return chosen, False
    print("Invalid input %r, skipping." % raw)
    return None, True


############################################################
# TWO-PASS WORKFLOW
############################################################


def _item_title(item):
    if item.type == 'episode':
        return "%s - %02dx%02d - %s" % (
            item.grandparentTitle, int(item.parentIndex), int(item.index), item.title)
    if item.type == 'movie':
        return item.title
    return 'Unknown'


def _build_parts_for_item(item, compute_hashes):
    parts = {}
    for part in item.media:
        pi = get_media_info(part, compute_hashes=compute_hashes)
        if not cfg['FIND_DUPLICATE_FILEPATHS_ONLY']:
            pi['score'], pi['score_breakdown'] = get_score(pi)
        pi['show_key'] = item.key
        parts[part.id] = pi
    return parts


def _skip_group_for_pass0_failure(title, library, item, pass0_status, reason):
    """Build a stub group entry that the rest of the pipeline treats as skipped."""
    return {
        'title': title,
        'library': library,
        'item_key': getattr(item, 'key', None),
        'parts': {},
        'decision': {
            'keeper_id': None,
            'reason': 'metadata refresh failed',
            'skip': True,
            'skip_reason': "PASS0 refresh failed: " + reason,
            'candidates_existing': [],
            'top_score': None,
            'second_score': None,
            'score_delta': None,
            'youngest_age_hours': None,
            'max_size_ratio': None,
        },
        'discovered_at': datetime.now(timezone.utc).isoformat(),
        'pass0_status': pass0_status,
    }


def discovery_pass(sections):
    """
    Pass 1: gather every duplicate group, score, and tentatively decide.
    Pure observation — no Plex writes, no file moves.

    If PRE_ANALYZE_DUPLICATES is enabled, each item gets a PASS 0 refresh
    (analyze + poll-reload) before its metadata is read.
    """
    pre_analyze = bool(cfg.get('PRE_ANALYZE_DUPLICATES', False))
    analyze_timeout = float(cfg.get('ANALYZE_TIMEOUT_SECONDS', 60))
    compute_hashes = bool(cfg.get('PARTIAL_HASH_ENABLED'))

    suffix = " (with PASS 0 metadata refresh)" if pre_analyze else ""
    print("\n[PASS 1/2] Discovery — gathering duplicates and scoring%s..." % suffix)
    log.info("PASS1 START pre_analyze=%s analyze_timeout=%s", pre_analyze, analyze_timeout)
    groups = []

    for section in sections:
        try:
            dupes = get_dupes(section)
        except Exception:
            log.exception("Failed to fetch dupes for section %r", section)
            print("Failed to fetch dupes for section %r — see activity.log" % section)
            run_report['errors'].append({'stage': 'get_dupes', 'section': section})
            continue
        print("  found %d dupe groups in %r" % (len(dupes), section))

        for item in dupes:
            try:
                title = _item_title(item)

                pass0_status = None
                if pre_analyze:
                    item, pass0_status = refresh_plex_item(item, analyze_timeout)
                    log.info("PASS0 group=%r status=%s changed_fields=%d",
                             title, pass0_status.get('status'),
                             len(pass0_status.get('changed_fields') or []))
                    if not pass0_status.get('success'):
                        reason = pass0_status.get('reason') or pass0_status.get('status')
                        log.warning("PASS0 SKIP group=%r status=%s reason=%r",
                                    title, pass0_status.get('status'), reason)
                        print("  [PASS0] skip %r — %s" % (title, reason))
                        groups.append(_skip_group_for_pass0_failure(
                            title, section, item, pass0_status, reason))
                        continue
                    if pass0_status.get('status') == 'sane_unchanged':
                        # Visible note: scoring will run on data we could not
                        # prove is fresh. See refresh_plex_item docstring.
                        print("  [PASS0] %r metadata sane but unchanged (potentially stale)" % title)

                log.info("PASS1 gather title=%r library=%r key=%r", title, section, item.key)
                parts = _build_parts_for_item(item, compute_hashes)
                decision = select_keeper(parts)
                group = {
                    'title': title,
                    'library': section,
                    'item_key': item.key,
                    'parts': parts,
                    'decision': decision,
                    'discovered_at': datetime.now(timezone.utc).isoformat(),
                    'pass0_status': pass0_status,
                }
                groups.append(group)
                log.info(
                    "PASS1 DECISION group=%r keeper=%s skip=%s reason=%r "
                    "top_score=%s second_score=%s delta=%s ratio=%s youngest_h=%s",
                    title, decision['keeper_id'], decision['skip'], decision['reason'],
                    decision['top_score'], decision['second_score'], decision['score_delta'],
                    decision['max_size_ratio'], decision['youngest_age_hours'],
                )
            except Exception:
                log.exception("Failed to gather item in section %r", section)
                run_report['errors'].append({
                    'stage': 'gather_item', 'section': section,
                    'title': getattr(item, 'title', None),
                })
    return groups


def _revalidate_and_act_group(group):
    """
    Pass 2 for a single group: re-fetch from Plex, recompute, compare to
    snapshot, then act only if everything is still consistent.
    """
    counters = dict.fromkeys((
        'groups_processed', 'groups_skipped_user', 'groups_skipped_safety',
        'groups_skipped_inconsistent', 'groups_failed_refetch',
        'items_removed', 'items_failed', 'freed_bytes',
    ), 0)

    record = {
        'title': group['title'],
        'library': group['library'],
        'item_key': group['item_key'],
        'pass0_status': group.get('pass0_status'),
        'discovery_decision': group['decision'],
        'discovery_candidates': [
            {
                'media_id': mid,
                'score': pi.get('score'),
                'score_breakdown': pi.get('score_breakdown'),
                'file_size': pi.get('file_size'),
                'files': pi.get('file'),
                'exists': pi.get('exists'),
                'video_codec': pi.get('video_codec'),
                'audio_codec': pi.get('audio_codec'),
                'video_resolution': pi.get('video_resolution'),
                'video_bitrate': pi.get('video_bitrate'),
                'video_duration': pi.get('video_duration'),
                'has_hdr': pi.get('has_hdr'),
                'has_dv': pi.get('has_dv'),
            }
            for mid, pi in group['parts'].items()
        ],
        'revalidation': None,
        'items_removed': [],
        'errors': [],
    }

    title = group['title']
    library_name = group['library']
    snap_decision = group['decision']

    try:
        if snap_decision['skip']:
            record['revalidation'] = {'status': 'skipped_in_discovery',
                                      'reason': snap_decision['skip_reason']}
            log.info("PASS2 SKIP_FROM_PASS1 group=%r reason=%r",
                     title, snap_decision['skip_reason'])
            counters['groups_skipped_safety'] = 1
            return counters

        # Re-fetch the item from Plex
        try:
            fresh_item = plex.fetchItem(group['item_key'])
        except Exception as e:
            log.warning("PASS2 refetch failed group=%r error=%s", title, e)
            record['revalidation'] = {'status': 'refetch_failed', 'error': str(e)}
            record['errors'].append({'stage': 'refetch', 'detail': str(e)})
            counters['groups_failed_refetch'] = 1
            return counters

        compute_hashes = bool(cfg.get('PARTIAL_HASH_ENABLED'))
        fresh_parts = _build_parts_for_item(fresh_item, compute_hashes)
        fresh_decision = select_keeper(fresh_parts)

        diffs = detect_inconsistencies(group['parts'], fresh_parts, snap_decision, fresh_decision)
        record['revalidation'] = {
            'status': 'consistent' if not diffs else 'inconsistent',
            'diffs': diffs,
            'fresh_decision': fresh_decision,
        }

        log.info(
            "PASS2 REVALIDATE group=%r consistent=%s diff_count=%d fresh_keeper=%s fresh_skip=%s",
            title, not diffs, len(diffs), fresh_decision['keeper_id'], fresh_decision['skip'],
        )

        if diffs:
            print("\n[!] Skipping %r — revalidation found %d inconsistencies:" % (title, len(diffs)))
            for d in diffs[:6]:
                print("    - %s" % d)
            counters['groups_skipped_inconsistent'] = 1
            return counters

        keeper_id = fresh_decision['keeper_id']

        # Keeper-selection interactivity model:
        #   * "acting" run (real moves/deletes) = not DRY_RUN and not AUDIT_MODE.
        #   * When ACTING: prompt per group unless AUTO_DELETE is on.
        #   * When NOT acting (DRY_RUN or AUDIT_MODE) nothing is ever mutated, so
        #     the run is unattended UNLESS CONFIRM_BEFORE_ACTION=true, which opts
        #     into assisted manual selection. This gives the two audit sub-modes:
        #       AUDIT_MODE + CONFIRM_BEFORE_ACTION=false -> fully unattended (cron)
        #       AUDIT_MODE + CONFIRM_BEFORE_ACTION=true  -> assisted manual review
        #     No destructive action occurs in either case while not acting.
        acting = (not cfg.get('DRY_RUN', True)) and not bool(cfg.get('AUDIT_MODE', False))
        if acting:
            interactive = not cfg.get('AUTO_DELETE', False)
        else:
            interactive = bool(cfg.get('CONFIRM_BEFORE_ACTION', True))
        if interactive:
            chosen, skipped = _manual_choose_keeper(title, fresh_parts, keeper_id, fresh_decision['reason'])
            if skipped:
                counters['groups_skipped_user'] = 1
                return counters
            if chosen != keeper_id:
                record['revalidation']['keeper_id_manual_override'] = chosen
                log.info("PASS2 MANUAL_OVERRIDE group=%r recommended=%r chosen=%r",
                         title, keeper_id, chosen)
            keeper_id = chosen
        else:
            log.info("PASS2 AUTO_KEEPER group=%r keeper=%r (no prompt: acting=%s)",
                     title, keeper_id, acting)

        keeper_info = fresh_parts[keeper_id]

        # Stability check: re-read every candidate's size after a brief wait.
        # Any size change → a file is actively being written (Tdarr transcode,
        # mid-copy, mid-import). Skip the whole group. Cheap last-mile safety.
        # Only meaningful on an acting run (DRY_RUN/AUDIT never move files).
        stability_wait = float(cfg.get('STABILITY_CHECK_SECONDS', 0))
        if acting and stability_wait > 0:
            all_paths = []
            for pi in fresh_parts.values():
                all_paths.extend(pi.get('file') or [])
            stable, changes = is_files_stable(all_paths, stability_wait)
            record['revalidation']['stability_check'] = {
                'status': 'stable' if stable else 'unstable',
                'wait_seconds': stability_wait,
                'changes': changes,
            }
            if not stable:
                print("\n[!] Skipping %r — files still being modified during stability check:" % title)
                for c in changes[:6]:
                    print("    - %s: %s → %s bytes"
                          % (c['file'], c['size_before'], c['size_after']))
                log.warning("PASS2 STABILITY_FAIL group=%r changes=%r", title, changes)
                counters['groups_skipped_inconsistent'] = 1
                return counters

        write_decision(title=title, note=fresh_decision.get('reason'))
        write_decision(keeping=keeper_info)
        print("\tKeeping  : %r - %r" % (keeper_id, keeper_info.get('file')))

        delete_delay = float(cfg.get('PLEX_DELETE_DELAY_SECONDS', 2.0))
        for mid, pi in fresh_parts.items():
            if mid == keeper_id:
                continue
            if should_skip(pi['file']):
                print("\tSkipping removal (SKIP_LIST match): %r" % mid)
                log.info("PASS2 SKIP_LIST group=%r media_id=%r", title, mid)
                record['items_removed'].append({
                    'media_id': mid, 'mode': 'skipped_skip_list',
                    'success': False, 'files': pi['file'],
                })
                continue

            missing_note = "" if pi.get('exists', True) else " [missing on disk]"
            print("\tRemoving%s : %r - %r" % (missing_note, mid, pi['file']))

            reason = "duplicate (keeper id=%r, %s)" % (keeper_id, fresh_decision.get('reason'))
            result = remove_item(
                pi, reason,
                keeper_info=keeper_info,
                title=title,
                library_name=library_name,
                year=getattr(fresh_item, 'year', None),
            )
            record['items_removed'].append(result)

            if result['success']:
                counters['items_removed'] += 1
                if pi.get('exists', True) and result['mode'] != 'dry_run':
                    counters['freed_bytes'] += pi.get('file_size', 0) or 0
                write_decision(removed=pi, note="mode=%s" % result['mode'])
            else:
                counters['items_failed'] += 1
                record['errors'].append({
                    'stage': 'remove_item',
                    'media_id': mid,
                    'detail': result.get('error'),
                })

            if delete_delay > 0:
                time.sleep(delete_delay)

        counters['groups_processed'] = 1
        return counters

    except Exception:
        log.exception("PASS2 unexpected error group=%r", title)
        record['errors'].append({'stage': 'revalidation_or_action', 'detail': 'unexpected exception'})
        run_report['errors'].append({'stage': 'revalidation_or_action', 'title': title})
        return counters
    finally:
        run_report['groups'].append(record)


def revalidate_and_act(groups):
    """Pass 2 driver. Returns aggregated counters."""
    print("\n[PASS 2/2] Revalidation + action — re-fetching from Plex...")
    log.info("PASS2 START groups=%d", len(groups))

    totals = dict.fromkeys((
        'groups_processed', 'groups_skipped_user', 'groups_skipped_safety',
        'groups_skipped_inconsistent', 'groups_failed_refetch',
        'items_removed', 'items_failed', 'freed_bytes',
    ), 0)

    for group in groups:
        deltas = _revalidate_and_act_group(group)
        for k, v in deltas.items():
            totals[k] += v

    return totals


############################################################
# MAIN
############################################################

if __name__ == "__main__":
    print("""
       _                 _                   __ _           _
 _ __ | | _____  __   __| |_   _ _ __   ___ / _(_)_ __   __| | ___ _ __
| '_ \\| |/ _ \\ \\/ /  / _` | | | | '_ \\ / _ \\ |_| | '_ \\ / _` |/ _ \\ '__|
| |_) | |  __/>  <  | (_| | |_| | |_) |  __/  _| | | | | (_| |  __/ |
| .__/|_|\\___/_/\\_\\  \\__,_|\\__,_| .__/ \\___|_| |_|_| |_|\\__,_|\\___|_|
|_|                             |_|

#########################################################################
# Author:   l3uddz                                                      #
# URL:      https://github.com/l3uddz/plex_dupefinder                   #
# --                                                                    #
#         Part of the Cloudbox project: https://cloudbox.works          #
#########################################################################
#                   GNU General Public License v3.0                     #
#########################################################################
""")
    # Runtime setup (deferred from import so the module stays import-safe for tests).
    setup_logging()
    validate_config()
    plex = connect_plex()
    validate_libraries(plex)

    print("Initialized — run_id=%s" % run_id)

    audit_mode = bool(cfg.get('AUDIT_MODE', False))
    if audit_mode:
        # Force DRY_RUN at runtime so every downstream guard observes it.
        # This is intentionally NOT persisted to disk.
        cfg['DRY_RUN'] = True
        log.info("AUDIT_MODE active — forcing DRY_RUN=True for this run")

    dry_run = bool(cfg.get('DRY_RUN', True))
    quarantine_mode = bool(cfg.get('QUARANTINE_MODE', True))
    auto_delete = bool(cfg.get('AUTO_DELETE', False))

    print("=" * 60)
    if audit_mode:
        print("MODE: AUDIT — full pipeline (discovery + revalidation + reports), NO actions")
    elif dry_run:
        print("MODE: DRY-RUN — no files or Plex entries will be touched")
    elif quarantine_mode:
        print("MODE: QUARANTINE — files will be moved to:")
        print("      %s" % cfg.get('QUARANTINE_DIR'))
    else:
        print("MODE: DIRECT DELETE — Plex will DELETE the file from disk (irreversible, no quarantine)")

    # Effective keeper-selection behaviour, made explicit so an operator can see
    # at a glance whether the run will ever block on input. Per-group prompts
    # happen ONLY in interactive mode (AUTO_DELETE=false) AND when the run
    # actually acts (not DRY_RUN). AUDIT_MODE forces DRY_RUN=True, so it — like
    # any dry run — is fully unattended.
    confirm_before_action = bool(cfg.get('CONFIRM_BEFORE_ACTION', True))
    acting = (not dry_run) and (not audit_mode)
    interactive_mode = (not auto_delete) if acting else confirm_before_action
    print("INTERACTIVE_MODE=%s (per-group keeper prompts) | AUDIT_MODE=%s | CONFIRM_BEFORE_ACTION=%s"
          % (interactive_mode, audit_mode, confirm_before_action))
    print("AUTO_DELETE=%s | MIN_FILE_AGE_HOURS=%s | MIN_SCORE_DIFFERENCE=%s | MAX_SIZE_RATIO=%s"
          % (auto_delete, cfg.get('MIN_FILE_AGE_HOURS'),
             cfg.get('MIN_SCORE_DIFFERENCE'), cfg.get('MAX_SIZE_RATIO')))
    print("STABILITY_CHECK_SECONDS=%s | PARTIAL_HASH_ENABLED=%s | REQUIRE_LOCAL_FS_ACCESS=%s"
          % (cfg.get('STABILITY_CHECK_SECONDS'),
             cfg.get('PARTIAL_HASH_ENABLED'), cfg.get('REQUIRE_LOCAL_FS_ACCESS')))
    print("PRE_ANALYZE_DUPLICATES=%s | ANALYZE_TIMEOUT_SECONDS=%s"
          % (cfg.get('PRE_ANALYZE_DUPLICATES'), cfg.get('ANALYZE_TIMEOUT_SECONDS')))
    print("=" * 60)

    if not dry_run and not quarantine_mode and auto_delete:
        confirm = input("Direct delete + auto delete is destructive. Type 'I UNDERSTAND' to continue: ").strip()
        if confirm != 'I UNDERSTAND':
            print("Aborting.")
            sys.exit(0)

    # ----- PASS 1 -----
    groups = discovery_pass(cfg['PLEX_LIBRARIES'])

    actionable = [g for g in groups if not g['decision']['skip']]
    skipped_p1 = len(groups) - len(actionable)
    print("\nDiscovery complete: %d total | %d actionable | %d skipped by safety"
          % (len(groups), len(actionable), skipped_p1))
    log.info("PASS1 END total=%d actionable=%d skipped=%d",
             len(groups), len(actionable), skipped_p1)

    plan_path = write_plan_file(groups)

    pre_analyze_enabled = bool(cfg.get('PRE_ANALYZE_DUPLICATES', False))
    pass0_attempted = sum(1 for g in groups if (g.get('pass0_status') or {}).get('attempted'))
    pass0_changed = sum(1 for g in groups
                        if (g.get('pass0_status') or {}).get('status') == 'sane_and_changed')
    pass0_unchanged = sum(1 for g in groups
                          if (g.get('pass0_status') or {}).get('status') == 'sane_unchanged')
    pass0_failed = sum(1 for g in groups
                       if (g.get('pass0_status') or {}).get('attempted')
                       and not (g.get('pass0_status') or {}).get('success'))
    run_report['phases']['pass0'] = {
        'enabled': pre_analyze_enabled,
        'groups_attempted': pass0_attempted,
        'groups_sane_and_changed': pass0_changed,
        'groups_sane_unchanged': pass0_unchanged,
        'groups_failed': pass0_failed,
        'timeout_seconds': cfg.get('ANALYZE_TIMEOUT_SECONDS'),
        'note': ('sane_unchanged is ambiguous — metadata is valid but we '
                 'could not prove analyze() actually ran (Plex async). '
                 'Treated as success; see refresh_plex_item docstring.'),
    }

    run_report['phases']['discovery'] = {
        'completed_at': datetime.now(timezone.utc).isoformat(),
        'groups_found': len(groups),
        'groups_actionable': len(actionable),
        'groups_skipped': skipped_p1,
        'plan_path': plan_path,
    }

    # Confirmation gate before Pass 2 acts destructively.
    if (not dry_run and auto_delete
            and cfg.get('CONFIRM_BEFORE_ACTION', True)
            and actionable):
        ans = input(
            "\n%d groups are actionable. Proceed to Pass 2 (re-validate + act)? "
            "Type 'YES' (uppercase) to continue: " % len(actionable)
        ).strip()
        if ans != 'YES':
            print("Aborting before Pass 2. Plan file preserved for review.")
            run_report['phases']['revalidation'] = {'status': 'aborted_by_user'}
            write_json_report()
            sys.exit(0)

    # ----- PASS 2 -----
    totals = revalidate_and_act(groups)
    run_report['phases']['revalidation'] = {
        'completed_at': datetime.now(timezone.utc).isoformat(),
        'groups_skipped_inconsistent': totals['groups_skipped_inconsistent'],
        'groups_failed_refetch': totals['groups_failed_refetch'],
    }
    run_report['phases']['action'] = {
        'completed_at': datetime.now(timezone.utc).isoformat(),
        'groups_processed': totals['groups_processed'],
        'items_removed': totals['items_removed'],
        'items_failed': totals['items_failed'],
        'freed_bytes': totals['freed_bytes'],
    }

    run_report['integrations'] = {
        'plex_refresh': refresh_plex_libraries(cfg.get('PLEX_LIBRARIES') or []),
        'radarr': trigger_radarr_rescan(),
        'sonarr': trigger_sonarr_rescan(),
    }

    if audit_mode:
        mode_label = 'AUDIT'
    elif dry_run:
        mode_label = 'DRY-RUN'
    elif quarantine_mode:
        mode_label = 'QUARANTINE'
    else:
        mode_label = 'DIRECT DELETE'
    run_report['summary'] = {
        'mode': mode_label,
        'groups_found': len(groups),
        'groups_processed': totals['groups_processed'],
        'groups_skipped_user': totals['groups_skipped_user'],
        'groups_skipped_safety': totals['groups_skipped_safety'],
        'groups_skipped_inconsistent': totals['groups_skipped_inconsistent'],
        'groups_failed_refetch': totals['groups_failed_refetch'],
        'items_removed': totals['items_removed'],
        'items_failed': totals['items_failed'],
        'freed_bytes': totals['freed_bytes'],
        'freed_human': bytes_to_string(totals['freed_bytes']),
    }

    quarantine_stats = summarize_quarantine()
    run_report['quarantine'] = quarantine_stats

    write_json_report()

    print("\n" + "=" * 60)
    print("SUMMARY (run_id=%s)" % run_id)
    print("=" * 60)
    print("Mode                              : %s" % mode_label)
    if pre_analyze_enabled:
        print("PASS 0 demonstrably refreshed     : %d  (changed metadata observed)"
              % pass0_changed)
        print("PASS 0 sane but unchanged         : %d  (ambiguous: see report)"
              % pass0_unchanged)
        print("PASS 0 failed (timeout / error)   : %d" % pass0_failed)
    print("Groups found                      : %d" % len(groups))
    print("Groups processed                  : %d" % totals['groups_processed'])
    print("Groups skipped by user            : %d" % totals['groups_skipped_user'])
    print("Groups skipped by safety (Pass 1) : %d" % totals['groups_skipped_safety'])
    print("Groups skipped (Pass 2 drift)     : %d" % totals['groups_skipped_inconsistent'])
    print("Groups failed re-fetch            : %d" % totals['groups_failed_refetch'])
    print("Items removed                     : %d" % totals['items_removed'])
    print("Items failed to remove            : %d" % totals['items_failed'])
    print("Space freed (actual moves)        : %s" % bytes_to_string(totals['freed_bytes']))

    if quarantine_stats['enabled']:
        print("-" * 60)
        print("QUARANTINE (standing total in %s)" % quarantine_stats['dir'])
        print("Files in quarantine               : %d" % quarantine_stats['file_count'])
        print("Quarantine size                   : %s" % quarantine_stats['total_human'])
        oldest = quarantine_stats['oldest_age_days']
        print("Oldest file age                   : %s"
              % ("%.1f days" % oldest if oldest is not None else "n/a"))
        retention = quarantine_stats['retention_days']
        if retention:
            print("Files older than %-4s days        : %d"
                  % (retention, quarantine_stats['files_over_retention']))
        if quarantine_stats['sidecars_unreadable']:
            print("Unreadable sidecars (skipped)     : %d"
                  % quarantine_stats['sidecars_unreadable'])
        print("(quarantine is never auto-purged — review and clear it manually)")
