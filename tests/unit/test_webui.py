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
