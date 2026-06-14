"""Characterization tests for the legacy engine's stream / media-info helpers.

CTO-02 raises the coverage floor on ``plex_dupefinder.py`` (the only component
that moves/deletes media). These tests pin the CURRENT behaviour of pure /
fakeable helpers that were previously uncovered, using tiny fake objects with
just the attributes the code reads — no real Plex, network or destructive FS.

Functions exercised here:
  * _collect_media_streams()          — single-pass audio/sub/video aggregation
  * _max_audio_channels()             — richest single track (MAX, not SUM)
  * get_media_info()                  — attribute mapping + PATH_MAPPINGS resolve
  * build_tabulated()                 — header/row shaping (cfg-driven)
  * millis_to_string / bytes_to_string / kbps_to_string — display formatters
  * should_skip()                     — SKIP_LIST substring match
  * _item_metadata_sane()            — PASS-0 completeness gate
  * _snapshot_media_metadata / _snapshot_diff — drift fingerprint + diff
  * compute_lowest_confidence_groups()— riskiest-first ranking by score_delta
  * _files_confirmed_absent()         — confirmed-gone vs unreachable distinction
  * _item_title()                     — display title for episode/movie/other
  * has_sane_metadata()               — per-part decision-readiness gate
  * is_files_stable()                 — size-stability check across a brief wait
  * get_file_age_hours()              — mtime age (None when not readable)
  * compute_partial_hashes()          — head/tail/size fingerprint
  * audit_scoring_config()            — FILENAME_SCORES double-counting detector
  * _write_quarantine_sidecar()       — self-contained restore metadata sidecar
  * summarize_quarantine()            — read-only quarantine inventory
  * write_decision()                  — human-readable decisions.log line
  * write_json_report()               — machine report (gated on JSON_REPORT_DIR)
  * write_plan_file()                 — Pass-1 discovery snapshot

Run with:  pytest tests/regression/test_engine_streams.py -q
"""

import copy
import hashlib
import json
import os
from datetime import UTC, datetime, timedelta

import plex_dupefinder as pd
import pytest

# --------------------------------------------------------------------------- #
# fakes — minimal objects exposing only the attributes the engine reads
# --------------------------------------------------------------------------- #


class FakeStream:
    """An audio/subtitle/video stream; only set what a test needs."""

    def __init__(self, **attrs):
        for key, value in attrs.items():
            setattr(self, key, value)


class FakePart:
    def __init__(
        self,
        file="/m/x.mkv",
        size=1000,
        audio=None,
        subs=None,
        video=None,
        exists=None,
        accessible=None,
        raise_audio=False,
        raise_subs=False,
        raise_video=False,
    ):
        self.file = file
        self.size = size
        self.exists = exists
        self.accessible = accessible
        self._audio = audio or []
        self._subs = subs or []
        self._video = video or []
        self._raise_audio = raise_audio
        self._raise_subs = raise_subs
        self._raise_video = raise_video

    def audioStreams(self):
        if self._raise_audio:
            raise RuntimeError("boom")
        return self._audio

    def subtitleStreams(self):
        if self._raise_subs:
            raise RuntimeError("boom")
        return self._subs

    def videoStreams(self):
        if self._raise_video:
            raise RuntimeError("boom")
        return self._video


class FakeMedia:
    """A plexapi Media: a list of parts plus the flat attrs get_media_info reads."""

    def __init__(self, parts, **attrs):
        self.parts = parts
        for key, value in attrs.items():
            setattr(self, key, value)


class FakeItem:
    """A plexapi item carrying a ``type`` (and whatever _item_title reads)."""

    def __init__(self, **attrs):
        for key, value in attrs.items():
            setattr(self, key, value)


def make_media(file="/m/a.mkv", **attrs):
    """A full Media with one part and complete flat metadata for get_media_info."""
    defaults = dict(
        id=42,
        bitrate=5000,
        videoCodec="hevc",
        videoResolution="1080",
        height=1080,
        width=1920,
        duration=3_600_000,
        audioCodec="eac3",
    )
    defaults.update(attrs)
    part = FakePart(file=file, size=2000, audio=[FakeStream(channels=6)])
    return FakeMedia([part], **defaults)


def part_row(**kw):
    """A scored-part dict in the shape build_tabulated consumes."""
    base = dict(
        id=1,
        score=1000,
        exists=True,
        file=["/m/a.mkv"],
        file_size=10**9,
        video_duration=3_600_000,
        video_bitrate=5000,
        video_resolution="1080",
        video_width=1920,
        video_height=1080,
        video_codec="hevc",
        audio_codec="eac3",
        audio_channels=6,
        has_hdr=False,
        has_dv=False,
    )
    base.update(kw)
    return base


@pytest.fixture
def cfg(monkeypatch):
    """Fresh, deterministic config per test, restored automatically by monkeypatch."""
    base = copy.deepcopy(pd.cfg)
    base.update(
        PATH_MAPPINGS={},
        PARTIAL_HASH_ENABLED=False,
        FIND_DUPLICATE_FILEPATHS_ONLY=False,
    )
    monkeypatch.setattr(pd, "cfg", base)
    return base


# --------------------------------------------------------------------------- #
# _collect_media_streams
# --------------------------------------------------------------------------- #


def test_collect_media_streams_aggregates_all_signals():
    part = FakePart(
        audio=[FakeStream(channels=8), FakeStream(channels=2)],
        subs=[FakeStream(), FakeStream()],
        video=[FakeStream(colorTrc="smpte2084"), FakeStream(DOVIPresent=True)],
    )
    out = pd._collect_media_streams(FakeItem(parts=[part]))
    assert out == {
        "max_audio_channels": 8,  # richest track, not 8+2
        "audio_track_count": 2,
        "subtitle_count": 2,
        "has_hdr": True,  # smpte2084
        "has_dv": True,  # DOVIPresent
    }


def test_collect_media_streams_arib_hlg_counts_as_hdr():
    part = FakePart(video=[FakeStream(colorTrc="ARIB-STD-B67")])  # case-insensitive
    assert pd._collect_media_streams(FakeItem(parts=[part]))["has_hdr"] is True


def test_collect_media_streams_sums_tracks_across_parts():
    p1 = FakePart(audio=[FakeStream(channels=6)], subs=[FakeStream()])
    p2 = FakePart(audio=[FakeStream(channels=2), FakeStream(channels=8)])
    out = pd._collect_media_streams(FakeItem(parts=[p1, p2]))
    assert out["audio_track_count"] == 3
    assert out["subtitle_count"] == 1
    assert out["max_audio_channels"] == 8


def test_collect_media_streams_plain_sdr_has_no_flags():
    part = FakePart(audio=[FakeStream(channels=2)], video=[FakeStream(colorTrc="bt709")])
    out = pd._collect_media_streams(FakeItem(parts=[part]))
    assert out["has_hdr"] is False
    assert out["has_dv"] is False


def test_collect_media_streams_swallows_stream_errors():
    # A part whose audioStreams()/subtitleStreams()/videoStreams() all raise must
    # not crash collection — it just contributes nothing.
    bad = FakePart(raise_audio=True, raise_subs=True, raise_video=True)
    out = pd._collect_media_streams(FakeItem(parts=[bad]))
    assert out == {
        "max_audio_channels": 0,
        "audio_track_count": 0,
        "subtitle_count": 0,
        "has_hdr": False,
        "has_dv": False,
    }


def test_collect_media_streams_missing_channels_attr_defaults_zero():
    # A stream with no ``channels`` attribute counts as 0 channels.
    part = FakePart(audio=[FakeStream(), FakeStream(channels=6)])
    out = pd._collect_media_streams(FakeItem(parts=[part]))
    assert out["max_audio_channels"] == 6
    assert out["audio_track_count"] == 2


# --------------------------------------------------------------------------- #
# _max_audio_channels
# --------------------------------------------------------------------------- #


def test_max_audio_channels_picks_richest_track():
    item = FakeItem(parts=[FakePart(audio=[FakeStream(channels=6), FakeStream(channels=8)])])
    assert pd._max_audio_channels(item) == 8


def test_max_audio_channels_spans_parts():
    item = FakeItem(
        parts=[FakePart(audio=[FakeStream(channels=2)]), FakePart(audio=[FakeStream(channels=6)])]
    )
    assert pd._max_audio_channels(item) == 6


def test_max_audio_channels_zero_when_no_audio():
    assert pd._max_audio_channels(FakeItem(parts=[FakePart(audio=[])])) == 0


# --------------------------------------------------------------------------- #
# get_media_info
# --------------------------------------------------------------------------- #


def test_get_media_info_maps_attributes(cfg):
    info = pd.get_media_info(make_media())
    assert info["id"] == 42
    assert info["video_bitrate"] == 5000
    assert info["video_codec"] == "hevc"
    assert info["video_resolution"] == "1080"
    assert info["video_width"] == 1920
    assert info["video_height"] == 1080
    assert info["video_duration"] == 3_600_000
    assert info["audio_codec"] == "eac3"
    assert info["audio_channels"] == 6  # richest track from streams
    assert info["file_size"] == 2000
    assert info["multipart"] is False


def test_get_media_info_resolves_path_via_mappings(cfg):
    cfg["PATH_MAPPINGS"] = {"/tv/": "/mnt/real/"}
    info = pd.get_media_info(make_media(file="/tv/Show/ep.mkv"))
    assert info["file"] == ["/mnt/real/Show/ep.mkv"]
    # The original Plex logical path is recorded on the existence record.
    assert info["parts_existence"][0]["plex_path"] == "/tv/Show/ep.mkv"


def test_get_media_info_passthrough_without_mapping(cfg):
    info = pd.get_media_info(make_media(file="/m/clip.mkv"))
    assert info["file"] == ["/m/clip.mkv"]
    # No mapping applied -> plex_path stays None (no rewrite happened).
    assert info["parts_existence"][0]["plex_path"] is None


def test_get_media_info_propagates_stream_flags(cfg):
    part = FakePart(
        file="/m/a.mkv",
        size=10,
        audio=[FakeStream(channels=8), FakeStream(channels=2)],
        subs=[FakeStream()],
        video=[FakeStream(colorTrc="smpte2084", DOVIPresent=True)],
    )
    media = FakeMedia(
        [part],
        id=1,
        bitrate=1,
        videoCodec="hevc",
        videoResolution="4k",
        height=2160,
        width=3840,
        duration=10,
        audioCodec="truehd",
    )
    info = pd.get_media_info(media)
    assert info["has_hdr"] is True
    assert info["has_dv"] is True
    assert info["audio_track_count"] == 2
    assert info["subtitle_count"] == 1
    assert info["audio_channels"] == 8


def test_get_media_info_multipart_and_size_sum(cfg):
    p1 = FakePart(file="/m/cd1.mkv", size=1000, audio=[FakeStream(channels=6)])
    p2 = FakePart(file="/m/cd2.mkv", size=2500, audio=[FakeStream(channels=6)])
    media = FakeMedia(
        [p1, p2],
        id=7,
        bitrate=4000,
        videoCodec="h264",
        videoResolution="720",
        height=720,
        width=1280,
        duration=20,
        audioCodec="ac3",
    )
    info = pd.get_media_info(media)
    assert info["multipart"] is True
    assert info["file"] == ["/m/cd1.mkv", "/m/cd2.mkv"]
    assert info["file_size"] == 3500


def test_get_media_info_always_reports_existence(cfg):
    info = pd.get_media_info(make_media(file="/no_such_dir_zzz/movie.mkv"))
    assert "exists" in info
    assert len(info["parts_existence"]) == 1


# --------------------------------------------------------------------------- #
# build_tabulated
# --------------------------------------------------------------------------- #


def test_build_tabulated_headers_and_row_shape(cfg):
    parts = {1: part_row(id=1, score=1000)}
    headers, data = pd.build_tabulated(parts, {1: 1})
    assert headers == [
        "choice",
        "score",
        "exists",
        "id",
        "file",
        "size",
        "duration",
        "bitrate",
        "resolution",
        "codecs",
    ]
    assert len(data) == 1
    assert len(data[0]) == len(headers)


def test_build_tabulated_formats_cells(cfg):
    parts = {1: part_row(id=1, score=1000)}
    _, data = pd.build_tabulated(parts, {1: 1})
    row = data[0]
    assert row[0] == 1  # choice
    assert row[1] == "1,000"  # score, comma-grouped
    assert row[2] == "Yes"  # exists
    assert row[5] == "953.7 MB"  # 1e9 bytes
    assert row[6] == "01:00:00"  # duration
    assert row[7] == "4.88 Mbps"  # bitrate
    assert row[8] == "1080 (1920 x 1080)"  # resolution
    assert row[9] == "hevc, eac3 x 6"  # codecs (no HDR/DV tags)


def test_build_tabulated_marks_missing_and_hdr_dv_tags(cfg):
    parts = {2: part_row(id=2, score=5000, exists=False, has_hdr=True, has_dv=True)}
    _, data = pd.build_tabulated(parts, {2: 2})
    row = data[0]
    assert row[2] == "MISSING"
    assert row[9] == "hevc, eac3 x 6 [DV/HDR]"


def test_build_tabulated_drops_score_in_filepaths_only_mode(cfg):
    cfg["FIND_DUPLICATE_FILEPATHS_ONLY"] = True
    parts = {1: part_row(id=1, score=1000)}
    headers, data = pd.build_tabulated(parts, {1: 1})
    assert "score" not in headers
    assert len(data[0]) == len(headers)


# --------------------------------------------------------------------------- #
# display formatters
# --------------------------------------------------------------------------- #


def test_millis_to_string():
    assert pd.millis_to_string(0) == "00:00:00"
    assert pd.millis_to_string(3_661_000) == "01:01:01"
    assert pd.millis_to_string(5_400_000) == "01:30:00"


def test_bytes_to_string():
    assert pd.bytes_to_string(1) == "1 byte"
    assert pd.bytes_to_string(0) == "0 bytes"
    assert pd.bytes_to_string(500) == "500 bytes"
    assert pd.bytes_to_string(1536) == "1 KB"  # KB precision is 0
    assert pd.bytes_to_string(1_073_741_824) == "1.0 GB"
    assert pd.bytes_to_string(int(1.5 * 1_073_741_824)) == "1.5 GB"


def test_kbps_to_string():
    assert pd.kbps_to_string(500) == "500 Kbps"
    assert pd.kbps_to_string(2048) == "2.00 Mbps"


# --------------------------------------------------------------------------- #
# should_skip
# --------------------------------------------------------------------------- #


def test_should_skip_matches_substring(cfg):
    cfg["SKIP_LIST"] = ["sample", "/trailers/"]
    assert pd.should_skip(["/m/Movie.sample.mkv"]) is True
    assert pd.should_skip(["/m/Movie/trailers/teaser.mkv"]) is True
    assert pd.should_skip(["/m/Movie.mkv"]) is False


def test_should_skip_empty_list_never_skips(cfg):
    cfg["SKIP_LIST"] = []
    assert pd.should_skip(["/m/anything.sample.mkv"]) is False


# --------------------------------------------------------------------------- #
# _item_metadata_sane
# --------------------------------------------------------------------------- #


def test_item_metadata_sane_accepts_complete_item():
    item = FakeItem(
        media=[
            FakeMedia(
                [FakePart(file="/m/a.mkv")], bitrate=5000, duration=3_600_000, videoCodec="hevc"
            )
        ]
    )
    assert pd._item_metadata_sane(item) is True


def test_item_metadata_sane_rejects_no_media():
    assert pd._item_metadata_sane(FakeItem(media=[])) is False


def test_item_metadata_sane_rejects_no_parts():
    item = FakeItem(media=[FakeMedia([], bitrate=5000, duration=10, videoCodec="hevc")])
    assert pd._item_metadata_sane(item) is False


@pytest.mark.parametrize(
    "overrides",
    [
        dict(bitrate=0),
        dict(duration=0),
        dict(videoCodec=""),
        dict(videoCodec="unknown"),
    ],
)
def test_item_metadata_sane_rejects_incomplete_metadata(overrides):
    attrs = dict(bitrate=5000, duration=3_600_000, videoCodec="hevc")
    attrs.update(overrides)
    item = FakeItem(media=[FakeMedia([FakePart(file="/m/a.mkv")], **attrs)])
    assert pd._item_metadata_sane(item) is False


def test_item_metadata_sane_rejects_empty_file_path():
    item = FakeItem(
        media=[FakeMedia([FakePart(file="")], bitrate=5000, duration=10, videoCodec="hevc")]
    )
    assert pd._item_metadata_sane(item) is False


# --------------------------------------------------------------------------- #
# _snapshot_media_metadata + _snapshot_diff
# --------------------------------------------------------------------------- #


def test_snapshot_media_metadata_captures_fingerprint():
    media = FakeMedia(
        [FakePart(file="/m/a.mkv", size=10)],
        id=1,
        bitrate=5000,
        duration=10,
        videoCodec="hevc",
        audioCodec="eac3",
        videoResolution="1080",
        width=1920,
        height=1080,
        audioChannels=6,
    )
    item = FakeItem(media=[media], updatedAt="ts")
    snap = pd._snapshot_media_metadata(item)
    assert snap["updatedAt"] == "ts"
    assert len(snap["media"]) == 1
    m = snap["media"][0]
    assert m["bitrate"] == 5000
    assert m["videoCodec"] == "hevc"
    assert m["parts"] == [{"id": None, "size": 10, "file": "/m/a.mkv"}]


def test_snapshot_diff_clean_when_identical():
    snap = {
        "updatedAt": "1",
        "media": [{"id": 1, "bitrate": 5000, "parts": [{"id": 1, "size": 10, "file": "/a"}]}],
    }
    assert pd._snapshot_diff(snap, copy.deepcopy(snap)) == []


def test_snapshot_diff_flags_media_field_change():
    before = {
        "updatedAt": "1",
        "media": [{"id": 1, "bitrate": 5000, "parts": [{"id": 1, "size": 10, "file": "/a"}]}],
    }
    after = copy.deepcopy(before)
    after["media"][0]["bitrate"] = 6000
    assert pd._snapshot_diff(before, after) == ["media[0].bitrate"]


def test_snapshot_diff_flags_updated_at_and_counts():
    before = {
        "updatedAt": "1",
        "media": [{"id": 1, "bitrate": 5000, "parts": [{"id": 1, "size": 10, "file": "/a"}]}],
    }
    bumped = copy.deepcopy(before)
    bumped["updatedAt"] = "2"
    assert pd._snapshot_diff(before, bumped) == ["updatedAt"]

    fewer_media = copy.deepcopy(before)
    fewer_media["media"] = []
    assert pd._snapshot_diff(before, fewer_media) == ["media_count(1->0)"]

    fewer_parts = copy.deepcopy(before)
    fewer_parts["media"][0]["parts"] = []
    assert pd._snapshot_diff(before, fewer_parts) == ["media[0].parts_count(1->0)"]


def test_snapshot_diff_flags_part_field_change():
    before = {
        "updatedAt": "1",
        "media": [{"id": 1, "parts": [{"id": 1, "size": 10, "file": "/a"}]}],
    }
    after = copy.deepcopy(before)
    after["media"][0]["parts"][0]["file"] = "/b"
    assert pd._snapshot_diff(before, after) == ["media[0].parts[0].file"]


# --------------------------------------------------------------------------- #
# compute_lowest_confidence_groups
# --------------------------------------------------------------------------- #


def _conf_groups():
    return [
        {
            "title": "A",
            "library": "L",
            "item_key": "k1",
            "decision": {
                "skip": False,
                "score_delta": 500,
                "top_score": 5000,
                "second_score": 4500,
                "keeper_id": 1,
            },
            "parts": {1: {"file": ["/a"]}},
        },
        {
            "title": "B",
            "decision": {"skip": False, "score_delta": 100, "keeper_id": 2},
            "parts": {2: {"file": ["/b"]}},
        },
        {"title": "C", "decision": {"skip": True, "score_delta": 1}},  # skipped
        {"title": "D", "decision": {"skip": False, "score_delta": None}},  # no delta
    ]


def test_lowest_confidence_ranks_smallest_delta_first(cfg):
    ranked = pd.compute_lowest_confidence_groups(_conf_groups(), limit=10)
    assert [r["title"] for r in ranked] == ["B", "A"]  # 100 before 500
    assert ranked[0]["score_delta"] == 100
    assert ranked[0]["keeper_files"] == ["/b"]


def test_lowest_confidence_excludes_skipped_and_no_delta(cfg):
    ranked = pd.compute_lowest_confidence_groups(_conf_groups(), limit=10)
    titles = {r["title"] for r in ranked}
    assert "C" not in titles  # skipped
    assert "D" not in titles  # score_delta is None


def test_lowest_confidence_honours_limit(cfg):
    ranked = pd.compute_lowest_confidence_groups(_conf_groups(), limit=1)
    assert [r["title"] for r in ranked] == ["B"]


def test_lowest_confidence_zero_limit_returns_all_ranked(cfg):
    ranked = pd.compute_lowest_confidence_groups(_conf_groups(), limit=0)
    assert [r["title"] for r in ranked] == ["B", "A"]


# --------------------------------------------------------------------------- #
# _files_confirmed_absent
# --------------------------------------------------------------------------- #


def test_files_confirmed_absent_true_only_when_all_confirmed_gone():
    info = {"parts_existence": [{"local_check": False}, {"local_check": False}]}
    assert pd._files_confirmed_absent(info) is True


def test_files_confirmed_absent_false_when_any_unreachable():
    # local_check None == filesystem unreachable -> must NOT be treated as gone.
    info = {"parts_existence": [{"local_check": False}, {"local_check": None}]}
    assert pd._files_confirmed_absent(info) is False


def test_files_confirmed_absent_false_when_present():
    info = {"parts_existence": [{"local_check": True}]}
    assert pd._files_confirmed_absent(info) is False


def test_files_confirmed_absent_false_when_no_records():
    assert pd._files_confirmed_absent({"parts_existence": []}) is False


# --------------------------------------------------------------------------- #
# _item_title
# --------------------------------------------------------------------------- #


def test_item_title_episode():
    ep = FakeItem(type="episode", grandparentTitle="Show", parentIndex=1, index=5, title="Pilot")
    assert pd._item_title(ep) == "Show - 01x05 - Pilot"


def test_item_title_movie():
    assert pd._item_title(FakeItem(type="movie", title="Dune")) == "Dune"


def test_item_title_other_is_unknown():
    assert pd._item_title(FakeItem(type="artist")) == "Unknown"


# --------------------------------------------------------------------------- #
# has_sane_metadata
# --------------------------------------------------------------------------- #


def test_has_sane_metadata_accepts_complete_part():
    part = dict(file=["/m/a.mkv"], video_duration=10, video_bitrate=5000, video_codec="hevc")
    assert pd.has_sane_metadata(part) == (True, "")


@pytest.mark.parametrize(
    "part, reason",
    [
        (dict(file=[]), "empty file list"),
        (
            dict(file=[""], video_duration=10, video_bitrate=1, video_codec="hevc"),
            "empty file path",
        ),
        (
            dict(file=["/a"], video_duration=0, video_bitrate=1, video_codec="hevc"),
            "video_duration <= 0",
        ),
        (
            dict(file=["/a"], video_duration=10, video_bitrate=0, video_codec="hevc"),
            "video_bitrate <= 0",
        ),
        (
            dict(file=["/a"], video_duration=10, video_bitrate=1, video_codec="Unknown"),
            "video_codec missing or Unknown",
        ),
    ],
)
def test_has_sane_metadata_rejects_incomplete(part, reason):
    ok, why = pd.has_sane_metadata(part)
    assert ok is False
    assert why == reason


# --------------------------------------------------------------------------- #
# is_files_stable
# --------------------------------------------------------------------------- #


def test_is_files_stable_short_circuits_on_zero_wait(tmp_path):
    f = tmp_path / "a.mkv"
    f.write_bytes(b"x" * 100)
    assert pd.is_files_stable([str(f)], 0) == (True, [])


def test_is_files_stable_short_circuits_on_empty_list():
    assert pd.is_files_stable([], 5) == (True, [])


def test_is_files_stable_true_when_size_unchanged(tmp_path, monkeypatch):
    f = tmp_path / "a.mkv"
    f.write_bytes(b"x" * 100)
    monkeypatch.setattr(pd.time, "sleep", lambda _: None)  # no real wait
    assert pd.is_files_stable([str(f)], 1) == (True, [])


def test_is_files_stable_reports_growing_file(tmp_path, monkeypatch):
    f = tmp_path / "growing.mkv"
    f.write_bytes(b"x" * 100)

    def grow(_seconds):
        f.write_bytes(b"x" * 200)  # file grows during the "wait"

    monkeypatch.setattr(pd.time, "sleep", grow)
    stable, changes = pd.is_files_stable([str(f)], 1)
    assert stable is False
    assert changes == [{"file": str(f), "size_before": 100, "size_after": 200}]


def test_is_files_stable_ignores_unreadable_file(tmp_path, monkeypatch):
    # A file we cannot stat is left to the existence layer, not flagged unstable.
    monkeypatch.setattr(pd.time, "sleep", lambda _: None)
    assert pd.is_files_stable([str(tmp_path / "gone.mkv")], 1) == (True, [])


# --------------------------------------------------------------------------- #
# get_file_age_hours
# --------------------------------------------------------------------------- #


def test_get_file_age_hours_none_for_missing_or_empty(tmp_path):
    assert pd.get_file_age_hours(str(tmp_path / "no.mkv")) is None
    assert pd.get_file_age_hours(None) is None
    assert pd.get_file_age_hours("") is None


def test_get_file_age_hours_non_negative_for_real_file(tmp_path):
    f = tmp_path / "a.mkv"
    f.write_bytes(b"x")
    age = pd.get_file_age_hours(str(f))
    assert age is not None
    assert age >= 0


# --------------------------------------------------------------------------- #
# compute_partial_hashes
# --------------------------------------------------------------------------- #


def test_compute_partial_hashes_head_and_tail(tmp_path):
    f = tmp_path / "big.bin"
    f.write_bytes(b"A" * 50)
    out = pd.compute_partial_hashes(str(f), hash_bytes=10)
    assert out["size"] == 50
    assert out["head_sha256"] == hashlib.sha256(b"A" * 10).hexdigest()
    assert out["tail_sha256"] is not None


def test_compute_partial_hashes_small_file_has_no_tail(tmp_path):
    f = tmp_path / "small.bin"
    f.write_bytes(b"B" * 5)
    out = pd.compute_partial_hashes(str(f), hash_bytes=10)
    assert out["size"] == 5
    assert out["head_sha256"] == hashlib.sha256(b"B" * 5).hexdigest()
    assert out["tail_sha256"] is None  # file smaller than hash window


def test_compute_partial_hashes_none_for_missing_or_empty(tmp_path):
    assert pd.compute_partial_hashes("") is None
    assert pd.compute_partial_hashes(str(tmp_path / "none.bin")) is None


# --------------------------------------------------------------------------- #
# audit_scoring_config
# --------------------------------------------------------------------------- #


def test_audit_scoring_config_flags_double_counting(cfg, monkeypatch):
    cfg["FILENAME_SCORES"] = {"*remux*": 500, "*proper*": 500, "*.mkv": 800, "*2160p*": 100}
    monkeypatch.setattr(pd, "run_report", {})
    pd.audit_scoring_config()
    report = pd.run_report["scoring_audit"]
    assert report["ok"] is False
    flagged = {(o["pattern"], o["token"]) for o in report["filename_source_overlaps"]}
    assert flagged == {("*remux*", "remux"), ("*2160p*", "2160p")}


def test_audit_scoring_config_clean_for_edition_tags_only(cfg, monkeypatch):
    cfg["FILENAME_SCORES"] = {"*proper*": 500, "*repack*": 500, "*.mkv": 800}
    monkeypatch.setattr(pd, "run_report", {})
    pd.audit_scoring_config()
    report = pd.run_report["scoring_audit"]
    assert report["ok"] is True
    assert report["filename_source_overlaps"] == []


# --------------------------------------------------------------------------- #
# _write_quarantine_sidecar
# --------------------------------------------------------------------------- #


def _read_sidecar(quarantine_path):
    with open(quarantine_path + ".dupefinder_meta.json", encoding="utf-8") as fp:
        return json.load(fp)


def test_write_quarantine_sidecar_full_metadata(tmp_path):
    qpath = str(tmp_path / "Movie" / "ep.mkv")
    os.makedirs(os.path.dirname(qpath))
    with open(qpath, "wb") as fh:
        fh.write(b"x")
    pd._write_quarantine_sidecar(
        original_path="/orig/Movie/ep.mkv",
        quarantine_path=qpath,
        part_info={"id": 99},
        keeper_info={"file": ["/k/keep.mkv"], "score": 1234, "score_breakdown": {"a": 1}},
        reason="lower score",
        original_size=2048,
        original_mtime=1_700_000_000.0,
        title="Movie",
        library_name="Movies",
        year=2021,
    )
    meta = _read_sidecar(qpath)
    assert meta["media_id"] == 99
    assert meta["original_path"] == "/orig/Movie/ep.mkv"
    assert meta["reason"] == "lower score"
    assert meta["original_size"] == 2048
    assert meta["original_mtime"] == 1_700_000_000.0
    assert meta["keeper"] == {"files": ["/k/keep.mkv"], "score": 1234, "score_breakdown": {"a": 1}}
    assert meta["library"] == "Movies"
    assert meta["title"] == "Movie"
    assert meta["year"] == 2021
    assert "run_id" in meta
    assert "quarantine_timestamp" in meta
    # restore_command moves the quarantined file back to the original path.
    assert meta["restore_command"].startswith("mv ")
    assert "/orig/Movie/ep.mkv" in meta["restore_command"]


def test_write_quarantine_sidecar_omits_optional_and_empty_keeper(tmp_path):
    qpath = str(tmp_path / "q2.mkv")
    pd._write_quarantine_sidecar("/o2", qpath, {"id": 1}, None, "r", 0, 0)
    meta = _read_sidecar(qpath)
    assert meta["keeper"] == {"files": [], "score": None, "score_breakdown": None}
    assert "library" not in meta
    assert "title" not in meta
    assert "year" not in meta


# --------------------------------------------------------------------------- #
# summarize_quarantine  (read-only inventory)
# --------------------------------------------------------------------------- #


def test_summarize_quarantine_disabled_when_no_dir(cfg):
    cfg["QUARANTINE_DIR"] = ""
    summary = pd.summarize_quarantine()
    assert summary["enabled"] is False
    assert summary["file_count"] == 0


def test_summarize_quarantine_missing_dir_reports_empty(cfg, tmp_path):
    cfg["QUARANTINE_DIR"] = str(tmp_path / "nope")
    summary = pd.summarize_quarantine()
    assert summary["enabled"] is True  # configured...
    assert summary["file_count"] == 0  # ...but nothing on disk


def test_summarize_quarantine_aggregates_sidecars(cfg, tmp_path):
    qdir = tmp_path / "q"
    (qdir / "Show").mkdir(parents=True)
    cfg["QUARANTINE_DIR"] = str(qdir)
    cfg["QUARANTINE_RETENTION_DAYS"] = 7
    now = datetime.now(UTC)
    old_ts = (now - timedelta(days=30)).isoformat()
    new_ts = (now - timedelta(days=1)).isoformat()
    for i, (ts, size) in enumerate([(old_ts, 1000), (new_ts, 2000)]):
        sidecar = qdir / "Show" / f"s{i}.mkv.dupefinder_meta.json"
        with open(sidecar, "w", encoding="utf-8") as fh:
            json.dump({"original_size": size, "quarantine_timestamp": ts}, fh)
    # A corrupt sidecar must be counted as unreadable, not crash the walk.
    with open(qdir / "Show" / "bad.dupefinder_meta.json", "w", encoding="utf-8") as fh:
        fh.write("{not json")

    summary = pd.summarize_quarantine()
    assert summary["enabled"] is True
    assert summary["file_count"] == 2
    assert summary["total_bytes"] == 3000
    assert summary["oldest_age_days"] == 30.0
    assert summary["files_over_retention"] == 1  # the 30-day-old one
    assert summary["sidecars_unreadable"] == 1


# --------------------------------------------------------------------------- #
# write_decision
# --------------------------------------------------------------------------- #


def test_write_decision_appends_human_readable_block(tmp_path, monkeypatch):
    dec = tmp_path / "decisions.log"
    monkeypatch.setattr(pd, "decision_filename", str(dec))
    pd.write_decision(title="T", keeping=["/k"], removed=["/r"], note="n")
    text = dec.read_text(encoding="utf-8")
    assert "Title    : T" in text
    assert "Keeping" in text
    assert "Removing" in text
    assert "Note     : n" in text


def test_write_decision_skips_empty_fields(tmp_path, monkeypatch):
    dec = tmp_path / "decisions.log"
    monkeypatch.setattr(pd, "decision_filename", str(dec))
    pd.write_decision(title="OnlyTitle")
    text = dec.read_text(encoding="utf-8")
    assert "Title    : OnlyTitle" in text
    assert "Keeping" not in text
    assert "Removing" not in text


# --------------------------------------------------------------------------- #
# write_json_report
# --------------------------------------------------------------------------- #


def test_write_json_report_none_when_dir_unset(cfg):
    cfg["JSON_REPORT_DIR"] = ""
    assert pd.write_json_report() is None


def test_write_json_report_writes_file(cfg, tmp_path, monkeypatch):
    out = tmp_path / "reports"
    out.mkdir()
    cfg["JSON_REPORT_DIR"] = str(out)
    monkeypatch.setattr(pd, "run_report", {"k": "v"})
    path = pd.write_json_report()
    assert path is not None
    assert os.path.isfile(path)
    with open(path, encoding="utf-8") as fp:
        data = json.load(fp)
    assert data["k"] == "v"
    assert "finished_at" in data  # stamped by write_json_report


# --------------------------------------------------------------------------- #
# write_plan_file
# --------------------------------------------------------------------------- #


def _plan_groups():
    return [
        {
            "title": "M",
            "library": "L",
            "item_key": "k",
            "pass0_status": None,
            "decision": {"skip": False},
            "parts": {1: {"score": 100, "file": ["/a"], "exists": True, "file_size": 10}},
        }
    ]


def test_write_plan_file_none_when_dir_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(pd, "default_plans_dir", str(tmp_path / "nodir"))
    assert pd.write_plan_file(_plan_groups()) is None


def test_write_plan_file_writes_snapshot(tmp_path, monkeypatch):
    plans = tmp_path / "plans"
    plans.mkdir()
    monkeypatch.setattr(pd, "default_plans_dir", str(plans))
    path = pd.write_plan_file(_plan_groups())
    assert path is not None
    assert os.path.isfile(path)
    with open(path, encoding="utf-8") as fp:
        plan = json.load(fp)
    assert "run_id" in plan
    assert len(plan["groups"]) == 1
    assert plan["groups"][0]["parts"][0]["media_id"] == 1
    assert plan["groups"][0]["parts"][0]["files"] == ["/a"]
