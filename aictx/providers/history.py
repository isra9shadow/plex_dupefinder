"""HistoryContextProvider — recurring incidents + already-applied actions.

Surfaces the incident memory so a prompt does not re-suggest fixes that are
already resolved/applied. To keep ``aictx`` free of a ``core.cache`` import cycle
and the provider pure/testable, the caller fetches incidents (via
``SqliteCache.recent_incidents``) and injects the list — this module never touches
``core``. Renders a COMPACT Tier.MEDIUM block; returns None when there is nothing.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Protocol

from aictx.provider import ContextBlock, Tier

if TYPE_CHECKING:
    from collections.abc import Sequence


class _IncidentLike(Protocol):
    """Structural shape the provider needs — matches ``core.cache.Incident``.

    Declared structurally so ``aictx`` never imports ``core``; any object with
    these read-only attributes (the injected ``Incident`` rows) is accepted.
    """

    @property
    def severity(self) -> str: ...
    @property
    def title(self) -> str: ...
    @property
    def status(self) -> str: ...
    @property
    def first_seen(self) -> float: ...
    @property
    def last_seen(self) -> float: ...
    @property
    def applied(self) -> list[object]: ...


_TITLE = "HISTORIAL DE INCIDENCIAS"
_INSTRUCTION = "No vuelvas a recomendar acciones ya aplicadas/resueltas."


def _fmt_last_seen(ts: float) -> str:
    """Render a unix timestamp as a compact UTC date (best-effort)."""
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except (OverflowError, OSError, ValueError):
        return "?"


def _fmt_action(action: object) -> str:
    """Render one applied action as a trimmed single-line string."""
    if isinstance(action, dict):
        for key in ("action", "title", "name", "summary"):
            value = action.get(key)
            if value:
                return str(value).strip()
    return str(action).strip()


class HistoryContextProvider:
    """Compact MEDIUM block of open recurring incidents + applied actions."""

    name = "history"

    def __init__(self, incidents: Sequence[_IncidentLike]) -> None:
        self._incidents = list(incidents)

    def block(self) -> ContextBlock | None:
        open_incidents = [i for i in self._incidents if i.status == "open"]
        applied_lines: list[str] = []
        seen: set[str] = set()
        for incident in self._incidents:
            for action in incident.applied:
                rendered = _fmt_action(action)
                if rendered and rendered not in seen:
                    seen.add(rendered)
                    applied_lines.append(f"- {rendered}")

        if not open_incidents and not applied_lines:
            return None

        parts: list[str] = []
        if open_incidents:
            lines = [
                f"- {inc.title} [{inc.severity}] "
                f"(visto por última vez {_fmt_last_seen(inc.last_seen)})"
                for inc in open_incidents
            ]
            parts.append("**Incidencias abiertas recurrentes:**\n" + "\n".join(lines))
        if applied_lines:
            parts.append("**Acciones ya aplicadas/resueltas:**\n" + "\n".join(applied_lines))
        parts.append(_INSTRUCTION)

        return ContextBlock(
            name="history",
            title=_TITLE,
            body="\n\n".join(parts),
            tier=Tier.MEDIUM,
        )
