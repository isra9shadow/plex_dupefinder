"""Radarr v3 client."""

from __future__ import annotations

from collections.abc import Sequence

from integrations.arr import ArrClient


class RadarrClient(ArrClient):
    def movies(self) -> list[dict[str, object]]:
        return self._list("movie")

    def edit_movie_tags(self, movie_ids: Sequence[int], tag_id: int, *, add: bool) -> None:
        """Add or remove ONE tag on many movies in a single call (PUT /movie/editor)."""
        if not movie_ids:
            return
        self._send(
            "PUT",
            "movie/editor",
            {
                "movieIds": list(movie_ids),
                "tags": [tag_id],
                "applyTags": "add" if add else "remove",
            },
        )

    def refresh_movies(self, movie_ids: Sequence[int]) -> None:
        """Queue a metadata refresh (from TMDb, incl. collection) for these movies."""
        if not movie_ids:
            return
        self._send("POST", "command", {"name": "RefreshMovie", "movieIds": list(movie_ids)})
