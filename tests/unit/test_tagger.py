"""Tests for modules.arr.tagger (Radarr tag writer — injected client, no network)."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

from core.types import SafetyMode
from integrations.radarr import RadarrClient
from modules.arr import tagger
from tests.fakes import make_context


def _client(
    movies: list[dict[str, object]], tags: list[dict[str, object]], calls: list[tuple]
) -> RadarrClient:
    routes = {"/movie": movies, "/tag": tags}

    def fetch(url: str, headers: Mapping[str, str], timeout: float) -> str:
        for needle, payload in routes.items():
            if url.endswith(needle):
                return json.dumps(payload)
        raise OSError(url)

    def write(
        method: str, url: str, headers: Mapping[str, str], body: bytes | None, timeout: float
    ) -> str:
        payload = json.loads(body) if body else {}
        calls.append((method, url, payload))
        if method == "POST" and url.endswith("/tag"):
            return json.dumps({"id": 99, "label": payload["label"]})
        return ""

    return RadarrClient("http://r", "KEY", fetcher=fetch, writer=write)


def _plan(tmp_path: Path) -> dict:
    return json.loads(
        (tmp_path / "reports" / "radarr_tagger" / "plan.json").read_text(encoding="utf-8")
    )


def test_dry_run_reports_diff_but_writes_nothing(tmp_path: Path) -> None:
    movies = [{"id": 1, "collection": {"tmdbId": 8091}}, {"id": 2, "collection": None}]
    calls: list[tuple] = []
    ctx = make_context(tmp_path)  # DRY_RUN default
    result = tagger.run(ctx, client=_client(movies, [], calls))

    assert calls == []  # nothing written to Radarr
    assert result.actions == 0
    assert result.metrics["to_add"] == 1.0
    assert _plan(tmp_path)["to_add"] == {"izumi-saga": 1}


def test_live_creates_tag_and_adds_it_in_bulk(tmp_path: Path) -> None:
    movies = [{"id": 1, "collection": {"tmdbId": 8091}}, {"id": 5, "collection": {"tmdbId": 10}}]
    calls: list[tuple] = []
    ctx = make_context(tmp_path, mode=SafetyMode.LIVE)
    result = tagger.run(ctx, client=_client(movies, [], calls))

    methods = [(m, u.split("/api/v3/")[-1]) for m, u, _ in calls]
    assert ("POST", "tag") in methods  # managed tag created on first use
    put = next(p for m, u, p in calls if m == "PUT")
    assert sorted(put["movieIds"]) == [1, 5] and put["applyTags"] == "add"
    assert result.actions == 2 and result.metrics["to_add"] == 2.0


def test_chunks_splits_evenly() -> None:
    assert tagger._chunks([1, 2, 3, 4, 5], 2) == [[1, 2], [3, 4], [5]]
    assert tagger._chunks([], 2) == []


def test_live_batches_large_add_to_avoid_timeout(tmp_path: Path) -> None:
    movies = [{"id": i, "collection": {"tmdbId": i}} for i in range(450)]
    calls: list[tuple] = []
    ctx = make_context(tmp_path, mode=SafetyMode.LIVE)
    result = tagger.run(ctx, client=_client(movies, [], calls))

    puts = [p for m, _u, p in calls if m == "PUT"]
    assert len(puts) == 3  # 450 movies / 200 per batch → 3 calls
    assert sum(len(p["movieIds"]) for p in puts) == 450
    assert result.actions == 450 and result.metrics["to_add"] == 450.0


def test_live_removes_managed_tag_when_no_longer_matches(tmp_path: Path) -> None:
    # Movie has izumi-saga but is no longer in a collection → tag must be removed.
    tags = [{"id": 99, "label": "izumi-saga"}]
    movies = [{"id": 1, "collection": None, "tags": [99]}]
    calls: list[tuple] = []
    ctx = make_context(tmp_path, mode=SafetyMode.LIVE)
    result = tagger.run(ctx, client=_client(movies, tags, calls))

    put = next(p for m, u, p in calls if m == "PUT")
    assert put["movieIds"] == [1] and put["applyTags"] == "remove" and put["tags"] == [99]
    assert result.metrics["to_remove"] == 1.0


def test_cluster_tags_shared_stem_group(tmp_path: Path) -> None:
    movies = [
        {"id": 1, "title": "Hellboy", "collection": None},
        {"id": 2, "title": "Hellboy II: The Golden Army", "collection": None},
        {"id": 3, "title": "The Matrix", "collection": None},  # alone → not tagged
    ]
    ctx = make_context(tmp_path, integrations={"radarr_tagger": {"cluster": True}})
    result = tagger.run(ctx, client=_client(movies, [], []))
    assert result.metrics["to_add"] == 2.0
    assert _plan(tmp_path)["to_add"] == {"izumi-saga": 2}


def test_cluster_verify_llm_can_reject_or_confirm(tmp_path: Path) -> None:
    movies = [
        {"id": 1, "title": "Hellboy", "collection": None},
        {"id": 2, "title": "Hellboy II", "collection": None},
    ]
    cfg = {"radarr_tagger": {"cluster": True, "cluster_verify": True}}
    # Separate roots so the per-stem verification cache doesn't carry over.
    rejected = tagger.run(
        make_context(tmp_path / "a", integrations=cfg),
        client=_client(movies, [], []),
        llm=lambda _p: "NO",
    )
    assert rejected.metrics["to_add"] == 0.0  # LLM says not a franchise → not tagged

    confirmed = tagger.run(
        make_context(tmp_path / "b", integrations=cfg),
        client=_client(movies, [], []),
        llm=lambda _p: "SI",
    )
    assert confirmed.metrics["to_add"] == 2.0


def test_refresh_untagged_dry_run_counts_targets(tmp_path: Path) -> None:
    movies = [
        {"id": 1, "collection": {"tmdbId": 1}, "tags": []},
        {"id": 2, "collection": None, "tags": []},
    ]
    ctx = make_context(tmp_path, integrations={"radarr_tagger": {"refresh_untagged": True}})
    result = tagger.run(ctx, client=_client(movies, [], []))  # DRY_RUN
    assert result.metrics["refreshed"] == 2.0  # both lack izumi-saga → would refresh


def test_refresh_untagged_live_calls_radarr(tmp_path: Path) -> None:
    movies = [{"id": 1, "collection": None, "tags": []}]
    calls: list[tuple] = []
    ctx = make_context(
        tmp_path, mode=SafetyMode.LIVE, integrations={"radarr_tagger": {"refresh_untagged": True}}
    )
    tagger.run(ctx, client=_client(movies, [], calls))
    assert any(m == "POST" and u.endswith("/command") for m, u, _p in calls)


def test_missing_radarr_config_writes_error_report(tmp_path: Path) -> None:
    # No integrations.radarr → builds client from config → ConfigError → error report.
    result = tagger.run(make_context(tmp_path))
    assert not result.ok
    summary = (tmp_path / "reports" / "radarr_tagger" / "summary.md").read_text(encoding="utf-8")
    assert "ERROR" in summary and "integrations.radarr" in summary


def test_never_touches_unmanaged_tags(tmp_path: Path) -> None:
    # A manual tag "keep" (no izumi: prefix) on a standalone movie must be left alone.
    tags = [{"id": 7, "label": "keep"}]
    movies = [{"id": 1, "collection": None, "tags": [7]}]
    calls: list[tuple] = []
    ctx = make_context(tmp_path, mode=SafetyMode.LIVE)
    result = tagger.run(ctx, client=_client(movies, tags, calls))

    assert calls == []  # no add, no remove — izumi only manages its own prefix
    assert result.metrics["to_remove"] == 0.0
