#!/usr/bin/env python3


import json
import os
import sys

from plexapi.myplex import MyPlexAccount
from getpass import getpass

config_path = os.path.join(os.path.dirname(os.path.realpath(sys.argv[0])), 'config.json')
base_config = {
    'PLEX_SERVER': 'https://plex.your-server.com',
    'PLEX_TOKEN': '',
    'PLEX_LIBRARIES': {},
    'AUDIO_CODEC_SCORES': {'Unknown': 0, 'wmapro': 200, 'mp2': 500, 'mp3': 1000, 'ac3': 1000, 'dca': 2000, 'pcm': 2500,
                           'flac': 2500, 'dca-ma': 4000, 'truehd': 4500, 'aac': 1000, 'eac3': 1250, 'opus': 1500},
    # Modernised: HEVC/AV1 are the preferred archive codecs in 2024+; H.264 is
    # acceptable but no longer the winner; legacy codecs are penalised.
    'VIDEO_CODEC_SCORES': {'Unknown': 0,
                           'av1': 14000,
                           'hevc': 12000, 'h265': 12000,
                           'h264': 8000,
                           'vp9': 6000,
                           'mpeg4': -3000,
                           'vc1': -2000,
                           'mpeg1video': -5000, 'mpeg2video': -5000,
                           'wmv2': -8000, 'wmv3': -8000,
                           'msmpeg4': -8000, 'msmpeg4v2': -8000, 'msmpeg4v3': -8000},
    'VIDEO_RESOLUTION_SCORES': {'Unknown': 0, '4k': 20000, '1080': 10000, '720': 5000, '480': 3000, 'sd': 1000},
    # Modernised default — keep MKV, penalise legacy containers, prefer modern
    # WEB-DL / BluRay encodes. Users with existing config.json keep their
    # scores; only fresh installs pick up these defaults.
    # Default OFF: for modern storage-efficient libraries, raw file size
    # rewards bloated encodes. Codec, resolution and filename signals are
    # more reliable quality indicators. Turn on if you want size to break
    # ties.
    'SCORE_FILESIZE': False,
    'FILENAME_SCORES': {
        '*Remux*': 25000,
        '*2160p*BluRay*': 20000,
        '*4K*BluRay*': 20000,
        '*1080p*BluRay*': 15000,
        '*720p*BluRay*': 8000,
        '*2160p*WEB-DL*': 14000,
        '*4K*WEB-DL*': 14000,
        '*1080p*WEB-DL*': 12000,
        '*WEB-DL*': 6000,
        '*WEBRip*': 4000,
        '*HDTV*': -5000,
        '*DVDRip*': -3000,
        '*dvd*': -3000,
        '*CAM*': -20000,
        '*TS*': -5000,
        '*REPACK*': 1500,
        '*PROPER*': 1500,
        '*EXTENDED*': 500,
        '*.mkv': 2000,
        '*.mp4': 500,
        '*.avi': -10000,
        '*.ts': -5000,
        '*.vob': -10000,
        '*.wmv': -8000,
        '*.flv': -10000,
    },
    'SKIP_LIST': [],
    # Lightweight metadata bonuses (read from Plex, no extra I/O).
    'HDR_SCORE': 3000,
    'DOLBY_VISION_SCORE': 5000,
    'SUBTITLE_SCORE_PER_TRACK': 50,
    'AUDIO_TRACK_SCORE': 100,
    'AUTO_DELETE': False,
    'FIND_DUPLICATE_FILEPATHS_ONLY': False,

    # ----- Safety -----
    # DRY_RUN: simulate everything, never touch files or Plex. Default True
    # so a fresh install can never destroy data on first run.
    'DRY_RUN': True,
    # QUARANTINE_MODE: when not DRY_RUN, move files into QUARANTINE_DIR
    # instead of asking Plex to delete them. Recoverable, audit-friendly.
    'QUARANTINE_MODE': True,
    'QUARANTINE_DIR': '',
    'QUARANTINE_RETENTION_DAYS': 30,
    # Refuse to choose between two candidates whose scores are this close.
    # 0 disables the check. Recommended: >=1000 to require a clear winner.
    'MIN_SCORE_DIFFERENCE': 0,
    # Skip any group whose files are younger than this many hours. Protects
    # against race conditions with active downloads/moves/scans.
    # 0 disables. Default 24 = wait a day before considering a file stable.
    'MIN_FILE_AGE_HOURS': 24,
    # If any non-keeper sibling is more than this multiple of the keeper's
    # size, refuse to delete. Catches obvious mis-pairings (e.g. a 2 GB rip
    # 'beating' an 80 GB remux because of a scoring quirk).
    # 0 disables. Default 5.0.
    'MAX_SIZE_RATIO': 5.0,
    # When True, abort any group whose files are not reachable from this host.
    # Use when the script does not run on the Plex host and you do not trust
    # Plex's exists/accessible flags alone.
    'REQUIRE_LOCAL_FS_ACCESS': False,
    # Just before acting on a group, re-read every candidate's size, wait
    # this many seconds, then read again. Any size change → skip the group.
    # Catches Tdarr transcodes / active copies that the cooldown missed.
    # Set to 0 to disable. Cost: this much extra time per actionable group.
    'STABILITY_CHECK_SECONDS': 2.0,
    # AUDIT_MODE: run Pass 1 + Pass 2 + revalidation + reporting but never
    # quarantine, delete, or modify Plex. Effectively forces DRY_RUN=True
    # at runtime. Use for scoring validation and regression testing.
    'AUDIT_MODE': False,

    # ----- Two-pass validation -----
    # Lightweight head+tail hash (no full-file read) used as a cheap
    # cross-pass consistency check: detects files actively being written or
    # transcoded between discovery and action.
    'PARTIAL_HASH_ENABLED': False,
    'PARTIAL_HASH_BYTES': 1048576,  # 1 MiB head + 1 MiB tail
    # Pause for confirmation between discovery and action when auto-deleting
    # in a non-dry-run mode. Manual mode ignores this (per-item prompts).
    'CONFIRM_BEFORE_ACTION': True,
    # Sleep between consecutive removals within a group during PASS 2, to
    # avoid hammering the Plex HTTP API. Increase if Plex returns rate-limit
    # errors. Set to 0 to disable.
    'PLEX_DELETE_DELAY_SECONDS': 2.0,
    # ----- PASS 0: optional Plex re-analyse before scoring -----
    # When True, every duplicate item gets item.analyze() called and we
    # poll for fresh metadata before discovery scores it. This avoids
    # decisions based on stale codec/bitrate/duration in Plex. Slow on
    # large libraries (sequential per item), so default OFF.
    'PRE_ANALYZE_DUPLICATES': False,
    'ANALYZE_TIMEOUT_SECONDS': 60,

    # ----- Reporting -----
    # Directory to write per-run JSON reports. Empty disables reporting.
    'JSON_REPORT_DIR': '',
    # File log verbosity for activity.log. One of DEBUG, INFO, WARNING, ERROR.
    # Default INFO; DEBUG adds per-part tracing (large on big libraries).
    # activity.log is size-rotated (10 MiB x 5 backups) regardless of level.
    'LOG_LEVEL': 'INFO',

    # ----- Integrations -----
    'PLEX_REFRESH_AFTER': False,
    'RADARR_URL': '',
    'RADARR_API_KEY': '',
    'RADARR_RESCAN_AFTER': False,
    'SONARR_URL': '',
    'SONARR_API_KEY': '',
    'SONARR_RESCAN_AFTER': False,

    # ----- Network -----
    'REQUESTS_TIMEOUT': 30,
}
cfg = None


def prefilled_default_config(configs):
    default_config = base_config.copy()

    # Set the token and server url
    default_config['PLEX_SERVER'] = configs['url']
    default_config['PLEX_TOKEN'] = configs['token']

    # Set AUTO_DELETE config option
    default_config['AUTO_DELETE'] = configs['auto_delete']

    # sections
    default_config['PLEX_LIBRARIES'] = [
        'Movies',
        'TV'
    ]

    # filename scores
    default_config['FILENAME_SCORES'] = {
        '*Remux*': 20000,
        '*1080p*BluRay*': 15000,
        '*720p*BluRay*': 10000,
        '*WEB*NTB*': 5000,
        '*WEB*VISUM*': 5000,
        '*WEB*KINGS*': 5000,
        '*WEB*CasStudio*': 5000,
        '*WEB*SiGMA*': 5000,
        '*WEB*QOQ*': 5000,
        '*WEB*TROLLHD*': 2500,
        '*REPACK*': 1500,
        '*PROPER*': 1500,
        '*WEB*TBS*': -1000,
        '*HDTV*': -1000,
        '*dvd*': -1000,
        '*.avi': -1000,
        '*.ts': -1000,
        '*.vob': -5000
    }

    return default_config


def build_config():
    if os.path.exists(config_path):
        return False
    print(f"Dumping default config to: {config_path}")

    configs = dict(url='', token='', auto_delete=False)

    # Get URL
    configs['url'] = input("Plex Server URL: ")

    # Get Credentials for plex.tv
    user = input("Plex Username: ")
    password = getpass('Plex Password: ')

    # Get choice for Auto Deletion
    auto_del = input("Auto Delete duplicates? [y/n]: ").strip().lower()
    while auto_del not in ('y', 'n'):
        auto_del = input("Auto Delete duplicates? [y/n]: ").strip().lower()
    configs['auto_delete'] = (auto_del == 'y')

    account = MyPlexAccount(user, password)
    configs['token'] = account.authenticationToken

    with open(config_path, 'w') as fp:
        json.dump(prefilled_default_config(configs), fp, sort_keys=True, indent=2)

    return True


def dump_config():
    if not os.path.exists(config_path):
        return False
    with open(config_path, 'w') as fp:
        json.dump(cfg, fp, sort_keys=True, indent=2)
    return True


def load_config():
    with open(config_path, 'r') as fp:
        return json.load(fp)


def upgrade_settings(defaults, currents):
    upgraded = False

    def inner_upgrade(default, current, key=None):
        sub_upgraded = False
        merged = current.copy()
        if isinstance(default, dict):
            for k, v in default.items():
                # missing k
                if k not in current:
                    merged[k] = v
                    sub_upgraded = True
                    if not key:
                        print("Added %r config option: %s" % (str(k), str(v)))
                    else:
                        print("Added %r to config option %r: %s" % (str(k), str(key), str(v)))
                    continue
                # iterate children
                if isinstance(v, (dict, list)):
                    did_upgrade, merged[k] = inner_upgrade(default[k], current[k], key=k)
                    sub_upgraded = did_upgrade or sub_upgraded

        elif isinstance(default, list) and key:
            for v in default:
                if v not in current:
                    merged.append(v)
                    sub_upgraded = True
                    print("Added to config option %r: %s" % (str(key), str(v)))
                    continue
        return sub_upgraded, merged

    upgraded, upgraded_settings = inner_upgrade(defaults, currents)
    return upgraded, upgraded_settings


############################################################
# LOAD CFG
############################################################

# config.json is loaded at import so existing tooling keeps working. The
# behaviour depends on what is available so the module can also be imported
# without side effects (e.g. by the test suite):
#   * config.json present        -> load it and merge in any new defaults
#                                    (exit after persisting if keys were added).
#   * absent, interactive (TTY)  -> run the first-run setup wizard, then exit.
#   * absent, non-interactive    -> fall back to built-in defaults, no prompt,
#                                    no exit (tests / tooling / CI).
if os.path.exists(config_path):
    tmp = load_config()
    upgraded, cfg = upgrade_settings(base_config, tmp)
    if upgraded:
        dump_config()
        print("New config options were added, adjust and restart!")
        sys.exit(0)
elif sys.stdin.isatty():
    build_config()
    print("Please edit the default configuration before running again!")
    sys.exit(0)
else:
    cfg = dict(base_config)
