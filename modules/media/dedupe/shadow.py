"""Read-only shadow-compare of the native vs legacy dedupe decision (CTO-12).

A pure, side-effect-free comparator for an eventual *parity window*: it lets the
native port (``scoring.score`` + ``keeper.select_keeper``) run alongside the
legacy ``plex_dupefinder`` pipeline and surfaces any drift, WITHOUT acting. It
NEVER touches Plex, the filesystem, the network, or any global ``cfg`` — it reads
only the already-built ``parts`` mapping and the two pure dedupe modules.

The native pipeline mirrors the legacy ``_build_parts_for_item`` -> ``get_score``
-> ``select_keeper`` flow: each part already carries the fields scoring/keeper
need; for every part we (re)compute ``score`` via :func:`scoring.score` (exactly
as the legacy does, and only when NOT in paths-only mode, where the score is
ignored), then run :func:`keeper.select_keeper` to get the decision dict.
"""

from __future__ import annotations

from collections.abc import Mapping

from modules.media.dedupe.keeper import KeeperPolicy, select_keeper
from modules.media.dedupe.scoring import ScoreWeights, score

# Only these fields change the action taken; everything else (reason strings,
# diagnostics) is explanatory and intentionally excluded from drift detection.
_DECISION_FIELDS: tuple[str, ...] = (
    "keeper_id",
    "skip",
    "skip_reason",
    "top_score",
    "second_score",
    "score_delta",
)


def native_decision(
    parts: Mapping[int, Mapping[str, object]],
    weights: ScoreWeights,
    policy: KeeperPolicy,
) -> dict[str, object]:
    """Compute the native keeper decision for ``parts``, purely.

    Mirrors the legacy build-then-select flow: each part is copied (the input is
    never mutated), its ``score`` / ``score_breakdown`` are (re)computed via
    :func:`scoring.score` unless the policy is paths-only (where the keeper
    ignores the score), and the resulting parts are handed to
    :func:`keeper.select_keeper`.
    """
    built: dict[int, dict[str, object]] = {}
    for mid, part in parts.items():
        pi: dict[str, object] = dict(part)
        if not policy.find_duplicate_filepaths_only:
            pi["score"], pi["score_breakdown"] = score(pi, weights)
        built[mid] = pi
    return select_keeper(built, policy)


def diff_decision(
    native: Mapping[str, object],
    legacy: Mapping[str, object],
) -> list[str]:
    """Return human-readable differences in the decision-relevant fields.

    Compares only the fields that change the action taken (``keeper_id``,
    ``skip``, ``skip_reason``, ``top_score``, ``second_score``, ``score_delta``).
    An empty list means the two decisions are identical for those fields.
    """
    diffs: list[str] = []
    for field in _DECISION_FIELDS:
        n = native.get(field)
        legacy_val = legacy.get(field)
        if n != legacy_val:
            diffs.append(f"{field}: native={n!r} legacy={legacy_val!r}")
    return diffs
