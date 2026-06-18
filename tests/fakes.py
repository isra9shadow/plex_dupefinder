"""Reusable in-memory fakes/helpers for platform tests."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from core import logging as log_mod
from core.config import Config, LoggingConfig, ReportingConfig, SafetyConfig
from core.fs import Fs
from core.notify import Notifier
from core.safety import SafetyPolicy
from core.types import RunContext, SafetyMode


@dataclass
class FakeClock:
    """A controllable monotonic clock for time-dependent logic."""

    now: float = 0.0

    def advance(self, seconds: float) -> None:
        self.now += seconds

    def __call__(self) -> float:
        return self.now


def make_media_tree(root: Path) -> Path:
    """Create a tiny fake Plex-style media layout under ``root``."""
    (root / "series TV" / "Show (2020)" / "Season 01").mkdir(parents=True, exist_ok=True)
    (root / "Peliculas" / "Movie (2019)").mkdir(parents=True, exist_ok=True)
    return root


def make_context(
    tmp_path: Path,
    *,
    mode: SafetyMode = SafetyMode.DRY_RUN,
    paths: dict[str, str] | None = None,
    integrations: dict[str, dict[str, object]] | None = None,
) -> RunContext:
    """Build a real RunContext wired to temp dirs — for module tests."""
    cfg = Config(
        safety=SafetyConfig(quarantine_dir=tmp_path / "q", mode=mode),
        logging=LoggingConfig(dir=tmp_path / "logs"),
        reporting=ReportingConfig(dir=tmp_path / "reports"),
        paths=paths or {},
        integrations=integrations or {},
    )
    log_mod.configure(level="INFO", log_dir=cfg.logging.dir, run_id="test", console=False)
    return RunContext(
        run_id="test",
        mode=mode,
        config=cfg,
        logger=log_mod.get_logger("test"),
        fs=Fs(cfg, dry_run=mode != SafetyMode.LIVE),
        safety=SafetyPolicy(cfg),
        notify=Notifier(cfg),
    )
