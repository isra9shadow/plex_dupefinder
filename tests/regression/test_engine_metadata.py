"""Coverage for the engine's metadata-sanity helpers (CTO-02 ratchet, tests only).

These gate whether a decision runs on trustworthy data, so they sit on the path
to a deletion. Pure / fake-object inputs — no Plex.

Run with:  pytest tests/regression/test_engine_metadata.py -q
"""

from __future__ import annotations

import plex_dupefinder as pd


class _Part:
    def __init__(self, file="/m/x.mkv", id=9, size=10):
        self.file = file
        self.id = id
        self.size = size


class _Media:
    def __init__(self, bitrate=8000, duration=1000, videoCodec="hevc", parts=None):
        self.bitrate = bitrate
        self.duration = duration
        self.videoCodec = videoCodec
        self.parts = [_Part()] if parts is None else parts


class _Item:
    def __init__(self, media=None, updatedAt="2024-01-01"):
        self.media = [_Media()] if media is None else media
        self.updatedAt = updatedAt


# --- has_sane_metadata (part_info dict) --------------------------------------


def _pi(**over):
    base = {
        "file": ["/m/x.mkv"],
        "video_duration": 1000,
        "video_bitrate": 8000,
        "video_codec": "hevc",
    }
    base.update(over)
    return base


def test_has_sane_metadata_ok():
    ok, reason = pd.has_sane_metadata(_pi())
    assert ok is True and reason == ""


def test_has_sane_metadata_rejects():
    assert pd.has_sane_metadata(_pi(file=[]))[0] is False
    assert pd.has_sane_metadata(_pi(file=[""]))[0] is False
    assert pd.has_sane_metadata(_pi(video_duration=0))[0] is False
    assert pd.has_sane_metadata(_pi(video_bitrate=0))[0] is False
    assert pd.has_sane_metadata(_pi(video_codec="Unknown"))[0] is False
    assert pd.has_sane_metadata(_pi(video_codec=""))[0] is False


# --- _files_confirmed_absent (part_info dict) --------------------------------


def test_files_confirmed_absent():
    assert pd._files_confirmed_absent({}) is False  # no existence info -> never "gone"
    assert pd._files_confirmed_absent({"parts_existence": [{"local_check": False}]}) is True
    # A mount outage (None) must NOT be mistaken for "gone".
    assert pd._files_confirmed_absent({"parts_existence": [{"local_check": None}]}) is False
    assert (
        pd._files_confirmed_absent(
            {"parts_existence": [{"local_check": False}, {"local_check": True}]}
        )
        is False
    )


# --- _item_metadata_sane (fake item) -----------------------------------------


def test_item_metadata_sane_true():
    assert pd._item_metadata_sane(_Item()) is True


def test_item_metadata_sane_false():
    assert pd._item_metadata_sane(_Item(media=[])) is False
    assert pd._item_metadata_sane(_Item(media=[_Media(bitrate=0)])) is False
    assert pd._item_metadata_sane(_Item(media=[_Media(duration=0)])) is False
    assert pd._item_metadata_sane(_Item(media=[_Media(videoCodec="")])) is False
    assert pd._item_metadata_sane(_Item(media=[_Media(parts=[])])) is False
    assert pd._item_metadata_sane(_Item(media=[_Media(parts=[_Part(file="")])])) is False


# --- _snapshot_media_metadata (fake item) ------------------------------------


def test_snapshot_media_metadata_shape():
    snap = pd._snapshot_media_metadata(_Item())
    assert snap["updatedAt"] == "2024-01-01"
    assert len(snap["media"]) == 1
    assert snap["media"][0]["bitrate"] == 8000
    assert snap["media"][0]["parts"][0]["file"] == "/m/x.mkv"
