"""Sonarr v3 client."""

from __future__ import annotations

from integrations.arr import ArrClient


class SonarrClient(ArrClient):
    def series(self) -> list[dict[str, object]]:
        return self._list("series")
