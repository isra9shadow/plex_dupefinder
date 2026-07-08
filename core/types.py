"""Domain types and the frozen contract surface (FREEZE-2).

Protocols/events/`RunContext` live here (one types module) per ADR-0008. Concrete
``core`` classes are referenced under ``TYPE_CHECKING`` to avoid import cycles;
with ``from __future__ import annotations`` the field annotations are strings at
runtime, so importing this module never pulls in config/fs/etc.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

# The Unraid HOST launchers (menu.py/webui.py) run on Python 3.9, where StrEnum
# (3.11+) does not exist. Fall back to a str+Enum with StrEnum's __str__ so this
# module imports cleanly on the host; the containerised modules use real StrEnum.
if sys.version_info >= (3, 11):
    from enum import StrEnum
else:  # pragma: no cover - exercised only on the 3.9 host launcher
    from enum import Enum

    class StrEnum(str, Enum):
        __str__ = str.__str__


if TYPE_CHECKING:
    from core.config import Config
    from core.fs import Fs
    from core.logging import Logger
    from core.notify import Notifier
    from core.safety import SafetyPolicy


class SafetyMode(StrEnum):
    """How aggressively a run may act. Default is the safe one."""

    DRY_RUN = "dry_run"
    AUDIT = "audit"
    LIVE = "live"

    @classmethod
    def default(cls) -> SafetyMode:
        return cls.DRY_RUN


class Verdict(StrEnum):
    OK = "ok"
    DOUBTFUL = "doubtful"
    BAD = "bad"


class EventKind(StrEnum):
    RUN_START = "run_start"
    RUN_OK = "run_ok"
    RUN_FAIL = "run_fail"
    ALERT = "alert"
    SUMMARY = "summary"


class NotifyLevel(StrEnum):
    NONE = "none"
    FAIL = "fail"
    ALL = "all"


@dataclass(frozen=True)
class FailureRecord:
    category: str
    message: str
    src: str | None = None
    dest: str | None = None


@dataclass(frozen=True)
class ActionRecord:
    action: str
    src: str
    dest: str | None = None
    bytes: int = 0


@dataclass(frozen=True)
class QuarantineEntry:
    original_path: str
    quarantine_path: str
    sidecar_path: str
    reason: str
    restore_command: str
    ts: float


@dataclass(frozen=True)
class PurgeResult:
    deleted: int
    bytes_reclaimed: int
    simulated: bool


@dataclass(frozen=True)
class HealthResult:
    ok: bool
    checks: dict[str, bool]
    details: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class NotificationEvent:
    kind: EventKind
    module: str
    run_id: str
    title: str
    body: str = ""
    fields: dict[str, str] = field(default_factory=dict)


@dataclass
class ModuleResult:
    module: str
    run_id: str
    mode: SafetyMode
    actions: int = 0
    quarantined: int = 0
    bytes_reclaimed: int = 0
    failures: list[FailureRecord] = field(default_factory=list)
    metrics: dict[str, float] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.failures

    def add_failure(self, failure: FailureRecord) -> None:
        self.failures.append(failure)


@dataclass
class RunReport:
    run_id: str
    module: str
    mode: SafetyMode
    started: float
    finished: float
    actions: list[ActionRecord] = field(default_factory=list)
    failures: list[FailureRecord] = field(default_factory=list)
    metrics: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class RunContext:
    """Everything a module receives from ``run(ctx)``."""

    run_id: str
    mode: SafetyMode
    config: Config
    logger: Logger
    fs: Fs
    safety: SafetyPolicy
    notify: Notifier
