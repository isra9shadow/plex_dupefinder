"""Tests for modules.ops.analyst (aictx pipeline: structured JSON diagnosis)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from core.cache import SqliteCache
from core.errors import IntegrationError
from modules.ops import analyst
from tests.fakes import make_context


class _NullRuntime:
    """Stub so tests never shell out to docker/nvidia-smi via the real runtime provider."""

    name = "runtime"

    def block(self) -> None:
        return None


@pytest.fixture(autouse=True)
def _no_live_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(analyst, "RuntimeContextProvider", lambda *a, **k: _NullRuntime())


def _write_plan(tmp_path: Path, needs_review: list[dict[str, object]]) -> None:
    out = tmp_path / "reports" / "organizer"
    out.mkdir(parents=True, exist_ok=True)
    (out / "plan.json").write_text(
        json.dumps({"confident": [], "needs_review": needs_review}), encoding="utf-8"
    )


def _read_summary(tmp_path: Path) -> str:
    return (tmp_path / "reports" / "analyst" / "summary.md").read_text(encoding="utf-8")


def _read_plan(tmp_path: Path) -> dict[str, object]:
    return json.loads((tmp_path / "reports" / "analyst" / "plan.json").read_text(encoding="utf-8"))


def _finding(title: str = "Renombrar", commands: list[str] | None = None) -> dict[str, object]:
    return {
        "summary": "resumen",
        "findings": [
            {
                "title": title,
                "severity": "warning",
                "confidence": 70,
                "root_cause": "causa",
                "evidence": [{"kind": "fact", "detail": "ev"}],
                "recommended_actions": ["accion"],
                "unraid_commands": list(commands or []),
                "risk": "low",
                "priority": 3,
            }
        ],
    }


class _FakeOllama:
    def __init__(
        self, *, payload: dict[str, object] | None = None, fail: bool = False, **_: object
    ):
        self._payload = payload
        self._fail = fail
        self.calls: list[dict[str, object]] = []

    def generate_json(
        self,
        prompt: str,
        schema: dict[str, object],
        *,
        system: str | None = None,
        num_ctx: int | None = None,
        timeout: float | None = None,
    ) -> dict[str, object]:
        self.calls.append({"prompt": prompt, "system": system, "num_ctx": num_ctx})
        if self._fail:
            raise IntegrationError("ollama down")
        return self._payload if self._payload is not None else {"summary": "ok", "findings": []}


# --- pure helpers --------------------------------------------------------------


def test_heuristics_buckets_by_kind_and_confidence() -> None:
    items = [
        {"kind": "movie", "confidence": 0},
        {"kind": "movie", "confidence": 40},
        {"kind": "series", "confidence": 70},
        {"kind": "unknown", "confidence": 85},
    ]
    stats = analyst.heuristics(items)
    assert stats["total"] == 4
    assert stats["by_kind"] == {"movie": 2, "series": 1, "unknown": 1}
    assert stats["by_confidence"] == {"0": 1, "1-49": 1, "50-79": 1, "80+": 1}


def test_aggregate_skips_buckets_by_normalized_reason() -> None:
    groups = [
        {"title": "A", "discovery_decision": {"skip_reason": "score delta 0 below threshold 1000"}},
        {
            "title": "B",
            "discovery_decision": {"skip_reason": "score delta 50 below threshold 1000"},
        },
        {"title": "C", "revalidation": {"reason": "cooldown: 'x' is 2.00h old"}},
        {"title": "D"},
    ]
    skips, samples = analyst.aggregate_skips(groups)
    assert skips["score delta N below threshold N"] == 2
    assert samples["score delta N below threshold N"] == ["A", "B"]


def test_module_body_states_no_review_folder_and_lists_files() -> None:
    body = analyst.module_body(
        [{"filename": "x.mkv", "kind": "unknown", "confidence": 0}], {"total": 1}, None
    )
    assert "revision" in body.lower()  # the no-review-folder domain note
    assert "x.mkv" in body


# --- run integration -----------------------------------------------------------


def test_run_diagnoses_needs_review(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _write_plan(
        tmp_path,
        [
            {"filename": "gr31l@nd.mkv", "kind": "unknown", "confidence": 0},
            {"filename": "Some Movie.mkv", "kind": "movie", "confidence": 55},
        ],
    )
    fake = _FakeOllama(payload=_finding(title="Renombrar gr31l@nd"))
    monkeypatch.setattr(analyst, "OllamaClient", lambda **kw: fake)

    ctx = make_context(tmp_path)
    result = analyst.run(ctx)

    assert result.ok
    assert result.actions == 0  # read-only
    assert result.metrics["needs_review"] == 2.0
    # system context (Unraid facts) is sent separately; prompt carries the data.
    assert fake.calls and "Unraid" in str(fake.calls[0]["system"])
    assert "gr31l@nd" in str(fake.calls[0]["prompt"])
    assert "Renombrar" in _read_summary(tmp_path)  # rendered from structured JSON
    plan = _read_plan(tmp_path)
    assert plan["organizer"]["total"] == 2
    assert plan["diagnosis"]["findings"][0]["title"] == "Renombrar gr31l@nd"
    # memory loop: the finding was recorded as an incident for future runs.
    cache = SqliteCache(tmp_path / "reports" / "cache" / "incidents.db")
    try:
        assert len(cache.recent_incidents("analyst")) == 1
    finally:
        cache.close()


def test_run_includes_dupefinder_skips(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _write_plan(tmp_path, [])
    reports = tmp_path / "df_reports"
    reports.mkdir()
    (reports / "dupefinder_report_abc_20260625T000000Z.json").write_text(
        json.dumps(
            {
                "summary": {"groups_found": 3},
                "failure_summary": {"PLEX_API_ERROR": 2, "UNKNOWN": 0},
                "groups": [
                    {
                        "title": "A",
                        "discovery_decision": {"skip_reason": "score delta 0 below threshold 1000"},
                    },
                    {
                        "title": "B",
                        "discovery_decision": {"skip_reason": "score delta 7 below threshold 1000"},
                    },
                    {"title": "C", "revalidation": {"reason": "cooldown: 'c' is 2.00h old"}},
                ],
            }
        ),
        encoding="utf-8",
    )
    fake = _FakeOllama(payload=_finding(title="Duplicados score delta"))
    monkeypatch.setattr(analyst, "OllamaClient", lambda **kw: fake)

    ctx = make_context(tmp_path, integrations={"analyst": {"dupefinder_reports": str(reports)}})
    result = analyst.run(ctx)

    assert result.ok
    assert result.metrics["dupe_skips"] == 3.0
    assert "Dupefinder" in str(fake.calls[0]["prompt"])
    assert "score delta" in str(fake.calls[0]["prompt"])
    plan = _read_plan(tmp_path)
    assert plan["dupefinder"]["skips"]["score delta N below threshold N"] == 2


def test_run_guard_strips_non_unraid_commands(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _write_plan(tmp_path, [{"filename": "x.mkv", "kind": "movie", "confidence": 10}])
    fake = _FakeOllama(payload=_finding(commands=["systemctl restart plex", "docker restart Plex"]))
    monkeypatch.setattr(analyst, "OllamaClient", lambda **kw: fake)

    ctx = make_context(tmp_path)
    result = analyst.run(ctx)

    assert result.ok
    plan = _read_plan(tmp_path)
    cmds = plan["diagnosis"]["findings"][0]["unraid_commands"]
    assert cmds == ["docker restart Plex"]  # systemctl vetoed deterministically
    assert "vetados" in str(plan["note"])


def test_run_no_plan_is_config_failure(tmp_path: Path) -> None:
    ctx = make_context(tmp_path)
    result = analyst.run(ctx)
    assert not result.ok
    assert result.failures[0].category == "config"


def test_run_resilient_when_ollama_fails(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _write_plan(tmp_path, [{"filename": "x.mkv", "kind": "movie", "confidence": 10}])
    monkeypatch.setattr(analyst, "OllamaClient", lambda **kw: _FakeOllama(fail=True))

    ctx = make_context(tmp_path)
    result = analyst.run(ctx)

    assert not result.ok
    assert "Ollama no disponible" in _read_summary(tmp_path)
    plan = _read_plan(tmp_path)
    assert plan["organizer"]["total"] == 1  # heuristics still saved
    assert plan["diagnosis"] is None


def test_run_empty_needs_review_does_not_call_ai(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _write_plan(tmp_path, [])

    def boom(**_: object) -> object:
        raise AssertionError("AI must not be called when there is nothing to analyze")

    monkeypatch.setattr(analyst, "OllamaClient", boom)

    ctx = make_context(tmp_path)
    result = analyst.run(ctx)

    assert result.ok
    assert result.metrics["needs_review"] == 0.0
    assert "movio todo" in _read_summary(tmp_path)
