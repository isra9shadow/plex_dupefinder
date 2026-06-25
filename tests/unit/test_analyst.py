"""Tests for modules.ops.analyst (read-only: explain why files were not moved)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from core.errors import IntegrationError
from modules.ops import analyst
from tests.fakes import make_context


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


class _FakeOllama:
    def __init__(self, *, answer: str = "Resumen IA.", fail: bool = False, **_: object) -> None:
        self._answer = answer
        self._fail = fail
        self.prompts: list[str] = []

    def complete(self, prompt: str, *, timeout: float | None = None) -> str:
        self.prompts.append(prompt)
        if self._fail:
            raise IntegrationError("ollama down")
        return self._answer


# --- pure heuristics -----------------------------------------------------------


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


# --- run integration -----------------------------------------------------------


def test_run_summarises_needs_review(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _write_plan(
        tmp_path,
        [
            {"filename": "gr31l@nd.mkv", "kind": "unknown", "confidence": 0},
            {"filename": "Some Movie.mkv", "kind": "movie", "confidence": 55},
        ],
    )
    fake = _FakeOllama(answer="Grupo leet: renombrar. Grupo sin anio: bajar umbral.")
    monkeypatch.setattr(analyst, "OllamaClient", lambda **kw: fake)

    ctx = make_context(tmp_path)
    result = analyst.run(ctx)

    assert result.ok
    assert result.actions == 0  # read-only
    assert result.metrics["needs_review"] == 2.0
    # Worst (confidence 0) listed first in the prompt.
    assert fake.prompts and fake.prompts[0].index("gr31l@nd") < fake.prompts[0].index("Some Movie")
    assert "renombrar" in _read_summary(tmp_path)
    assert _read_plan(tmp_path)["stats"]["total"] == 2


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

    assert not result.ok  # failure recorded
    assert "Ollama no disponible" in _read_summary(tmp_path)
    assert _read_plan(tmp_path)["stats"]["total"] == 1  # heuristics still saved


def test_run_empty_needs_review_does_not_call_ai(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _write_plan(tmp_path, [])

    def boom(**_: object) -> object:
        raise AssertionError("AI must not be called when needs_review is empty")

    monkeypatch.setattr(analyst, "OllamaClient", boom)

    ctx = make_context(tmp_path)
    result = analyst.run(ctx)

    assert result.ok
    assert result.metrics["needs_review"] == 0.0
    assert "movio todo" in _read_summary(tmp_path)
