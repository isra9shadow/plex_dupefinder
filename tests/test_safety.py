"""Minimal safety tests for the decision functions that can delete media.

Covers, per the production hardening brief:
  * get_score()               — scoring favours efficient/quality encodes
  * select_keeper()           — every skip guard that prevents a bad deletion
  * check_file_exists()       — filesystem-authoritative existence
  * _quarantine_logical_path()— quarantine destinations stay inside the title tree
  * detect_inconsistencies()  — drift between PASS 1 and PASS 2 aborts the group

Run with:  pytest tests/ -q
"""
import copy
import os

import pytest

import plex_dupefinder as pd


@pytest.fixture
def cfg():
    """Fresh, deterministic config for each test (defaults with all optional
    skip-guards disabled, so tests opt into the one they exercise)."""
    base = copy.deepcopy(pd.cfg)
    base.update(
        MIN_FILE_AGE_HOURS=0,
        MIN_SCORE_DIFFERENCE=0,
        MAX_SIZE_RATIO=0,
        REQUIRE_LOCAL_FS_ACCESS=False,
        FIND_DUPLICATE_FILEPATHS_ONLY=False,
        PARTIAL_HASH_ENABLED=False,
    )
    pd.cfg = base
    return base


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def make_media(codec='hevc', res='1080', acodec='truehd', bitrate=8000,
               duration=3_600_000, w=1920, h=1080, channels=6, size=10 ** 9,
               files=None, hdr=False, dv=False, subs=0, atracks=0):
    return {
        'audio_codec': acodec, 'video_codec': codec, 'video_resolution': res,
        'video_bitrate': bitrate, 'video_duration': duration,
        'video_width': w, 'video_height': h, 'audio_channels': channels,
        'file': files if files is not None else ['/m/Movie.1080p.BluRay.Remux.mkv'],
        'file_size': size, 'has_hdr': hdr, 'has_dv': dv,
        'subtitle_count': subs, 'audio_track_count': atracks,
    }


def make_part(media_id, score, size, exists=True, age_hours=100.0, local=True,
              duration=3_600_000, bitrate=5000, codec='hevc', files=None):
    files = files if files is not None else ['/mnt/media/x_%s.mkv' % media_id]
    return {
        'id': media_id, 'score': score, 'file_size': size, 'exists': exists,
        'file': files, 'video_duration': duration, 'video_bitrate': bitrate,
        'video_codec': codec,
        'parts_existence': [
            {'local_check': local, 'age_hours': age_hours, 'file': f} for f in files
        ],
    }


# --------------------------------------------------------------------------- #
# get_score
# --------------------------------------------------------------------------- #

def test_get_score_returns_total_and_breakdown(cfg):
    total, breakdown = pd.get_score(make_media())
    assert isinstance(total, int)
    assert isinstance(breakdown, dict)
    for key in ('audio_codec', 'video_codec', 'resolution', 'filename',
                'bitrate', 'duration', 'dimensions', 'audio_channels'):
        assert key in breakdown


def test_get_score_hevc_remux_beats_h264_webdl(cfg):
    remux = make_media(codec='hevc', acodec='truehd',
                       files=['/m/Movie.1080p.BluRay.Remux.mkv'])
    webdl = make_media(codec='h264', acodec='aac',
                       files=['/m/Movie.1080p.WEB-DL.mkv'])
    assert pd.get_score(remux)[0] > pd.get_score(webdl)[0]


def test_get_score_components_match_config(cfg):
    _, b = pd.get_score(make_media(codec='hevc', res='1080'))
    assert b['video_codec'] == cfg['VIDEO_CODEC_SCORES']['hevc']
    assert b['resolution'] == cfg['VIDEO_RESOLUTION_SCORES']['1080']


def test_get_score_hdr_and_dv_add_bonuses(cfg):
    plain = pd.get_score(make_media(hdr=False, dv=False))[0]
    fancy = pd.get_score(make_media(hdr=True, dv=True))[0]
    assert fancy - plain == cfg['HDR_SCORE'] + cfg['DOLBY_VISION_SCORE']


# --------------------------------------------------------------------------- #
# select_keeper
# --------------------------------------------------------------------------- #

def test_select_keeper_picks_highest_score(cfg):
    parts = {1: make_part(1, score=1000, size=10), 2: make_part(2, score=5000, size=10)}
    d = pd.select_keeper(parts)
    assert d['skip'] is False
    assert d['keeper_id'] == 2


def test_select_keeper_skips_when_no_candidate_exists(cfg):
    parts = {1: make_part(1, 1000, 10, exists=False),
             2: make_part(2, 5000, 10, exists=False)}
    d = pd.select_keeper(parts)
    assert d['skip'] is True
    assert d['keeper_id'] is None


def test_select_keeper_excludes_missing_candidate(cfg):
    # Higher-scoring file is missing on disk → must NOT be chosen.
    parts = {1: make_part(1, 9999, 10, exists=False),
             2: make_part(2, 100, 10, exists=True)}
    d = pd.select_keeper(parts)
    assert d['keeper_id'] == 2


def test_select_keeper_cooldown_skips_young_files(cfg):
    cfg['MIN_FILE_AGE_HOURS'] = 24
    parts = {1: make_part(1, 5000, 10, age_hours=1.0),
             2: make_part(2, 1000, 10, age_hours=200.0)}
    d = pd.select_keeper(parts)
    assert d['skip'] is True
    assert d['reason'] == 'cooldown protection'


def test_select_keeper_score_delta_threshold(cfg):
    cfg['MIN_SCORE_DIFFERENCE'] = 1000
    parts = {1: make_part(1, 5000, 10), 2: make_part(2, 5500, 10)}  # delta 500 < 1000
    d = pd.select_keeper(parts)
    assert d['skip'] is True
    assert d['reason'] == 'score delta too small'


def test_select_keeper_size_ratio_protection(cfg):
    cfg['MAX_SIZE_RATIO'] = 5.0
    # Keeper wins on score but is tiny; a sibling 10x larger signals a likely
    # mis-pairing, so the group must be skipped.
    parts = {1: make_part(1, 9000, size=1_000_000),       # keeper, small
             2: make_part(2, 1000, size=10_000_000)}       # 10x larger sibling
    d = pd.select_keeper(parts)
    assert d['skip'] is True
    assert d['reason'] == 'size ratio protection'


def test_select_keeper_skips_on_insane_metadata(cfg):
    bad = make_part(1, 5000, 10)
    bad['video_bitrate'] = 0          # insane → analysis incomplete
    parts = {1: bad, 2: make_part(2, 1000, 10)}
    d = pd.select_keeper(parts)
    assert d['skip'] is True
    assert d['reason'] == 'metadata sanity check failed'


# --------------------------------------------------------------------------- #
# check_file_exists
# --------------------------------------------------------------------------- #

def test_check_file_exists_local_and_plex_agree(tmp_path):
    f = tmp_path / 'movie.mkv'
    f.write_text('x')
    r = pd.check_file_exists(str(f), plex_exists=True)
    assert r['exists'] is True


def test_check_file_exists_disagreement_is_missing(tmp_path):
    f = tmp_path / 'movie.mkv'
    f.write_text('x')
    # File is really there but Plex says gone → treat as MISSING for safety.
    r = pd.check_file_exists(str(f), plex_exists=False)
    assert r['exists'] is False
    assert 'DISAGREEMENT' in r['reason']


def test_check_file_exists_local_only_missing(tmp_path):
    missing = tmp_path / 'gone.mkv'   # parent dir exists, file does not
    r = pd.check_file_exists(str(missing), plex_exists=None, plex_accessible=None)
    assert r['exists'] is False


def test_check_file_exists_plex_only_when_fs_unreachable():
    # Parent dir not present on this host → fall back to Plex's claim.
    r = pd.check_file_exists('/no_such_dir_zzz/movie.mkv', plex_exists=True)
    assert r['exists'] is True
    assert 'plex-only' in r['reason']


# --------------------------------------------------------------------------- #
# _quarantine_logical_path
# --------------------------------------------------------------------------- #

def _norm(p):
    return p.replace(os.sep, '/')


def test_quarantine_path_anchors_on_show_title():
    src = '/mnt/user/Media/TV/Breaking Bad/Season 01/ep.mkv'
    assert _norm(pd._quarantine_logical_path(src, 'Breaking Bad')) == \
        'Breaking Bad/Season 01/ep.mkv'


def test_quarantine_path_matches_movie_folder_with_year():
    src = '/mnt/user/Media/Movies/Dune (2021)/Dune.2021.mkv'
    assert _norm(pd._quarantine_logical_path(src, 'Dune')) == \
        'Dune (2021)/Dune.2021.mkv'


def test_quarantine_path_fallback_without_title_match():
    src = '/mnt/user/Media/Movies/Some Folder/file.mkv'
    # No title match → last two dirs + filename.
    assert _norm(pd._quarantine_logical_path(src, 'Totally Different')) == \
        'Movies/Some Folder/file.mkv'


def test_quarantine_path_never_escapes_with_traversal():
    # Result is always relative (no leading slash, no '..').
    out = _norm(pd._quarantine_logical_path('/a/b/c/d.mkv', 'b'))
    assert not out.startswith('/')
    assert '..' not in out.split('/')


# --------------------------------------------------------------------------- #
# detect_inconsistencies
# --------------------------------------------------------------------------- #

def _decision(keeper_id=2, skip=False):
    return {'keeper_id': keeper_id, 'skip': skip, 'skip_reason': None}


def test_detect_inconsistencies_clean_when_identical(cfg):
    parts = {1: make_part(1, 1000, 10), 2: make_part(2, 5000, 10)}
    snap = copy.deepcopy(parts)
    diffs = pd.detect_inconsistencies(snap, parts, _decision(), _decision())
    assert diffs == []


def test_detect_inconsistencies_flags_size_change(cfg):
    snap = {1: make_part(1, 1000, 10), 2: make_part(2, 5000, 10)}
    fresh = copy.deepcopy(snap)
    fresh[2]['file_size'] = 999  # file changed between passes
    diffs = pd.detect_inconsistencies(snap, fresh, _decision(), _decision())
    assert any('size changed' in d for d in diffs)


def test_detect_inconsistencies_flags_fresh_skip(cfg):
    parts = {1: make_part(1, 1000, 10), 2: make_part(2, 5000, 10)}
    diffs = pd.detect_inconsistencies(parts, copy.deepcopy(parts),
                                      _decision(), _decision(skip=True))
    assert any('wants to skip' in d for d in diffs)


def test_detect_inconsistencies_flags_keeper_change(cfg):
    parts = {1: make_part(1, 1000, 10), 2: make_part(2, 5000, 10)}
    diffs = pd.detect_inconsistencies(parts, copy.deepcopy(parts),
                                      _decision(keeper_id=2), _decision(keeper_id=1))
    assert any('keeper changed' in d for d in diffs)
