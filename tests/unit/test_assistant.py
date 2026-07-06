"""Tests for aictx.assistant (NL routing + prompt assembly, pure)."""

from __future__ import annotations

from aictx import assistant


def test_route_by_keyword() -> None:
    assert assistant.route("un servicio está caído") == ["uptime"]
    assert assistant.route("la base de datos está corrupta") == ["dbcheck"]
    assert assistant.route("dns no resuelve, getaddrinfo ENOTFOUND") == ["netdoctor"]
    # "error 13" hits permsdoctor first; "error" also pulls in logwatch (reasonable).
    perms = assistant.route("permisos: error 13 en appdata")
    assert perms[0] == "permsdoctor" and "logwatch" in perms


def test_route_multiple_and_cap() -> None:
    # "espacio" -> diskwatch+status, "log" -> logwatch, "estado" -> status(dup)
    got = assistant.route("mira el espacio en disco y los logs y el estado", max_modules=4)
    assert got[:3] == ["diskwatch", "status", "logwatch"]
    assert len(got) <= 4
    assert got.count("status") == 1  # de-duplicated


def test_route_default_when_no_match() -> None:
    assert assistant.route("hola qué tal") == ["status", "uptime"]


def test_build_prompt_has_question_sections_and_discipline() -> None:
    prompt = assistant.build_prompt(
        "¿por qué?", [("uptime", "todo arriba"), ("dbcheck", "1 corrupta")]
    )
    assert "PREGUNTA: ¿por qué?" in prompt
    assert "### uptime" in prompt and "todo arriba" in prompt
    assert "### dbcheck" in prompt and "1 corrupta" in prompt
    assert "fact" in prompt and "hipótesis" in prompt  # evidence discipline
    assert prompt.rstrip().endswith("RESPUESTA:")


def test_assemble_runs_each_routed_module() -> None:
    seen: list[str] = []

    def run_and_read(module: str) -> str:
        seen.append(module)
        return f"resumen de {module}"

    prompt, sections = assistant.assemble("un servicio está caído", run_and_read)
    assert seen == ["uptime"]  # routed to uptime, and it was run+read
    assert sections == [("uptime", "resumen de uptime")]
    assert "resumen de uptime" in prompt


def test_fallback_answer_surfaces_summaries() -> None:
    out = assistant.fallback_answer([("uptime", "todo ok"), ("dbcheck", "")], "conexión rechazada")
    assert "IA no disponible: conexión rechazada" in out
    assert "### uptime" in out and "todo ok" in out
    assert "### dbcheck" in out and "(sin datos)" in out
