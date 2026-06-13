"""izumi core platform — the shared spine consumed by every module.

Public surface is frozen in ``core/CONTRACT.md`` (FREEZE-2). Sprint 0 lays the
foundations: error hierarchy (``core.errors``), domain types (``core.types``)
and secret loading (``core.secrets``). Config, logging, locks, safety, fs and
notify arrive in Sprint 1.
"""

from __future__ import annotations

__version__ = "0.1.0"
