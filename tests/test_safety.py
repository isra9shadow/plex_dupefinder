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


def test_get_score_hevc_beats_avc_even_with_higher_bitrate(cfg):
    # Issue #1: a bloated AVC must NOT outscore an efficient HEVC.
    hevc = make_media(codec='hevc', acodec='eac3', bitrate=8000,
                      files=['/m/Movie.1080p.WEB-DL.HEVC.mkv'])
    avc = make_media(codec='h264', acodec='eac3', bitrate=25000,
                     files=['/m/Movie.1080p.WEB-DL.x264.mkv'])
    assert pd.get_score(hevc)[0] > pd.get_score(avc)[0]


def test_get_score_codec_aliases(cfg):
    assert pd.get_score(make_media(codec='x265'))[1]['video_codec'] == \
        cfg['VIDEO_CODEC_SCORES']['hevc']
    for avc_alias in ('h264', 'x264', 'avc'):
        assert pd.get_score(make_media(codec=avc_alias))[1]['video_codec'] == 8000


def test_get_score_filename_positive_sum_is_capped(cfg):
    cfg['FILENAME_SCORE_CAP'] = 2000
    # REPACK(500)+PROPER(500)+EXTENDED(500)+.mkv(800) = 2300 -> clamped to 2000.
    m = make_media(files=['/m/Movie.REPACK.PROPER.EXTENDED.mkv'])
    assert pd.get_score(m)[1]['filename'] == 2000


def test_get_score_preference_order_matches_target(cfg):
    # 2160p DV/HDR HEVC > 2160p HEVC > 1080p REMUX > 1080p HEVC > 1080p AVC > 720p AVC
    a = make_media(codec='hevc', res='4k', hdr=True, dv=True, acodec='eac3',
                   channels=6, bitrate=18000, files=['/m/M.2160p.WEB-DL.DV.HDR.HEVC.mkv'])
    b = make_media(codec='hevc', res='4k', hdr=True, dv=False, acodec='eac3',
                   channels=6, bitrate=18000, files=['/m/M.2160p.WEB-DL.HDR.HEVC.mkv'])
    c = make_media(codec='h264', res='1080', acodec='truehd', channels=8,
                   bitrate=30000, files=['/m/M.1080p.BluRay.REMUX.AVC.TrueHD.mkv'])
    d = make_media(codec='hevc', res='1080', acodec='eac3', channels=6,
                   bitrate=8000, files=['/m/M.1080p.WEB-DL.HEVC.mkv'])
    e = make_media(codec='h264', res='1080', acodec='ac3', channels=6,
                   bitrate=12000, files=['/m/M.1080p.WEB-DL.x264.mkv'])
    f = make_media(codec='h264', res='720', acodec='ac3', channels=6,
                   bitrate=6000, files=['/m/M.720p.WEB-DL.x264.mkv'])
    scores = [pd.get_score(x)[0] for x in (a, b, c, d, e, f)]
    assert scores == sorted(scores, reverse=True), scores


# --------------------------------------------------------------------------- #
# _source_score (first-class source dimension)
# --------------------------------------------------------------------------- #

def test_source_score_ranking(cfg):
    cases = {
        '/m/Movie.2160p.BluRay.REMUX.mkv': ('remux', 8000),
        '/m/Movie.1080p.BluRay.x264.mkv': ('bluray', 3000),
        '/m/Movie.1080p.WEB-DL.mkv': ('web-dl', 2000),
        '/m/Movie.1080p.WEBRip.mkv': ('webrip', 1000),
        '/m/Movie.1080p.HDTV.mkv': ('hdtv', -3000),
        '/m/Movie.DVDRip.mkv': ('dvd', -3000),
        '/m/Movie.2019.CAM.mkv': ('cam', -15000),
    }
    for path, (key, score) in cases.items():
        assert pd._source_score([path]) == (score, key), path


def test_source_score_highest_tier_wins_when_multiple(cfg):
    # "BluRay REMUX" must score as remux, not bluray.
    assert pd._source_score(['/m/Movie.2160p.BluRay.REMUX.HEVC.mkv']) == (8000, 'remux')


def test_source_score_none_for_clean_filebot_name(cfg):
    # Filebot-renamed file with no source tag → no source signal (not negative).
    assert pd._source_score(['/m/Movie (2018).mkv']) == (0, None)


# --------------------------------------------------------------------------- #
# _max_audio_channels (MAX, not SUM)
# --------------------------------------------------------------------------- #

class _FakeStream:
    def __init__(self, channels):
        self.channels = channels


class _FakePart:
    def __init__(self, channels_list):
        self._streams = [_FakeStream(c) for c in channels_list]

    def audioStreams(self):
        return self._streams


class _FakeItem:
    def __init__(self, parts_channels):
        self.parts = [_FakePart(c) for c in parts_channels]


def test_max_audio_channels_uses_max_not_sum():
    # 7.1 + 5.1 + 2.0 commentary → 8 (richest track), NOT 16 (sum).
    assert pd._max_audio_channels(_FakeItem([[8, 6, 2]])) == 8


def test_max_audio_channels_across_parts():
    assert pd._max_audio_channels(_FakeItem([[2], [6]])) == 6


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
# resolve_fs_path (Plex logical path -> real filesystem path)
# --------------------------------------------------------------------------- #

def test_resolve_fs_path_passthrough_without_mappings(cfg):
    cfg['PATH_MAPPINGS'] = {}
    assert pd.resolve_fs_path('/tv/Show/ep.mkv') == '/tv/Show/ep.mkv'


def test_resolve_fs_path_maps_the_reported_unraid_case(cfg):
    cfg['PATH_MAPPINGS'] = {'/tv/': '/mnt/user/media/series TV/'}
    assert pd.resolve_fs_path('/tv/The Walking Dead/Season 10/ep.mkv') == \
        '/mnt/user/media/series TV/The Walking Dead/Season 10/ep.mkv'


def test_resolve_fs_path_longest_prefix_wins(cfg):
    cfg['PATH_MAPPINGS'] = {'/media/': '/a/', '/media/tv/': '/b/'}
    assert pd.resolve_fs_path('/media/tv/Show/ep.mkv') == '/b/Show/ep.mkv'


def test_resolve_fs_path_no_match_passthrough(cfg):
    cfg['PATH_MAPPINGS'] = {'/movies/': '/x/'}
    assert pd.resolve_fs_path('/tv/Show/ep.mkv') == '/tv/Show/ep.mkv'


def test_resolve_fs_path_empty_or_none(cfg):
    cfg['PATH_MAPPINGS'] = {'/tv/': '/x/'}
    assert pd.resolve_fs_path('') == ''
    assert pd.resolve_fs_path(None) is None


# --------------------------------------------------------------------------- #
# quarantine_files observability (console output + error detail)
# --------------------------------------------------------------------------- #

def test_quarantine_failure_reports_full_detail(cfg, tmp_path, capsys):
    cfg['QUARANTINE_DIR'] = str(tmp_path / 'q')
    os.makedirs(cfg['QUARANTINE_DIR'])
    missing = str(tmp_path / 'nope' / 'Movie' / 'ep.mkv')   # source does not exist
    res = pd.quarantine_files({'id': 1232465, 'file': [missing]},
                              title='Movie', library_name='Movies')
    assert res['moved'] == []
    err = res['errors'][0]
    assert err['exception_type'] == 'FileNotFoundError'
    assert err['source_exists'] is False
    out = capsys.readouterr().out
    assert 'QUARANTINE FAILED' in out
    assert 'media_id=1232465' in out
    assert 'exception_type=FileNotFoundError' in out
    assert 'source_exists=False' in out
    assert 'destination_parent_exists=' in out


def test_quarantine_success_moves_file_and_reports(cfg, tmp_path, capsys):
    cfg['QUARANTINE_DIR'] = str(tmp_path / 'q')
    os.makedirs(cfg['QUARANTINE_DIR'])
    src_dir = tmp_path / 'media' / 'Movie'
    src_dir.mkdir(parents=True)
    src = src_dir / 'Movie.mkv'
    src.write_bytes(b'x' * 2048)
    res = pd.quarantine_files({'id': 7, 'file': [str(src)]},
                              keeper_info={'file': ['/k.mkv'], 'score': 1},
                              title='Movie', library_name='Movies')
    assert len(res['moved']) == 1
    assert os.path.isfile(res['moved'][0])    # the file is physically in QUARANTINE_DIR
    assert not src.exists()                   # source was moved away
    out = capsys.readouterr().out
    assert 'QUARANTINED' in out
    assert 'media_id=7' in out
    assert 'elapsed_ms=' in out


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
