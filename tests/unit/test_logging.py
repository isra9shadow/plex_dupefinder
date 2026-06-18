"""Tests for core.logging (structured JSON logging)."""

from __future__ import annotations

import json
import re
from pathlib import Path

from core import logging as logmod


def test_new_run_id_format() -> None:
    assert re.match(r"^\d{8}-\d{6}-[0-9a-f]{6}$", logmod.new_run_id())


def test_structured_json_log(tmp_path: Path) -> None:
    logmod.configure(level="DEBUG", log_dir=tmp_path, run_id="rid123", console=False)
    log = logmod.get_logger("test")
    log.debug("dbg")
    log.info("hello world", key="value", n=3)
    log.warning("warn")
    log.error("boom")
    payloads = [
        json.loads(line)
        for line in (tmp_path / "izumi.log").read_text(encoding="utf-8").splitlines()
    ]
    info = next(p for p in payloads if p["msg"] == "hello world")
    assert info["run_id"] == "rid123"
    assert info["key"] == "value"
    assert info["n"] == 3
    assert any(p["level"] == "ERROR" for p in payloads)


def test_console_handler(tmp_path: Path) -> None:
    logmod.configure(level="INFO", log_dir=tmp_path, run_id="r", console=True)
    logmod.get_logger("t").warning("w")
    assert (tmp_path / "izumi.log").exists()
