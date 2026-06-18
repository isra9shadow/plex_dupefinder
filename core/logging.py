"""Structured JSON logging with size rotation and a per-run run_id."""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

_ROOT = "izumi"
_run_id = ""


def new_run_id() -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{uuid.uuid4().hex[:6]}"


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "run_id": getattr(record, "run_id", ""),
            "msg": record.getMessage(),
        }
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            payload.update(fields)
        return json.dumps(payload, default=str, sort_keys=True)


class Logger:
    """Thin structured wrapper around a stdlib logger, bound to a run_id."""

    def __init__(self, base: logging.Logger, run_id: str) -> None:
        self._log = base
        self._run_id = run_id

    def _emit(self, level: int, msg: str, fields: dict[str, object]) -> None:
        self._log.log(level, msg, extra={"run_id": self._run_id, "fields": fields})

    def debug(self, msg: str, **fields: object) -> None:
        self._emit(logging.DEBUG, msg, fields)

    def info(self, msg: str, **fields: object) -> None:
        self._emit(logging.INFO, msg, fields)

    def warning(self, msg: str, **fields: object) -> None:
        self._emit(logging.WARNING, msg, fields)

    def error(self, msg: str, **fields: object) -> None:
        self._emit(logging.ERROR, msg, fields)


def configure(
    *,
    level: str = "INFO",
    log_dir: Path,
    run_id: str,
    rotate_mb: int = 10,
    backups: int = 5,
    console: bool = True,
) -> None:
    global _run_id
    _run_id = run_id
    log_dir.mkdir(parents=True, exist_ok=True)
    base = logging.getLogger(_ROOT)
    base.setLevel(level.upper())
    base.handlers.clear()
    base.propagate = False
    fmt = _JsonFormatter()
    file_handler = RotatingFileHandler(
        log_dir / "izumi.log",
        maxBytes=rotate_mb * 1024 * 1024,
        backupCount=backups,
        encoding="utf-8",
    )
    file_handler.setFormatter(fmt)
    base.addHandler(file_handler)
    if console:
        stream = logging.StreamHandler()
        stream.setFormatter(fmt)
        base.addHandler(stream)


def get_logger(name: str) -> Logger:
    return Logger(logging.getLogger(f"{_ROOT}.{name}"), _run_id)
