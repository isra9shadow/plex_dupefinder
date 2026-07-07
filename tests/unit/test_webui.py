"""Tests for webui.py (read-only reports server — pure bits only)."""

from __future__ import annotations

import webui


def test_parse_defaults() -> None:
    ns = webui._parse([])
    assert ns.port == 8888
    assert ns.host == "0.0.0.0"  # noqa: S104 - asserting the documented default
    assert ns.dir is None


def test_parse_overrides() -> None:
    ns = webui._parse(["--dir", "/x", "--port", "9000", "--host", "127.0.0.1"])
    assert (ns.dir, ns.port, ns.host) == ("/x", 9000, "127.0.0.1")


def test_default_reports_dir_returns_a_path() -> None:
    # Either the configured reporting.dir or the ./reports fallback — always a str.
    d = webui.default_reports_dir()
    assert isinstance(d, str) and d


def test_api_disabled_without_token() -> None:
    ok, code, msg = webui.check_request("health", "x", expected_token="")
    assert not ok and code == 403 and "deshabilitada" in msg


def test_api_rejects_bad_token() -> None:
    ok, code, _ = webui.check_request("health", "wrong", expected_token="secret")
    assert not ok and code == 403


def test_api_rejects_non_whitelisted_action() -> None:
    ok, code, msg = webui.check_request("dbrepair", "secret", expected_token="secret")
    assert not ok and code == 400 and "no permitida" in msg  # apply/destructive never allowed


def test_api_allows_readonly_action_with_token() -> None:
    ok, code, _ = webui.check_request("health", "secret", expected_token="secret")
    assert ok and code == 200


def test_check_token_ask_apply() -> None:
    assert webui.check_token("s", expected_token="s")[0] is True
    assert webui.check_token("x", expected_token="s")[0] is False
    assert webui.check_token("s", expected_token="")[0] is False  # API off


def test_export_markdown_concatenates_summaries(tmp_path) -> None:
    (tmp_path / "uptime").mkdir()
    (tmp_path / "uptime" / "summary.md").write_text("servicios ok", encoding="utf-8")
    (tmp_path / "cache").mkdir()  # infra dir, skipped
    (tmp_path / "cache" / "summary.md").write_text("x", encoding="utf-8")
    out = webui.export_markdown(str(tmp_path))
    assert out.startswith("# izumi")
    assert "## uptime" in out and "servicios ok" in out
    assert "## cache" not in out  # infra dir excluded
