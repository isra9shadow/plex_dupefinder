"""Pure, typed port of the legacy ``_quarantine_logical_path`` (CTO-12 / MIGRATION_PLAN §9).

Same logical-relative path construction as ``plex_dupefinder._quarantine_logical_path``,
but with NO global ``cfg``, NO Plex and NO real filesystem: the destination root is
passed explicitly via ``quarantine_dir`` and ``quarantine_logical_path`` returns the
ABSOLUTE destination ``os.path.join(quarantine_dir, <logical-rel>)``. Locked to the
legacy by ``tests/regression/test_dedupe_quarantine_parity.py`` so the eventual
migration is verifiable, not a leap of faith.

Scope / parity caveat
---------------------
The legacy ``_quarantine_logical_path(src, title)`` is itself already PURE — pure
string ops, no ``cfg``, no FS, no Plex — and computes only the *relative* sub-tree
(title-anchored show/movie folder + filename). It does NOT read ``QUARANTINE_DIR``;
that join lives in the caller ``quarantine_files``. This module reproduces that
relative construction byte-for-byte in ``logical_relative_path`` and then joins the
root in ``quarantine_logical_path``, mirroring the legacy line
``dest = os.path.join(quarantine_root, logical_rel)``.

The legacy *collision* handling (library-name suffix on the top folder, then a unix
timestamp on the filename) is deliberately NOT ported here: it branches on
``os.path.exists(dest)`` against the real filesystem and is therefore impure. That
existence-collision step stays in ``core/fs`` at wiring time. Only the pure path
construction is reproduced here, exactly as the task requires.
"""

from __future__ import annotations

import os
import re


def _key(s: str) -> str:
    """Lowercase, strip non-alphanumerics — for fuzzy comparison.

    Byte-for-byte identical to the legacy nested ``_key`` helper.
    """
    return re.sub(r"[^a-z0-9]", "", s.lower())


def logical_relative_path(src: str, title: str) -> str:
    """Return the logical *relative* path for a quarantined file.

    Exact pure port of ``plex_dupefinder._quarantine_logical_path``. Strips
    everything up to (and including) the library-root prefix by locating the
    directory component that matches the Plex *title*. Only the title-level folder
    and everything below it are kept. Falls back to (last-two-dirs + filename) when
    no title match is found.

    Examples
    --------
    src = /mnt/user/Media/TV/Breaking Bad/Season 01/ep.mkv , title = "Breaking Bad"
    →     Breaking Bad/Season 01/ep.mkv

    src = /mnt/user/Media/Movies/Dune (2021)/Dune.2021.mkv , title = "Dune"
    →     Dune (2021)/Dune.2021.mkv
    """
    # Normalise separators and split, discarding empty segments from leading /
    parts = [p for p in src.replace("\\", "/").split("/") if p]
    if not parts:
        return os.path.basename(src)

    filename = parts[-1]
    dirs = parts[:-1]
    if not dirs:
        return filename

    title_key = _key(title) if title else ""
    anchor: int | None = None

    if title_key:
        for i, d in enumerate(dirs):
            dk = _key(d)
            # Match when title is wholly contained in dir name or vice-versa.
            # This handles "Dune (2021)" matching title "Dune", and exact hits
            # like "Breaking Bad" → "Breaking Bad".
            if title_key == dk or title_key in dk or dk in title_key:
                anchor = i
                break

    if anchor is not None:
        # Equivalent to the legacy ``dirs[anchor:] + [filename]`` (unpacked for ruff).
        logical = [*dirs[anchor:], filename]
    else:
        # Fallback: last two directory levels + filename.
        # Covers show/season/ep and movie-folder/file without needing the title.
        # Equivalent to the legacy ``dirs[-2:] + [filename]``.
        logical = [*dirs[-2:], filename]

    return os.path.join(*logical)


def quarantine_logical_path(src: str, title: str, quarantine_dir: str) -> str:
    """Return the absolute quarantine destination for ``src``.

    Mirrors the legacy ``dest = os.path.join(quarantine_root, logical_rel)`` using
    the explicitly-passed ``quarantine_dir`` (no global ``cfg``). The relative
    sub-tree is built by :func:`logical_relative_path`.

    NB: collision handling (top-folder library suffix, timestamp fallback) is NOT
    applied here — it depends on the real filesystem and stays in ``core/fs`` at
    wiring time. See the module docstring.
    """
    return os.path.join(quarantine_dir, logical_relative_path(src, title))
