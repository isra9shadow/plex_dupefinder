"""Safety/observability tests for the operational features added for
autonomous Unraid operation:

  * classify_exception()          — exceptions map to the right failure bucket
  * record_failure()              — counts + structured entry in the report
  * audit_scoring_config()        — FILENAME/SOURCE double-counting is detected
  * compute_lowest_confidence_groups() — riskiest decisions ranked first
  * purge_quarantine()            — DRY-RUN never deletes; real purge respects
                                    retention and fails closed on bad metadata
  * summarize_quarantine()        — read-only standing-quarantine accounting

These cover code paths that can DELETE data (auto-purge) or that an operator
relies on to trust a run (failure summary, confidence ranking), so they are
held to the same bar as the core decision tests.

Run with:  pytest tests/ -q
"""
import copy
import json
import os
from datetime import datetime, timedelta, timezone

import pytest

import plex_dupefinder as pd


@pytest.fixture
def cfg():
    """Fresh, deterministic config per test, RESTORED on teardown.

    Several tests here mutate cfg['FILENAME_SCORES'] (the scoring-audit cases).
    Without restoring the module global, that mutation would leak into other
    test files via their own deepcopy-of-pd.cfg fixtures, so we save and put
    back the original object."""
    original = pd.cfg
    base = copy.deepcopy(pd.cfg)
    pd.cfg = base
    yield base
    pd.cfg = original


@pytest.fixture
def fresh_report():
    """Reset the module-level run_report counters the observability code mutates,
    so assertions are not polluted by other tests sharing the process."""
    pd.run_report['failures'] = []
    pd.run_report['failure_summary'] = {c: 0 for c in pd.FAILURE_CATEGORIES}
    pd.run_report['scoring_audit'] = None
    return pd.run_report


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def _write_sidecar(qdir, rel_dir, name, *, age_days, size=1000):
    """Create a quarantined media file + its sidecar with a controlled age.

    Returns the absolute media-file path so a test can assert on its existence.
    """
    target_dir = os.path.join(qdir, rel_dir) if rel_dir else qdir
    os.makedirs(target_dir, exist_ok=True)
    media_path = os.path.join(target_dir, name)
    with open(media_path, 'wb') as fp:
        fp.write(b'\0' * size)
    ts = (datetime.now(timezone.utc) - timedelta(days=age_days)).isoformat()
    sidecar = media_path + '.dupefinder_meta.json'
    with open(sidecar, 'w', encoding='utf-8') as fp:
        json.dump({
            'quarantine_path': media_path,
            'quarantine_timestamp': ts,
            'original_size': size,
        }, fp)
    return media_path


# --------------------------------------------------------------------------- #
# classify_exception
# --------------------------------------------------------------------------- #

def test_classify_permission_error_is_permission_denied():
    assert pd.classify_exception(PermissionError("denied")) == 'PERMISSION_DENIED'


def test_classify_file_not_found_is_path_not_found():
    assert pd.classify_exception(FileNotFoundError("nope")) == 'PATH_NOT_FOUND'


def test_classify_requests_exception_is_plex_api_error():
    # requests is stubbed in conftest; RequestException is a real subclass there.
    assert pd.classify_exception(pd.requests.RequestException("boom")) == 'PLEX_API_ERROR'


def test_classify_generic_oserror_is_move_failed():
    assert pd.classify_exception(OSError("cross-device link")) == 'MOVE_FAILED'


def test_classify_unknown_exception_is_unknown():
    assert pd.classify_exception(ValueError("weird")) == 'UNKNOWN'


def test_classify_none_is_unknown():
    assert pd.classify_exception(None) == 'UNKNOWN'


# --------------------------------------------------------------------------- #
# record_failure
# --------------------------------------------------------------------------- #

def test_record_failure_counts_and_records(fresh_report, capsys):
    exc = PermissionError("EACCES")
    pd.record_failure('PERMISSION_DENIED', 'move blocked',
                      src='/src/a.mkv', dest='/q/a.mkv', exc=exc,
                      media_id=42, stage='quarantine', console=True)
    assert fresh_report['failure_summary']['PERMISSION_DENIED'] == 1
    assert len(fresh_report['failures']) == 1
    entry = fresh_report['failures'][0]
    assert entry['category'] == 'PERMISSION_DENIED'
    assert entry['source'] == '/src/a.mkv'
    assert entry['destination'] == '/q/a.mkv'
    assert entry['exception_type'] == 'PermissionError'
    assert entry['media_id'] == 42
    # The whole point: the failure is visible on the console, not just in logs.
    out = capsys.readouterr().out
    assert 'PERMISSION_DENIED' in out
    assert '/src/a.mkv' in out
    assert '/q/a.mkv' in out


def test_record_failure_unknown_category_is_normalised(fresh_report):
    pd.record_failure('NOT_A_REAL_CATEGORY', 'x', console=False)
    assert fresh_report['failure_summary']['UNKNOWN'] == 1
    assert fresh_report['failures'][0]['category'] == 'UNKNOWN'


def test_record_failure_console_false_is_silent(fresh_report, capsys):
    pd.record_failure('MOVE_FAILED', 'silent', console=False)
    assert capsys.readouterr().out == ''
    assert fresh_report['failure_summary']['MOVE_FAILED'] == 1


# --------------------------------------------------------------------------- #
# audit_scoring_config  (FILENAME vs SOURCE double-counting)
# --------------------------------------------------------------------------- #

def test_audit_scoring_clean_for_default_filename_scores(cfg, fresh_report):
    # Default FILENAME_SCORES holds only edition tags + container extensions.
    audit = None
    cfg['FILENAME_SCORES'] = {
        '*REPACK*': 500, '*PROPER*': 500, '*EXTENDED*': 500,
        '*.mkv': 800, '*.mp4': 300, '*.avi': -10000,
    }
    pd.audit_scoring_config()
    audit = pd.run_report['scoring_audit']
    assert audit['ok'] is True
    assert audit['filename_source_overlaps'] == []


@pytest.mark.parametrize('pattern,token', [
    ('*WEB-DL*', 'web-dl'),
    ('*WEBRip*', 'webrip'),
    ('*HDTV*', 'hdtv'),
    ('*BluRay*', 'bluray'),
    ('*REMUX*', 'remux'),
    ('*2160p*', '2160p'),
])
def test_audit_scoring_flags_source_resolution_overlap(cfg, fresh_report, pattern, token):
    cfg['FILENAME_SCORES'] = {pattern: 1000, '*.mkv': 800}
    pd.audit_scoring_config()
    audit = pd.run_report['scoring_audit']
    assert audit['ok'] is False
    flagged = {o['pattern']: o['token'] for o in audit['filename_source_overlaps']}
    assert pattern in flagged
    assert flagged[pattern] == token


def test_audit_scoring_does_not_flag_extension_patterns(cfg, fresh_report):
    cfg['FILENAME_SCORES'] = {'*.mkv': 800, '*.ts': -5000, '*.vob': -10000}
    pd.audit_scoring_config()
    assert pd.run_report['scoring_audit']['ok'] is True


# --------------------------------------------------------------------------- #
# compute_lowest_confidence_groups
# --------------------------------------------------------------------------- #

def _group(title, *, skip=False, delta=None, keeper_id=1):
    return {
        'title': title, 'library': 'Movies', 'item_key': '/lib/%s' % title,
        'decision': {
            'skip': skip, 'score_delta': delta,
            'top_score': 10000, 'second_score': (10000 - delta) if delta is not None else None,
            'keeper_id': keeper_id,
        },
        'parts': {keeper_id: {'file': ['/m/%s.mkv' % title]}},
    }


def test_lowest_confidence_orders_by_smallest_delta(cfg):
    groups = [_group('A', delta=5000), _group('B', delta=100), _group('C', delta=900)]
    ranked = pd.compute_lowest_confidence_groups(groups, limit=10)
    assert [r['title'] for r in ranked] == ['B', 'C', 'A']
    assert ranked[0]['score_delta'] == 100


def test_lowest_confidence_excludes_skipped_and_single_candidate(cfg):
    groups = [
        _group('skipped', skip=True, delta=10),     # skipped → excluded
        _group('single', delta=None),               # no runner-up → excluded
        _group('real', delta=300),
    ]
    ranked = pd.compute_lowest_confidence_groups(groups, limit=10)
    assert [r['title'] for r in ranked] == ['real']


def test_lowest_confidence_respects_limit(cfg):
    groups = [_group(str(i), delta=i * 100 + 1) for i in range(20)]
    ranked = pd.compute_lowest_confidence_groups(groups, limit=5)
    assert len(ranked) == 5


# --------------------------------------------------------------------------- #
# purge_quarantine  (the only NEW code path that deletes data)
# --------------------------------------------------------------------------- #

def test_purge_disabled_returns_noop(cfg, tmp_path):
    cfg['AUTO_PURGE_QUARANTINE'] = False
    cfg['QUARANTINE_DIR'] = str(tmp_path)
    result = pd.purge_quarantine(simulate=False)
    assert result['enabled'] is False
    assert result['removed_files'] == 0


def test_purge_simulate_never_deletes(cfg, tmp_path, fresh_report):
    cfg['AUTO_PURGE_QUARANTINE'] = True
    cfg['QUARANTINE_DIR'] = str(tmp_path)
    cfg['QUARANTINE_RETENTION_DAYS'] = 7
    old = _write_sidecar(str(tmp_path), 'Old Movie', 'old.mkv', age_days=30)

    result = pd.purge_quarantine(simulate=True)

    # Reported as a candidate, but the file is UNTOUCHED on disk (DRY-RUN guarantee).
    assert result['simulated'] is True
    assert result['removed_files'] == 1
    assert result['reclaimed_bytes'] == 1000
    assert os.path.exists(old)
    assert os.path.exists(old + '.dupefinder_meta.json')


def test_purge_real_removes_only_expired(cfg, tmp_path, fresh_report):
    cfg['AUTO_PURGE_QUARANTINE'] = True
    cfg['QUARANTINE_DIR'] = str(tmp_path)
    cfg['QUARANTINE_RETENTION_DAYS'] = 7
    expired = _write_sidecar(str(tmp_path), 'Old', 'old.mkv', age_days=30, size=2000)
    fresh = _write_sidecar(str(tmp_path), 'New', 'new.mkv', age_days=1, size=3000)

    result = pd.purge_quarantine(simulate=False)

    assert result['removed_files'] == 1
    assert result['reclaimed_bytes'] == 2000
    assert not os.path.exists(expired)
    assert not os.path.exists(expired + '.dupefinder_meta.json')
    # Within-retention file must survive.
    assert os.path.exists(fresh)
    assert os.path.exists(fresh + '.dupefinder_meta.json')


def test_purge_fails_closed_on_bad_timestamp(cfg, tmp_path, fresh_report):
    """A corrupt/missing quarantine_timestamp must NOT cause a delete."""
    cfg['AUTO_PURGE_QUARANTINE'] = True
    cfg['QUARANTINE_DIR'] = str(tmp_path)
    cfg['QUARANTINE_RETENTION_DAYS'] = 7
    media = os.path.join(str(tmp_path), 'broken.mkv')
    with open(media, 'wb') as fp:
        fp.write(b'\0' * 100)
    with open(media + '.dupefinder_meta.json', 'w', encoding='utf-8') as fp:
        json.dump({'quarantine_path': media, 'quarantine_timestamp': 'not-a-date'}, fp)

    result = pd.purge_quarantine(simulate=False)

    assert result['removed_files'] == 0
    assert os.path.exists(media)


def test_purge_zero_retention_is_skipped(cfg, tmp_path, fresh_report):
    cfg['AUTO_PURGE_QUARANTINE'] = True
    cfg['QUARANTINE_DIR'] = str(tmp_path)
    cfg['QUARANTINE_RETENTION_DAYS'] = 0
    old = _write_sidecar(str(tmp_path), '', 'old.mkv', age_days=999)
    result = pd.purge_quarantine(simulate=False)
    assert result['removed_files'] == 0
    assert os.path.exists(old)


# --------------------------------------------------------------------------- #
# summarize_quarantine
# --------------------------------------------------------------------------- #

def test_summarize_quarantine_counts_and_sizes(cfg, tmp_path):
    cfg['QUARANTINE_DIR'] = str(tmp_path)
    cfg['QUARANTINE_RETENTION_DAYS'] = 7
    _write_sidecar(str(tmp_path), 'A', 'a.mkv', age_days=30, size=1000)
    _write_sidecar(str(tmp_path), 'B', 'b.mkv', age_days=1, size=2000)

    summary = pd.summarize_quarantine()

    assert summary['enabled'] is True
    assert summary['file_count'] == 2
    assert summary['total_bytes'] == 3000
    assert summary['files_over_retention'] == 1          # only the 30-day-old one
    assert summary['oldest_age_days'] >= 29


def test_summarize_quarantine_disabled_when_no_dir(cfg):
    cfg['QUARANTINE_DIR'] = ''
    summary = pd.summarize_quarantine()
    assert summary['enabled'] is False
    assert summary['file_count'] == 0
