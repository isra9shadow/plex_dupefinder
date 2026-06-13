"""Radarr v3 client."""

from __future__ import annotations

from integrations.arr import ArrClient


class RadarrClient(ArrClient):
    def movies(self) -> list[dict[str, object]]:
        return self._list("movie")
