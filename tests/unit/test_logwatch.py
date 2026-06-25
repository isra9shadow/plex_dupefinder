"""Tests for modules.ops.logwatch (read-only error scan + AI summary)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from core.errors import IntegrationError
from modules.ops import logwatch
from tests.fakes import make_context


def _read_plan(tmp_path: Path) -> dict[str, object]:
    return json.loads((tmp_path / "reports" / "logwatch" / "plan.json").read_text(encoding="utf-8"))


def _read_summary(tmp_path: Path) -> str:
    return (tmp_path / "reports" / "logwatch" / "summary.md").read_text(encoding="utf-8")


# --- pure extraction -----------------------------------------------------------


def test_extract_errors_matches_patterns() -> None:
    logs = (
        "2026-01-01 info: all good\n"
        "2026-01-01 ERROR: boom\n"
        "2026-01-01 something WARNING here\n"
        "2026-01-01 Traceback (most recent call last):\n"
        "2026-01-01 connection refused\n"
        "2026-01-01 GET /x 503\n"
        "2026-01-01 nothing to see\n"
    )
    found = logwatch.extract_errors(logs)
    assert "all good" not in "\n".join(found)
    assert any("ERROR" in line for line in found)
    assert any("WARNING" in line for line in found)
    assert any("Traceback" in line for line in found)
    assert any("refused" in line for line in found)
    assert any("503" in line for line in found)
    assert len(found) == 5


def test_cap_recent_keeps_most_recent() -> None:
    entries = [
        logwatch.ContainerErrors("a", ["a1", "a2", "a3"]),
        logwatch.ContainerErrors("b", ["b1", "b2", "b3"]),
    ]
    capped = logwatch._cap_recent(entries, 2)
    total = sum(len(e.lines) for e in capped)
    assert total == 2
    # Newest container (b) and its newest line is preserved.
    assert capped[-1].name == "b"
    assert capped[-1].lines[-1] == "b3"


# --- run integration -----------------------------------------------------------


def _patch_docker(
    monkeypatch: pytest.MonkeyPatch, *, names: list[str], logs: dict[str, str]
) -> None:
    monkeypatch.setattr(logwatch.docker, "container_names", lambda **kw: list(names))
    monkeypatch.setattr(
        logwatch.docker, "logs", lambda name, *, since_days, tail=0: logs.get(name, "")
    )


class _FakeOllama:
    def __init__(self, *, answer: str | None = None, fail: bool = False, **_: object) -> None:
        self._answer = answer or "Resumen IA."
        self._fail = fail
        self.prompts: list[str] = []

    def complete(self, prompt: str, *, timeout: float | None = None) -> str:
        self.prompts.append(prompt)
        if self._fail:
            raise IntegrationError("ollama down")
        return self._answer


def test_run_extracts_errors_and_writes_reports(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_docker(
        monkeypatch,
        names=["Plex", "Sonarr"],
        logs={
            "Plex": "info ok\nERROR media scan failed\n",
            "Sonarr": "all fine here\n",
        },
    )
    fake = _FakeOllama(answer="Plex: fallo de escaneo. Accion: revisar montaje.")
    monkeypatch.setattr(logwatch, "OllamaClient", lambda **kw: fake)

    ctx = make_context(tmp_path, integrations={"logwatch": {"days": 2}})
    result = logwatch.run(ctx)

    assert result.ok
    assert result.actions == 0  # read-only
    assert result.metrics["containers"] == 2.0
    assert result.metrics["error_lines"] == 1.0
    assert fake.prompts and "Contenedor: Plex" in fake.prompts[0]

    plan = _read_plan(tmp_path)
    assert plan["error_lines"] == 1
    assert "Plex" in plan["errors"]
    assert "Sonarr" not in plan["errors"]  # no error lines -> excluded
    assert "fallo de escaneo" in _read_summary(tmp_path)


def test_run_resilient_when_ollama_fails(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_docker(monkeypatch, names=["Plex"], logs={"Plex": "FATAL crash\n"})
    monkeypatch.setattr(logwatch, "OllamaClient", lambda **kw: _FakeOllama(fail=True))

    ctx = make_context(tmp_path)
    result = logwatch.run(ctx)

    # The Ollama failure is recorded but the run still produces a plan with raw errors.
    assert not result.ok
    assert result.failures[0].category == "integration"
    plan = _read_plan(tmp_path)
    assert plan["errors"]["Plex"] == ["FATAL crash"]
    assert "Ollama unavailable" in _read_summary(tmp_path)


def test_run_filters_containers(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_docker(
        monkeypatch,
        names=["Plex", "Sonarr", "Radarr"],
        logs={"Plex": "ERROR x\n", "Sonarr": "ERROR y\n", "Radarr": "ERROR z\n"},
    )
    monkeypatch.setattr(logwatch, "OllamaClient", lambda **kw: _FakeOllama())

    ctx = make_context(tmp_path, integrations={"logwatch": {"containers": ["Sonarr"]}})
    result = logwatch.run(ctx)

    assert result.metrics["containers"] == 1.0  # filtered down to Sonarr
    plan = _read_plan(tmp_path)
    assert set(plan["errors"]) == {"Sonarr"}


def test_run_no_errors_does_not_call_ai(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_docker(monkeypatch, names=["Plex"], logs={"Plex": "all good\nstill fine\n"})

    def boom(**_: object) -> object:
        raise AssertionError("AI must not be called when there are no errors")

    monkeypatch.setattr(logwatch, "OllamaClient", boom)

    ctx = make_context(tmp_path)
    result = logwatch.run(ctx)

    assert result.ok
    assert result.metrics["error_lines"] == 0.0
    assert "No error lines found" in _read_summary(tmp_path)


def test_run_reports_docker_unavailable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(logwatch.docker, "container_names", lambda **kw: [])
    monkeypatch.setattr(logwatch.docker, "probe", lambda: "")  # docker not reachable

    ctx = make_context(tmp_path)
    result = logwatch.run(ctx)

    assert not result.ok  # surfaced as a failure, not a silent "0 errors"
    assert result.metrics["containers"] == 0.0
    assert "Docker NOT reachable" in _read_summary(tmp_path)


def test_dedupe_errors_folds_repeats_with_counts() -> None:
    lines = [
        "2026-06-25T08:00:00Z ERROR db connection refused",
        "2026-06-25T08:00:01Z ERROR db connection refused",
        "2026-06-25T08:00:02Z ERROR db connection refused",
        "2026-06-25T09:00:00Z WARN disk almost full",
    ]
    out = logwatch.dedupe_errors(lines, limit=10)
    # timestamps stripped, repeats folded with a count, most frequent first.
    assert out[0] == "ERROR db connection refused  (x3)"
    assert "WARN disk almost full" in out
    assert len(out) == 2


def test_dedupe_errors_respects_limit() -> None:
    # distinct text (not digits — _norm masks numbers, which would collapse them).
    lines = [f"ERROR distinct {chr(97 + i)}" for i in range(20)]
    assert len(logwatch.dedupe_errors(lines, limit=5)) == 5


def test_extract_errors_matches_structured_logs() -> None:
    logs = "ok line\nts level=error something broke\nts [ERR] boom\nts unable to connect\n"
    found = logwatch.extract_errors(logs)
    assert len(found) == 3  # level=error, [ERR], unable to


def test_run_one_container_log_failure_is_recorded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(logwatch.docker, "container_names", lambda **kw: ["Plex", "Bad"])

    def fake_logs(name: str, *, since_days: float, tail: int = 0) -> str:
        if name == "Bad":
            raise RuntimeError("docker exploded")
        return "ERROR plex problem\n"

    monkeypatch.setattr(logwatch.docker, "logs", fake_logs)
    monkeypatch.setattr(logwatch, "OllamaClient", lambda **kw: _FakeOllama())

    ctx = make_context(tmp_path)
    result = logwatch.run(ctx)

    # Bad container's failure is recorded but Plex's errors are still summarised.
    assert any("Bad" in f.message for f in result.failures)
    plan = _read_plan(tmp_path)
    assert "Plex" in plan["errors"]
