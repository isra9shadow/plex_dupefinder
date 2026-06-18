"""Characterization tests for the legacy engine's ACTION / side-effect helpers.

CTO-02 continues raising the coverage floor on ``plex_dupefinder.py`` (the only
component that moves/deletes media). ``test_engine_streams.py`` pinned the pure
helpers; this file pins the previously-uncovered helpers that have observable
side effects but can be exercised WITHOUT a real Plex server, real network or
the destructive deletion of real user files:

  * quarantine_files()        — real shutil.move into a tmp quarantine + sidecar,
                                plus collision suffixing and missing-source error
  * purge_quarantine()        — simulate vs real over a fake quarantine tree with
                                ``.dupefinder_meta.json`` sidecars; age filtering,
                                disabled / no-dir / zero-retention short-circuits
  * _prune_empty_dirs()       — bottom-up empty-folder cleanup, bounded by stop
  * refresh_plex_libraries()  — fake ``pd.plex`` whose section().update() is a
                                stub; attempted / success / per-library errors
  * _arr_post_command /        — ``requests.post`` monkeypatched to a fake
    trigger_radarr_rescan /      response object; success / HTTP-error / network
    trigger_sonarr_rescan        error mapping and the not-configured guards
  * remove_plex_metadata()    — fake ``requests.delete`` response; 204 vs HTTP
                                error vs RequestException mapping
  * classify_exception()      — full FAILURE_CATEGORIES mapping incl. precedence
  * record_failure()          — taxonomy bucketing, UNKNOWN fallback, report entry
  * validate_libraries()      — fail-fast SystemExit on a typo'd library name

Everything that touches disk does so strictly inside ``tmp_path``; nothing here
deletes a real user file. Globals (cfg, plex, run_report) are monkeypatched and
restored automatically.

Run with:  pytest tests/regression/test_engine_actions.py -q
"""

import copy
import json
import os
from datetime import UTC, datetime, timedelta

import plex_dupefinder as pd
import pytest

# --------------------------------------------------------------------------- #
# fixtures — deterministic cfg / run_report, restored by monkeypatch
# --------------------------------------------------------------------------- #


@pytest.fixture
def cfg(monkeypatch):
    """Fresh, deterministic config per test (deep copy, auto-restored)."""
    base = copy.deepcopy(pd.cfg)
    base.update(
        PATH_MAPPINGS={},
        QUARANTINE_DIR="",
        PLEX_REFRESH_AFTER=False,
        RADARR_RESCAN_AFTER=False,
        SONARR_RESCAN_AFTER=False,
        AUTO_PURGE_QUARANTINE=False,
        QUARANTINE_RETENTION_DAYS=7,
    )
    monkeypatch.setattr(pd, "cfg", base)
    return base


@pytest.fixture
def report(monkeypatch):
    """A clean run_report with a zeroed failure_summary, isolated per test."""
    fresh = {
        "failures": [],
        "failure_summary": {c: 0 for c in pd.FAILURE_CATEGORIES},
    }
    monkeypatch.setattr(pd, "run_report", fresh)
    return fresh


# --------------------------------------------------------------------------- #
# fakes — minimal Plex / requests stand-ins
# --------------------------------------------------------------------------- #


class FakeSection:
    def __init__(self, on_update=None):
        self.updated = 0
        self._on_update = on_update

    def update(self):
        self.updated += 1
        if self._on_update is not None:
            self._on_update()


class FakeLibrary:
    """A plex.library: section(name) -> FakeSection, optionally raising."""

    def __init__(self, sections=None, raise_for=None):
        self._sections = sections or {}
        self._raise_for = raise_for or set()

    def section(self, name):
        if name in self._raise_for:
            raise RuntimeError(f"no such section {name!r}")
        return self._sections.setdefault(name, FakeSection())


class FakePlex:
    def __init__(self, library):
        self.library = library


class FakeResponse:
    """A requests.Response stand-in exposing only what the engine reads."""

    def __init__(self, status_code=200, json_data=None, text="", raise_json=False):
        self.status_code = status_code
        self._json = json_data
        self.text = text
        self._raise_json = raise_json

    def json(self):
        if self._raise_json:
            raise ValueError("not json")
        return self._json


# --------------------------------------------------------------------------- #
# classify_exception
# --------------------------------------------------------------------------- #


def test_classify_exception_none_is_unknown():
    assert pd.classify_exception(None) == "UNKNOWN"


def test_classify_exception_permission_before_oserror():
    # PermissionError is an OSError subclass — it must map to PERMISSION_DENIED,
    # not the generic MOVE_FAILED bucket.
    assert pd.classify_exception(PermissionError("nope")) == "PERMISSION_DENIED"


def test_classify_exception_file_not_found():
    assert pd.classify_exception(FileNotFoundError("gone")) == "PATH_NOT_FOUND"


def test_classify_exception_requests_error_is_plex_api():
    assert pd.classify_exception(pd.requests.RequestException("net")) == "PLEX_API_ERROR"


def test_classify_exception_generic_oserror_is_move_failed():
    assert pd.classify_exception(OSError("disk")) == "MOVE_FAILED"


def test_classify_exception_unrelated_is_unknown():
    assert pd.classify_exception(ValueError("?")) == "UNKNOWN"


# --------------------------------------------------------------------------- #
# record_failure
# --------------------------------------------------------------------------- #


def test_record_failure_buckets_known_category(report, capsys):
    pd.record_failure(
        "MOVE_FAILED",
        "boom",
        src="/s",
        dest="/d",
        exc=OSError("x"),
        media_id=7,
        stage="quarantine",
    )
    assert report["failure_summary"]["MOVE_FAILED"] == 1
    assert len(report["failures"]) == 1
    entry = report["failures"][0]
    assert entry["category"] == "MOVE_FAILED"
    assert entry["stage"] == "quarantine"
    assert entry["media_id"] == 7
    assert entry["source"] == "/s"
    assert entry["destination"] == "/d"
    assert entry["exception_type"] == "OSError"
    assert entry["exception"] == "x"
    # console=True (default) prints a loud banner an operator can see live.
    out = capsys.readouterr().out
    assert "FAILURE [MOVE_FAILED]" in out
    assert "stage=quarantine" in out


def test_record_failure_unknown_category_falls_back(report):
    pd.record_failure("NOT_A_CATEGORY", "msg", console=False)
    assert report["failure_summary"]["UNKNOWN"] == 1
    assert report["failures"][0]["category"] == "UNKNOWN"


def test_record_failure_console_false_is_silent(report, capsys):
    pd.record_failure("PLEX_API_ERROR", "quiet", console=False)
    assert capsys.readouterr().out == ""
    assert report["failure_summary"]["PLEX_API_ERROR"] == 1


def test_record_failure_no_exception_records_nulls(report):
    pd.record_failure("UNKNOWN", "no exc", console=False)
    entry = report["failures"][0]
    assert entry["exception_type"] is None
    assert entry["exception"] is None


# --------------------------------------------------------------------------- #
# refresh_plex_libraries
# --------------------------------------------------------------------------- #


def test_refresh_plex_libraries_not_attempted_when_disabled(cfg):
    cfg["PLEX_REFRESH_AFTER"] = False
    result = pd.refresh_plex_libraries(["Movies"])
    assert result == {"attempted": False, "libraries": [], "errors": []}


def test_refresh_plex_libraries_not_attempted_when_no_libraries(cfg):
    cfg["PLEX_REFRESH_AFTER"] = True
    result = pd.refresh_plex_libraries([])
    assert result["attempted"] is False


def test_refresh_plex_libraries_triggers_update(cfg, monkeypatch, capsys):
    cfg["PLEX_REFRESH_AFTER"] = True
    library = FakeLibrary()
    monkeypatch.setattr(pd, "plex", FakePlex(library))
    result = pd.refresh_plex_libraries(["Movies", "TV"])
    assert result["attempted"] is True
    assert result["libraries"] == ["Movies", "TV"]
    assert result["errors"] == []
    assert library._sections["Movies"].updated == 1
    assert "Triggered Plex refresh" in capsys.readouterr().out


def test_refresh_plex_libraries_records_per_library_error(cfg, monkeypatch):
    cfg["PLEX_REFRESH_AFTER"] = True
    library = FakeLibrary(raise_for={"Broken"})
    monkeypatch.setattr(pd, "plex", FakePlex(library))
    result = pd.refresh_plex_libraries(["Movies", "Broken"])
    assert result["libraries"] == ["Movies"]  # the good one still succeeded
    assert len(result["errors"]) == 1
    assert result["errors"][0]["library"] == "Broken"
    assert "no such section" in result["errors"][0]["error"]


# --------------------------------------------------------------------------- #
# _arr_post_command  (requests.post monkeypatched to a fake)
# --------------------------------------------------------------------------- #


def _capture_post(monkeypatch, response=None, exc=None):
    """Patch pd.requests.post; return a list that records each call's kwargs."""
    calls = []

    def fake_post(url, **kwargs):
        calls.append({"url": url, **kwargs})
        if exc is not None:
            raise exc
        return response

    monkeypatch.setattr(pd.requests, "post", fake_post)
    return calls


def test_arr_post_command_success_with_json(monkeypatch):
    resp = FakeResponse(status_code=201, json_data={"id": 1, "name": "RescanMovie"})
    calls = _capture_post(monkeypatch, response=resp)
    result = pd._arr_post_command("Radarr", "http://arr:7878/", "KEY", "RescanMovie")
    assert result["attempted"] is True
    assert result["success"] is True
    assert result["command"] == "RescanMovie"
    assert result["detail"] == {"id": 1, "name": "RescanMovie"}
    # URL is normalised (trailing slash stripped) and the command endpoint used.
    assert calls[0]["url"] == "http://arr:7878/api/v3/command"
    assert calls[0]["json"] == {"name": "RescanMovie"}
    assert calls[0]["headers"]["X-Api-Key"] == "KEY"


def test_arr_post_command_success_non_json_body(monkeypatch):
    resp = FakeResponse(status_code=200, text="OK-but-not-json", raise_json=True)
    _capture_post(monkeypatch, response=resp)
    result = pd._arr_post_command("Sonarr", "http://arr", "K", "RescanSeries")
    assert result["success"] is True
    assert result["detail"] == "OK-but-not-json"


def test_arr_post_command_http_error_maps_to_failure(monkeypatch):
    resp = FakeResponse(status_code=500, text="kaboom")
    _capture_post(monkeypatch, response=resp)
    result = pd._arr_post_command("Radarr", "http://arr", "K", "RescanMovie")
    assert result["success"] is False
    assert result["detail"].startswith("HTTP 500")


def test_arr_post_command_network_error_maps_to_failure(monkeypatch):
    _capture_post(monkeypatch, exc=pd.requests.RequestException("conn refused"))
    result = pd._arr_post_command("Radarr", "http://arr", "K", "RescanMovie")
    assert result["success"] is False
    assert result["detail"] == "conn refused"


# --------------------------------------------------------------------------- #
# trigger_radarr_rescan / trigger_sonarr_rescan
# --------------------------------------------------------------------------- #


def test_trigger_radarr_rescan_disabled(cfg):
    cfg["RADARR_RESCAN_AFTER"] = False
    assert pd.trigger_radarr_rescan() == {"attempted": False}


def test_trigger_radarr_rescan_enabled(cfg, monkeypatch):
    cfg["RADARR_RESCAN_AFTER"] = True
    cfg["RADARR_URL"] = "http://radarr"
    cfg["RADARR_API_KEY"] = "RK"
    calls = _capture_post(monkeypatch, response=FakeResponse(200, json_data={}))
    result = pd.trigger_radarr_rescan()
    assert result["success"] is True
    assert calls[0]["json"] == {"name": "RescanMovie"}


def test_trigger_sonarr_rescan_disabled(cfg):
    cfg["SONARR_RESCAN_AFTER"] = False
    assert pd.trigger_sonarr_rescan() == {"attempted": False}


def test_trigger_sonarr_rescan_enabled(cfg, monkeypatch):
    cfg["SONARR_RESCAN_AFTER"] = True
    cfg["SONARR_URL"] = "http://sonarr"
    cfg["SONARR_API_KEY"] = "SK"
    calls = _capture_post(monkeypatch, response=FakeResponse(200, json_data={}))
    result = pd.trigger_sonarr_rescan()
    assert result["success"] is True
    assert calls[0]["json"] == {"name": "RescanSeries"}


# --------------------------------------------------------------------------- #
# remove_plex_metadata  (requests.delete monkeypatched to a fake)
# --------------------------------------------------------------------------- #


def test_remove_plex_metadata_success(cfg, report, monkeypatch):
    cfg["PLEX_SERVER"] = "http://plex:32400"
    cfg["PLEX_TOKEN"] = "TOK"
    monkeypatch.setattr(pd.requests, "delete", lambda *a, **k: FakeResponse(204))
    ok, detail = pd.remove_plex_metadata("/library/metadata/1", 99)
    assert ok is True
    assert detail == "http_204"


def test_remove_plex_metadata_http_error(cfg, report, monkeypatch):
    cfg["PLEX_SERVER"] = "http://plex:32400"
    cfg["PLEX_TOKEN"] = "TOK"
    monkeypatch.setattr(pd.requests, "delete", lambda *a, **k: FakeResponse(404, text="not found"))
    ok, detail = pd.remove_plex_metadata("/library/metadata/1", 99)
    assert ok is False
    assert detail.startswith("http_404")
    assert report["failure_summary"]["PLEX_API_ERROR"] == 1


def test_remove_plex_metadata_request_exception(cfg, report, monkeypatch):
    cfg["PLEX_SERVER"] = "http://plex:32400"
    cfg["PLEX_TOKEN"] = "TOK"

    def boom(*a, **k):
        raise pd.requests.RequestException("timeout")

    monkeypatch.setattr(pd.requests, "delete", boom)
    ok, detail = pd.remove_plex_metadata("/library/metadata/1", 99)
    assert ok is False
    assert detail.startswith("request_error")
    assert report["failure_summary"]["PLEX_API_ERROR"] == 1


# --------------------------------------------------------------------------- #
# quarantine_files  (real shutil.move strictly inside tmp_path)
# --------------------------------------------------------------------------- #


def _make_source(tmp_path, rel="Media/Movies/Dune (2021)/Dune.2021.mkv", data=b"x" * 16):
    src = tmp_path / "src" / rel
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(data)
    return src


def test_quarantine_files_raises_without_dir(cfg):
    cfg["QUARANTINE_DIR"] = ""
    with pytest.raises(RuntimeError, match="QUARANTINE_DIR is not configured"):
        pd.quarantine_files({"id": 1, "file": ["/whatever.mkv"]})


def test_quarantine_files_moves_and_writes_sidecar(cfg, report, tmp_path):
    qdir = tmp_path / "quarantine"
    qdir.mkdir()
    cfg["QUARANTINE_DIR"] = str(qdir)
    src = _make_source(tmp_path)

    out = pd.quarantine_files(
        {"id": 5, "file": [str(src)]},
        keeper_info={"file": ["/keep.mkv"], "score": 10, "score_breakdown": {}},
        reason="lower score",
        title="Dune",
        library_name="Movies",
        year=2021,
    )
    assert out["errors"] == []
    assert len(out["moved"]) == 1
    dest = out["moved"][0]
    # The logical sub-tree (title folder downward) is mirrored, not the abs prefix.
    assert os.path.isfile(dest)
    assert dest.endswith(os.path.join("Dune (2021)", "Dune.2021.mkv"))
    assert not src.exists()  # source really moved
    # Sidecar written alongside with full provenance.
    with open(dest + ".dupefinder_meta.json", encoding="utf-8") as fp:
        meta = json.load(fp)
    assert meta["media_id"] == 5
    assert meta["reason"] == "lower score"
    assert meta["title"] == "Dune"


def test_quarantine_files_missing_source_records_failure(cfg, report, tmp_path):
    qdir = tmp_path / "quarantine"
    qdir.mkdir()
    cfg["QUARANTINE_DIR"] = str(qdir)
    ghost = tmp_path / "src" / "Movies" / "Ghost (2000)" / "ghost.mkv"

    out = pd.quarantine_files({"id": 9, "file": [str(ghost)]}, title="Ghost", library_name="Movies")
    assert out["moved"] == []
    assert len(out["errors"]) == 1
    err = out["errors"][0]
    assert err["exception_type"] == "FileNotFoundError"
    assert err["source_exists"] is False
    # A missing source is classified as PATH_NOT_FOUND in the failure summary.
    assert report["failure_summary"]["PATH_NOT_FOUND"] == 1


def test_quarantine_files_collision_suffixes_library(cfg, report, tmp_path):
    qdir = tmp_path / "quarantine"
    qdir.mkdir()
    cfg["QUARANTINE_DIR"] = str(qdir)
    # Pre-create the bare logical destination so collision-pass-1 fires.
    existing = qdir / "Dune (2021)" / "Dune.2021.mkv"
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"old")
    src = _make_source(tmp_path)

    out = pd.quarantine_files({"id": 5, "file": [str(src)]}, title="Dune", library_name="Movies")
    assert out["errors"] == []
    dest = out["moved"][0]
    # Top-level folder is suffixed with the upper-cased, sanitised library name.
    assert "Dune (2021)__MOVIES" in dest
    assert os.path.isfile(dest)
    # The pre-existing file is untouched.
    assert existing.read_bytes() == b"old"


# --------------------------------------------------------------------------- #
# _prune_empty_dirs
# --------------------------------------------------------------------------- #


def test_prune_empty_dirs_removes_up_to_stop(tmp_path):
    stop = tmp_path / "q"
    deep = stop / "Show" / "Season 01"
    deep.mkdir(parents=True)
    pd._prune_empty_dirs(str(deep), str(stop))
    # Empty Season + Show folders pruned; the stop dir itself is preserved.
    assert not (stop / "Show").exists()
    assert stop.is_dir()


def test_prune_empty_dirs_stops_at_nonempty(tmp_path):
    stop = tmp_path / "q"
    deep = stop / "Show" / "Season 01"
    deep.mkdir(parents=True)
    keep = stop / "Show" / "keep.txt"
    keep.write_text("x", encoding="utf-8")
    pd._prune_empty_dirs(str(deep), str(stop))
    # Season 01 empty -> pruned; Show is non-empty (keep.txt) -> preserved.
    assert not deep.exists()
    assert (stop / "Show").is_dir()


# --------------------------------------------------------------------------- #
# purge_quarantine  (fake quarantine tree in tmp_path)
# --------------------------------------------------------------------------- #


def _quarantine_entry(qdir, rel, *, age_days, size=1000, ts="auto"):
    """Create a fake quarantined file + sidecar; return its file path."""
    qfile = qdir / rel
    qfile.parent.mkdir(parents=True, exist_ok=True)
    qfile.write_bytes(b"q" * size)
    if ts == "auto":
        ts = (datetime.now(UTC) - timedelta(days=age_days)).isoformat()
    meta = {
        "quarantine_path": str(qfile),
        "quarantine_timestamp": ts,
        "original_size": size,
    }
    with open(str(qfile) + ".dupefinder_meta.json", "w", encoding="utf-8") as fp:
        json.dump(meta, fp)
    return qfile


def test_purge_quarantine_disabled(cfg):
    cfg["AUTO_PURGE_QUARANTINE"] = False
    result = pd.purge_quarantine(simulate=True)
    assert result["enabled"] is False
    assert result["candidates"] == 0


def test_purge_quarantine_no_dir_short_circuits(cfg, tmp_path):
    cfg["AUTO_PURGE_QUARANTINE"] = True
    cfg["QUARANTINE_DIR"] = str(tmp_path / "missing")
    result = pd.purge_quarantine(simulate=False)
    assert result["enabled"] is True
    assert result["candidates"] == 0


def test_purge_quarantine_zero_retention_short_circuits(cfg, tmp_path):
    qdir = tmp_path / "q"
    qdir.mkdir()
    cfg["AUTO_PURGE_QUARANTINE"] = True
    cfg["QUARANTINE_DIR"] = str(qdir)
    cfg["QUARANTINE_RETENTION_DAYS"] = 0
    result = pd.purge_quarantine(simulate=False)
    assert result["candidates"] == 0


def test_purge_quarantine_simulate_does_not_delete(cfg, tmp_path):
    qdir = tmp_path / "q"
    qdir.mkdir()
    cfg["AUTO_PURGE_QUARANTINE"] = True
    cfg["QUARANTINE_DIR"] = str(qdir)
    cfg["QUARANTINE_RETENTION_DAYS"] = 7
    old = _quarantine_entry(qdir, "Show/old.mkv", age_days=30, size=2000)
    young = _quarantine_entry(qdir, "Show/new.mkv", age_days=1, size=1000)

    result = pd.purge_quarantine(simulate=True)
    assert result["simulated"] is True
    assert result["candidates"] == 1  # only the 30-day-old one
    assert result["removed_files"] == 1
    assert result["reclaimed_bytes"] == 2000
    # Nothing actually deleted in a simulation.
    assert old.exists()
    assert young.exists()
    assert os.path.isfile(str(old) + ".dupefinder_meta.json")


def test_purge_quarantine_real_removes_expired_only(cfg, report, tmp_path):
    qdir = tmp_path / "q"
    qdir.mkdir()
    cfg["AUTO_PURGE_QUARANTINE"] = True
    cfg["QUARANTINE_DIR"] = str(qdir)
    cfg["QUARANTINE_RETENTION_DAYS"] = 7
    old = _quarantine_entry(qdir, "Show/Season 01/old.mkv", age_days=30, size=2000)
    young = _quarantine_entry(qdir, "Other/new.mkv", age_days=2, size=1000)

    result = pd.purge_quarantine(simulate=False)
    assert result["simulated"] is False
    assert result["removed_files"] == 1
    assert result["reclaimed_bytes"] == 2000
    assert result["errors"] == 0
    # Expired file AND its sidecar removed; empty parent dirs pruned.
    assert not old.exists()
    assert not os.path.exists(str(old) + ".dupefinder_meta.json")
    assert not (qdir / "Show").exists()  # empty tree pruned bottom-up
    # The young entry is untouched.
    assert young.exists()
    assert os.path.isfile(str(young) + ".dupefinder_meta.json")


def test_purge_quarantine_skips_unreadable_and_undated_sidecars(cfg, tmp_path):
    qdir = tmp_path / "q"
    qdir.mkdir()
    cfg["AUTO_PURGE_QUARANTINE"] = True
    cfg["QUARANTINE_DIR"] = str(qdir)
    cfg["QUARANTINE_RETENTION_DAYS"] = 7
    # Corrupt sidecar -> skipped, not crash.
    with open(qdir / "bad.dupefinder_meta.json", "w", encoding="utf-8") as fp:
        fp.write("{not json")
    # Sidecar with an unparseable timestamp -> skipped.
    _quarantine_entry(qdir, "undated.mkv", age_days=99, ts="not-a-date")

    result = pd.purge_quarantine(simulate=False)
    assert result["candidates"] == 0
    assert result["removed_files"] == 0


# --------------------------------------------------------------------------- #
# validate_libraries  (fail-fast SystemExit on a typo'd name)
# --------------------------------------------------------------------------- #


class _Section:
    def __init__(self, title):
        self.title = title


class _LibrarySections:
    def __init__(self, titles, raise_on_list=False):
        self._titles = titles
        self._raise = raise_on_list

    def sections(self):
        if self._raise:
            raise RuntimeError("api down")
        return [_Section(t) for t in self._titles]


class _PlexServer:
    def __init__(self, titles, raise_on_list=False):
        self.library = _LibrarySections(titles, raise_on_list)


def test_validate_libraries_passes_when_all_present(cfg):
    cfg["PLEX_LIBRARIES"] = ["Movies", "TV"]
    # No exception, no exit when every configured name exists on the server.
    pd.validate_libraries(_PlexServer(["Movies", "TV", "Music"]))


def test_validate_libraries_exits_on_missing(cfg, capsys):
    cfg["PLEX_LIBRARIES"] = ["Movies", "Typo"]
    with pytest.raises(SystemExit) as exc:
        pd.validate_libraries(_PlexServer(["Movies", "TV"]))
    assert exc.value.code == 2
    out = capsys.readouterr().out
    assert "not found on Plex" in out
    assert "'Typo'" in out


def test_validate_libraries_exits_when_listing_fails(cfg):
    cfg["PLEX_LIBRARIES"] = ["Movies"]
    with pytest.raises(SystemExit) as exc:
        pd.validate_libraries(_PlexServer([], raise_on_list=True))
    assert exc.value.code == 1


# --------------------------------------------------------------------------- #
# validate_config  (fail-fast on unsafe config; all paths inside tmp_path)
# --------------------------------------------------------------------------- #


@pytest.fixture
def safe_config(cfg, tmp_path, monkeypatch):
    """A minimally-valid config plus a writable plans dir, both in tmp_path."""
    cfg.update(
        PLEX_SERVER="http://plex:32400",
        PLEX_TOKEN="TOK",
        PLEX_LIBRARIES=["Movies"],
        DRY_RUN=True,
        AUDIT_MODE=False,
        QUARANTINE_MODE=True,
        QUARANTINE_DIR="",
        RADARR_RESCAN_AFTER=False,
        SONARR_RESCAN_AFTER=False,
        JSON_REPORT_DIR="",
    )
    monkeypatch.setattr(pd, "default_plans_dir", str(tmp_path / "plans"))
    return cfg


def test_validate_config_passes_in_dry_run(safe_config):
    # DRY_RUN needs no quarantine dir; should not exit.
    pd.validate_config()


def test_validate_config_exits_on_missing_credentials(safe_config):
    safe_config["PLEX_SERVER"] = ""
    safe_config["PLEX_TOKEN"] = ""
    safe_config["PLEX_LIBRARIES"] = []
    with pytest.raises(SystemExit) as exc:
        pd.validate_config()
    assert exc.value.code == 2


def test_validate_config_requires_quarantine_dir_when_live(safe_config):
    # Live + quarantine mode but no QUARANTINE_DIR -> abort.
    safe_config["DRY_RUN"] = False
    safe_config["QUARANTINE_MODE"] = True
    safe_config["QUARANTINE_DIR"] = ""
    with pytest.raises(SystemExit) as exc:
        pd.validate_config()
    assert exc.value.code == 2


def test_validate_config_creates_quarantine_dir_when_live(safe_config, tmp_path, capsys):
    qdir = tmp_path / "new_quarantine"
    safe_config["DRY_RUN"] = False
    safe_config["QUARANTINE_MODE"] = True
    safe_config["QUARANTINE_DIR"] = str(qdir)
    pd.validate_config()  # must not exit
    assert qdir.is_dir()  # created on demand
    assert "Created QUARANTINE_DIR" in capsys.readouterr().out


def test_validate_config_audit_mode_skips_quarantine_checks(safe_config):
    # AUDIT_MODE behaves like DRY_RUN — no quarantine dir required even when live.
    safe_config["DRY_RUN"] = False
    safe_config["AUDIT_MODE"] = True
    safe_config["QUARANTINE_MODE"] = True
    safe_config["QUARANTINE_DIR"] = ""
    pd.validate_config()


def test_validate_config_exits_on_arr_misconfig(safe_config):
    safe_config["RADARR_RESCAN_AFTER"] = True
    safe_config["RADARR_URL"] = ""
    safe_config["RADARR_API_KEY"] = ""
    with pytest.raises(SystemExit) as exc:
        pd.validate_config()
    assert exc.value.code == 2


def test_validate_config_creates_json_report_dir(safe_config, tmp_path, capsys):
    jdir = tmp_path / "reports"
    safe_config["JSON_REPORT_DIR"] = str(jdir)
    pd.validate_config()
    assert jdir.is_dir()
    assert "Created JSON_REPORT_DIR" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# _manual_choose_keeper  (input() monkeypatched — no real stdin)
# --------------------------------------------------------------------------- #


def _manual_part(media_id, score, file, exists=True):
    """A scored-part dict complete enough for build_tabulated to render."""
    return {
        "id": media_id,
        "score": score,
        "exists": exists,
        "file": [file],
        "file_size": 10**9,
        "video_duration": 3_600_000,
        "video_bitrate": 5000,
        "video_resolution": "1080",
        "video_width": 1920,
        "video_height": 1080,
        "video_codec": "hevc",
        "audio_codec": "eac3",
        "audio_channels": 6,
        "has_hdr": False,
        "has_dv": False,
    }


def _manual_parts():
    return {
        10: _manual_part(10, 5000, "/a.mkv"),
        20: _manual_part(20, 4000, "/b.mkv"),
    }


def _patch_input(monkeypatch, answers):
    """Feed scripted answers to builtins.input in order."""
    queue = list(answers)
    monkeypatch.setattr("builtins.input", lambda *a, **k: queue.pop(0))


def test_manual_choose_keeper_skip(cfg, monkeypatch):
    cfg["FIND_DUPLICATE_FILEPATHS_ONLY"] = False
    _patch_input(monkeypatch, ["s"])
    keeper, skipped = pd._manual_choose_keeper("T", _manual_parts(), 10, "best")
    assert keeper is None
    assert skipped is True


def test_manual_choose_keeper_recommended(cfg, monkeypatch):
    cfg["FIND_DUPLICATE_FILEPATHS_ONLY"] = False
    _patch_input(monkeypatch, ["b"])
    keeper, skipped = pd._manual_choose_keeper("T", _manual_parts(), 10, "best")
    assert keeper == 10
    assert skipped is False


def test_manual_choose_keeper_numeric_row(cfg, monkeypatch):
    cfg["FIND_DUPLICATE_FILEPATHS_ONLY"] = False
    # Rows are sorted by score desc -> row 1 == id 10, row 2 == id 20.
    _patch_input(monkeypatch, ["2"])
    keeper, skipped = pd._manual_choose_keeper("T", _manual_parts(), 10, "best")
    assert keeper == 20
    assert skipped is False


def test_manual_choose_keeper_invalid_input_skips(cfg, monkeypatch):
    cfg["FIND_DUPLICATE_FILEPATHS_ONLY"] = False
    _patch_input(monkeypatch, ["nonsense"])
    keeper, skipped = pd._manual_choose_keeper("T", _manual_parts(), 10, "best")
    assert keeper is None
    assert skipped is True


def test_manual_choose_keeper_missing_file_requires_confirmation(cfg, monkeypatch):
    cfg["FIND_DUPLICATE_FILEPATHS_ONLY"] = False
    parts = _manual_parts()
    parts[20]["exists"] = False  # choosing a missing file is dangerous
    # Row 2 (id 20) chosen, but confirmation is NOT 'YES' -> safety skip.
    _patch_input(monkeypatch, ["2", "no"])
    keeper, skipped = pd._manual_choose_keeper("T", parts, 10, "best")
    assert keeper is None
    assert skipped is True


def test_manual_choose_keeper_missing_file_confirmed_yes(cfg, monkeypatch):
    cfg["FIND_DUPLICATE_FILEPATHS_ONLY"] = False
    parts = _manual_parts()
    parts[20]["exists"] = False
    _patch_input(monkeypatch, ["2", "YES"])
    keeper, skipped = pd._manual_choose_keeper("T", parts, 10, "best")
    assert keeper == 20
    assert skipped is False


# --------------------------------------------------------------------------- #
# diagnose_paths  (get_dupes monkeypatched — no real Plex)
# --------------------------------------------------------------------------- #


class _DiagPart:
    def __init__(self, file):
        self.file = file


class _DiagMedia:
    def __init__(self, parts):
        self.parts = parts


class _DiagItem:
    def __init__(self, media):
        self.media = media


def test_diagnose_paths_reports_empty_mappings(cfg, monkeypatch, capsys):
    cfg["PATH_MAPPINGS"] = {}
    cfg["PLEX_LIBRARIES"] = []
    monkeypatch.setattr(pd, "get_dupes", lambda section: [])
    pd.diagnose_paths()
    out = capsys.readouterr().out
    assert "LOADED is EMPTY" in out
    assert "SUMMARY: 0/0" in out


def test_diagnose_paths_samples_parts_and_resolves(cfg, monkeypatch, tmp_path, capsys):
    real = tmp_path / "movie.mkv"
    real.write_bytes(b"x")
    cfg["PATH_MAPPINGS"] = {"/movies/": str(tmp_path) + "/"}
    cfg["PLEX_LIBRARIES"] = ["Movies"]
    item = _DiagItem([_DiagMedia([_DiagPart("/movies/movie.mkv")])])
    monkeypatch.setattr(pd, "get_dupes", lambda section: [item])
    pd.diagnose_paths(per_section=8)
    out = capsys.readouterr().out
    assert "MATCHED PREFIX: /movies/" in out
    assert "EXISTS        : True" in out
    assert "SUMMARY: 1/1" in out


def test_diagnose_paths_handles_get_dupes_error(cfg, monkeypatch, capsys):
    cfg["PATH_MAPPINGS"] = {"/m/": "/n/"}
    cfg["PLEX_LIBRARIES"] = ["Broken"]

    def boom(section):
        raise RuntimeError("plex offline")

    monkeypatch.setattr(pd, "get_dupes", boom)
    pd.diagnose_paths()
    out = capsys.readouterr().out
    assert "could not fetch duplicates" in out
