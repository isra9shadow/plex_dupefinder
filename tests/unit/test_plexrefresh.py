"""Tests for modules.ops.plexrefresh (tdarr → Plex targeted false-duplicate fix).

Fully offline: a fake Plex client records analyze/update calls and the
dupefinder report + media tree are built in a tmp dir.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from core.types import SafetyMode
from modules.ops import plexrefresh
from tests.fakes import make_context


def _read_plan(tmp_path: Path) -> dict[str, object]:
    return json.loads(
        (tmp_path / "reports" / "plexrefresh" / "plan.json").read_text(encoding="utf-8")
    )


def _read_summary(tmp_path: Path) -> str:
    return (tmp_path / "reports" / "plexrefresh" / "summary.md").read_text(encoding="utf-8")


# --- fake Plex client + item ---------------------------------------------------


@dataclass
class FakeItem:
    key: str
    type: str
    section: str
    folder: str


@dataclass
class FakePlex:
    """Records analyze/update calls; maps a path to canned items + a status."""

    items_by_path: dict[str, list[FakeItem]] = field(default_factory=dict)
    status: str = "sane_and_changed"
    analyzed: list[str] = field(default_factory=list)
    updated: list[tuple[str, str]] = field(default_factory=list)
    raise_on_analyze: bool = False

    def find_items_by_path(self, path: str) -> list[FakeItem]:
        return self.items_by_path.get(path, [])

    def analyze_item(self, item: FakeItem, timeout_s: float) -> str:
        if self.raise_on_analyze:
            raise RuntimeError("plex unreachable")
        self.analyzed.append(item.key)
        return self.status

    def update_section(self, section: str, folder: str) -> None:
        self.updated.append((section, folder))


def _write_report(directory: Path, groups: list[dict[str, object]]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "dupefinder_report_20260101.json").write_text(
        json.dumps({"groups": groups}), encoding="utf-8"
    )


# --- pure detection ------------------------------------------------------------


def test_is_stale_metadata_reason() -> None:
    assert plexrefresh.is_stale_metadata_reason("video_duration <= 0")
    assert plexrefresh.is_stale_metadata_reason(
        "candidate 5 has invalid metadata: video_codec missing or Unknown "
        "(Plex analysis may be incomplete)"
    )
    assert not plexrefresh.is_stale_metadata_reason("score delta 740 below threshold 1000")
    assert not plexrefresh.is_stale_metadata_reason("cooldown: 'x' is 2h old")


def test_detect_from_report_selects_only_stale_groups() -> None:
    report = {
        "groups": [
            {
                "skip_reason": "video_duration <= 0",
                "candidates": [{"file": "/media/Show/S01E01.mkv"}],
            },
            {
                "revalidation": {"reason": "video_codec missing or Unknown"},
                "candidates": [{"files": ["/media/Movie/movie.mkv"]}],
            },
            {
                # not a stale-metadata reason — must be ignored
                "skip_reason": "score delta 740 below threshold 1000",
                "candidates": [{"file": "/media/Other/x.mkv"}],
            },
        ]
    }
    paths = plexrefresh.detect_from_report(report)
    assert set(paths) == {"/media/Show/S01E01.mkv", "/media/Movie/movie.mkv"}


def test_detect_recent_files_respects_window_and_extension(tmp_path: Path) -> None:
    root = tmp_path / "tdarr_out"
    root.mkdir()
    fresh = root / "new.mkv"
    fresh.write_text("x")
    old = root / "old.mkv"
    old.write_text("x")
    txt = root / "note.txt"
    txt.write_text("x")

    import os

    now = 1_000_000.0
    os.utime(fresh, (now - 3600, now - 3600))  # 1h ago -> within window
    os.utime(old, (now - 90000, now - 90000))  # ~25h ago -> outside 24h
    os.utime(txt, (now, now))  # fresh but not media

    found = plexrefresh.detect_recent_files(
        [str(root)],
        extensions=(".mkv", ".mp4"),
        window_hours=24.0,
        now=now,
    )
    assert found == [str(fresh)]


def test_detect_recent_files_handles_missing_root() -> None:
    assert (
        plexrefresh.detect_recent_files(
            ["/does/not/exist"], extensions=(".mkv",), window_hours=24.0, now=0.0
        )
        == []
    )


# --- dry-run (default) ---------------------------------------------------------


def test_dry_run_lists_candidates_and_never_touches_plex(tmp_path: Path) -> None:
    reports = tmp_path / "dfreports"
    _write_report(
        reports,
        [{"skip_reason": "video_duration <= 0", "candidates": [{"file": "/media/a.mkv"}]}],
    )
    ctx = make_context(
        tmp_path,
        integrations={
            "plexrefresh": {
                "sources": ["dupefinder_report"],
                "dupefinder_reports": str(reports),
            }
        },
    )
    plex = FakePlex()
    result = plexrefresh.run(ctx, client=plex, now=1000.0)

    assert result.actions == 0  # dry-run never acts
    assert plex.analyzed == []  # Plex untouched
    assert plex.updated == []
    plan = _read_plan(tmp_path)
    assert plan["dry_run"] is True
    assert plan["candidates"] == 1
    assert plan["items"][0]["status"] == "dry_run"
    assert plan["items"][0]["path"] == "/media/a.mkv"
    assert "DRY-RUN" in _read_summary(tmp_path)


# --- live refresh --------------------------------------------------------------


def _live_ctx(tmp_path: Path, reports: Path) -> object:
    return make_context(
        tmp_path,
        mode=SafetyMode.LIVE,
        integrations={
            "plexrefresh": {
                "sources": ["dupefinder_report"],
                "dupefinder_reports": str(reports),
            }
        },
    )


def test_live_refreshes_resolved_item(tmp_path: Path) -> None:
    reports = tmp_path / "dfreports"
    _write_report(
        reports,
        [{"skip_reason": "video_duration <= 0", "candidates": [{"file": "/media/a.mkv"}]}],
    )
    ctx = _live_ctx(tmp_path, reports)
    plex = FakePlex(
        items_by_path={
            "/media/a.mkv": [FakeItem("/lib/1", "movie", "Movies", "/media")],
        },
        status="sane_and_changed",
    )
    result = plexrefresh.run(ctx, client=plex, now=1000.0)

    assert plex.analyzed == ["/lib/1"]
    assert plex.updated == []  # per-item analyze succeeded, no fallback
    assert result.actions == 1
    assert result.metrics["refreshed"] == 1.0
    assert result.ok
    plan = _read_plan(tmp_path)
    assert plan["refreshed"] == 1
    assert plan["items"][0]["status"] == "sane_and_changed"


def test_live_falls_back_to_section_when_item_not_changed(tmp_path: Path) -> None:
    reports = tmp_path / "dfreports"
    _write_report(
        reports,
        [{"skip_reason": "video_duration <= 0", "candidates": [{"file": "/media/ep.mkv"}]}],
    )
    ctx = _live_ctx(tmp_path, reports)
    plex = FakePlex(
        items_by_path={
            "/media/ep.mkv": [FakeItem("/lib/9", "episode", "TV", "/media/Show")],
        },
        status="timeout",  # per-item analyze did not confirm -> section fallback
    )
    result = plexrefresh.run(ctx, client=plex, now=1000.0)

    assert plex.analyzed == ["/lib/9"]
    assert plex.updated == [("TV", "/media/Show")]  # partial folder scan triggered
    assert result.metrics["refreshed"] == 1.0
    plan = _read_plan(tmp_path)
    assert plan["items"][0]["status"] == "fallback_section"
    assert plan["items"][0]["sections"] == ["TV"]


def test_live_unresolved_item_records_failure(tmp_path: Path) -> None:
    reports = tmp_path / "dfreports"
    _write_report(
        reports,
        [{"skip_reason": "video_duration <= 0", "candidates": [{"file": "/media/missing.mkv"}]}],
    )
    ctx = _live_ctx(tmp_path, reports)
    plex = FakePlex(items_by_path={})  # nothing resolves
    result = plexrefresh.run(ctx, client=plex, now=1000.0)

    assert plex.analyzed == []
    assert not result.ok
    assert any("refresh not confirmed" in f.message for f in result.failures)
    plan = _read_plan(tmp_path)
    assert plan["items"][0]["status"] == "unresolved"


def test_live_analyze_error_is_recorded_not_raised(tmp_path: Path) -> None:
    reports = tmp_path / "dfreports"
    _write_report(
        reports,
        [{"skip_reason": "video_duration <= 0", "candidates": [{"file": "/media/a.mkv"}]}],
    )
    ctx = _live_ctx(tmp_path, reports)
    plex = FakePlex(
        items_by_path={"/media/a.mkv": [FakeItem("/lib/1", "movie", "Movies", "/media")]},
        raise_on_analyze=True,
    )
    result = plexrefresh.run(ctx, client=plex, now=1000.0)

    assert not result.ok
    plan = _read_plan(tmp_path)
    assert plan["items"][0]["status"] == "analyze_failed"


def test_section_filter_scopes_items(tmp_path: Path) -> None:
    reports = tmp_path / "dfreports"
    _write_report(
        reports,
        [{"skip_reason": "video_duration <= 0", "candidates": [{"file": "/media/a.mkv"}]}],
    )
    ctx = make_context(
        tmp_path,
        mode=SafetyMode.LIVE,
        integrations={
            "plexrefresh": {
                "sources": ["dupefinder_report"],
                "dupefinder_reports": str(reports),
                "sections": ["Movies"],  # only refresh items in Movies
            }
        },
    )
    plex = FakePlex(
        items_by_path={
            "/media/a.mkv": [
                FakeItem("/lib/1", "movie", "Movies", "/media"),
                FakeItem("/lib/2", "movie", "Other", "/media"),
            ]
        },
        status="sane_and_changed",
    )
    plexrefresh.run(ctx, client=plex, now=1000.0)
    assert plex.analyzed == ["/lib/1"]  # /lib/2 (section 'Other') filtered out


# --- idempotency ---------------------------------------------------------------


def test_ledger_skips_already_refreshed_paths(tmp_path: Path) -> None:
    media = tmp_path / "media"
    media.mkdir()
    f = media / "a.mkv"
    f.write_text("data")
    reports = tmp_path / "dfreports"
    _write_report(
        reports,
        [{"skip_reason": "video_duration <= 0", "candidates": [{"file": str(f)}]}],
    )
    ctx = _live_ctx(tmp_path, reports)
    plex = FakePlex(
        items_by_path={str(f): [FakeItem("/lib/1", "movie", "Movies", str(media))]},
        status="sane_and_changed",
    )

    first = plexrefresh.run(ctx, client=plex, now=1000.0)
    assert first.metrics["refreshed"] == 1.0
    assert plex.analyzed == ["/lib/1"]

    # Second run: same (unchanged) file is in the ledger -> skipped, Plex untouched.
    plex2 = FakePlex(
        items_by_path={str(f): [FakeItem("/lib/1", "movie", "Movies", str(media))]},
        status="sane_and_changed",
    )
    second = plexrefresh.run(ctx, client=plex2, now=2000.0)
    assert plex2.analyzed == []
    assert second.metrics["refreshed"] == 0.0
    assert "already refreshed" in _read_summary(tmp_path)


def test_max_items_per_run_caps_and_defers(tmp_path: Path) -> None:
    reports = tmp_path / "dfreports"
    _write_report(
        reports,
        [
            {"skip_reason": "video_duration <= 0", "candidates": [{"file": f"/media/{i}.mkv"}]}
            for i in range(5)
        ],
    )
    ctx = make_context(
        tmp_path,
        mode=SafetyMode.LIVE,
        integrations={
            "plexrefresh": {
                "sources": ["dupefinder_report"],
                "dupefinder_reports": str(reports),
                "max_items_per_run": 2,
            }
        },
    )
    plex = FakePlex(
        items_by_path={
            f"/media/{i}.mkv": [FakeItem(f"/lib/{i}", "movie", "Movies", "/media")]
            for i in range(5)
        },
        status="sane_and_changed",
    )
    plexrefresh.run(ctx, client=plex, now=1000.0)
    plan = _read_plan(tmp_path)
    assert plan["candidates"] == 2  # only 2 considered this run
    assert "deferred" in plan["note"]


def test_live_without_client_records_config_failure(tmp_path: Path) -> None:
    reports = tmp_path / "dfreports"
    _write_report(
        reports,
        [{"skip_reason": "video_duration <= 0", "candidates": [{"file": "/media/a.mkv"}]}],
    )
    ctx = _live_ctx(tmp_path, reports)
    result = plexrefresh.run(ctx, client=None, now=1000.0)
    assert not result.ok
    assert any("requires an injected Plex client" in f.message for f in result.failures)


def test_no_candidates_is_clean(tmp_path: Path) -> None:
    ctx = make_context(tmp_path, integrations={"plexrefresh": {"sources": ["recent_files"]}})
    result = plexrefresh.run(ctx, client=FakePlex(), now=1000.0)
    assert result.ok
    assert result.metrics["candidates"] == 0.0
    assert "No stale-metadata candidates" in _read_summary(tmp_path)


def test_watch_list_is_always_a_candidate(tmp_path: Path) -> None:
    ctx = make_context(
        tmp_path,
        integrations={
            "plexrefresh": {
                "sources": ["recent_files"],
                "watch_list": ["/media/forced.mkv"],
            }
        },
    )
    result = plexrefresh.run(ctx, client=FakePlex(), now=1000.0)
    plan = _read_plan(tmp_path)
    assert plan["candidates"] == 1
    assert plan["items"][0]["source"] == "watch_list"
    assert result.metrics["candidates"] == 1.0


def test_static_no_destructive_or_subprocess_calls() -> None:
    """INVARIANT I1 guard: the module must not delete/move files or shell out."""
    src = Path(plexrefresh.__file__).read_text(encoding="utf-8")
    for forbidden in ("os.remove", "shutil.rmtree", ".unlink(", "import subprocess", "rmtree"):
        assert forbidden not in src, f"forbidden token in plexrefresh.py: {forbidden}"


@pytest.mark.parametrize("mode", [SafetyMode.DRY_RUN, SafetyMode.AUDIT])
def test_non_live_modes_are_dry(tmp_path: Path, mode: SafetyMode) -> None:
    reports = tmp_path / "dfreports"
    _write_report(
        reports,
        [{"skip_reason": "video_duration <= 0", "candidates": [{"file": "/media/a.mkv"}]}],
    )
    ctx = make_context(
        tmp_path,
        mode=mode,
        integrations={
            "plexrefresh": {"sources": ["dupefinder_report"], "dupefinder_reports": str(reports)}
        },
    )
    plex = FakePlex(items_by_path={"/media/a.mkv": [FakeItem("/lib/1", "movie", "M", "/m")]})
    result = plexrefresh.run(ctx, client=plex, now=1000.0)
    assert plex.analyzed == []
    assert result.actions == 0
