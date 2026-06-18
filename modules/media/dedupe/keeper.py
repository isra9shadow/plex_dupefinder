"""Pure, typed port of the legacy ``select_keeper`` (CTO-12 / MIGRATION_PLAN §9).

Same safety guards and decision dict as ``plex_dupefinder.select_keeper``, but
with NO global ``cfg``, NO Plex and NO filesystem: every threshold is passed
explicitly via ``KeeperPolicy`` and the function reads ONLY the already-built
``parts`` mapping (which carries ``score`` and ``parts_existence`` entries with
``age_hours`` / ``local_check`` / ``file``). Locked to the legacy by
``tests/regression/test_dedupe_keeper_parity.py`` so the eventual migration is
verifiable, not a leap of faith.

The legacy delegates two pure helpers — ``has_sane_metadata`` (reads only
``file`` / ``video_duration`` / ``video_bitrate`` / ``video_codec`` off the
part) and ``bytes_to_string`` (pure formatting) — both re-implemented here so the
skip-reason strings match byte-for-byte.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

Part = Mapping[str, object]
Parts = Mapping[int, Part]


def _num(value: object, default: float) -> float:
    if isinstance(value, int | float) and not isinstance(value, bool):
        return float(value)
    return default


def _int(value: object) -> int:
    return int(value) if isinstance(value, int | float | str) else 0


def _int_or_zero(value: object) -> int:
    """Mirror the legacy ``int(value or 0)`` (falsy -> 0, else int())."""
    return _int(value) if value else 0


def _iter_files(value: object) -> list[object]:
    return list(value) if isinstance(value, list | tuple) else []


@dataclass(frozen=True, slots=True)
class KeeperPolicy:
    """All thresholds ``select_keeper`` reads from config, resolved once."""

    require_local_fs_access: bool
    min_file_age_hours: float
    find_duplicate_filepaths_only: bool
    min_score_difference: int
    max_size_ratio: float

    @classmethod
    def from_config(cls, cfg: Mapping[str, object]) -> KeeperPolicy:
        return cls(
            require_local_fs_access=bool(cfg.get("REQUIRE_LOCAL_FS_ACCESS")),
            min_file_age_hours=_num(cfg.get("MIN_FILE_AGE_HOURS", 0), 0.0),
            find_duplicate_filepaths_only=bool(cfg["FIND_DUPLICATE_FILEPATHS_ONLY"]),
            min_score_difference=int(_num(cfg.get("MIN_SCORE_DIFFERENCE", 0), 0.0)),
            max_size_ratio=_num(cfg.get("MAX_SIZE_RATIO", 0), 0.0),
        )


def bytes_to_string(size_bytes: object) -> str:
    """Pure mirror of ``plex_dupefinder.bytes_to_string`` for skip messages."""
    try:
        if size_bytes == 1:
            return "1 byte"
        suffixes_table = [("bytes", 0), ("KB", 0), ("MB", 1), ("GB", 2), ("TB", 2), ("PB", 2)]
        num = float(size_bytes)  # type: ignore[arg-type]
        suffix, precision = suffixes_table[0]
        for suffix, precision in suffixes_table:  # noqa: B007  (mirrors legacy break-on-match)
            if num < 1024.0:
                break
            num /= 1024.0
        formatted = f"{int(num)}" if precision == 0 else str(round(num, ndigits=precision))
        return f"{formatted} {suffix}"
    except Exception:  # noqa: S110  (formatting fallback; mirrors legacy)
        pass
    return f"{int(size_bytes)} bytes"  # type: ignore[call-overload]


def has_sane_metadata(part_info: Part) -> tuple[bool, str]:
    """Pure mirror of ``plex_dupefinder.has_sane_metadata``."""
    files = _iter_files(part_info.get("file"))
    if not files:
        return False, "empty file list"
    for f in files:
        if not f or not str(f).strip():
            return False, "empty file path"
    if _int_or_zero(part_info.get("video_duration")) <= 0:
        return False, "video_duration <= 0"
    if _int_or_zero(part_info.get("video_bitrate")) <= 0:
        return False, "video_bitrate <= 0"
    codec = str(part_info.get("video_codec") or "").strip().lower()
    if not codec or codec == "unknown":
        return False, "video_codec missing or Unknown"
    return True, ""


def _existence(part: Part) -> list[Mapping[str, object]]:
    value = part.get("parts_existence", [])
    return list(value) if isinstance(value, list) else []


def select_keeper(parts: Parts, policy: KeeperPolicy) -> dict[str, object]:
    """Apply all safety checks and return a decision dict.

    Fields: keeper_id, reason, skip, skip_reason, candidates_existing,
    top_score, second_score, score_delta, youngest_age_hours, max_size_ratio.
    """
    decision: dict[str, object] = {
        "keeper_id": None,
        "reason": None,
        "skip": False,
        "skip_reason": None,
        "candidates_existing": [],
        "top_score": None,
        "second_score": None,
        "score_delta": None,
        "youngest_age_hours": None,
        "max_size_ratio": None,
    }

    existing_ids = [mid for mid, pi in parts.items() if pi.get("exists", True)]
    decision["candidates_existing"] = existing_ids

    if not existing_ids:
        decision["skip"] = True
        decision["skip_reason"] = "no candidate exists on disk; Plex metadata may be stale"
        decision["reason"] = "all candidates missing"
        return decision

    if policy.require_local_fs_access:
        any_local = any(
            any(ex.get("local_check") is not None for ex in _existence(pi)) for pi in parts.values()
        )
        if not any_local:
            decision["skip"] = True
            decision["skip_reason"] = (
                "REQUIRE_LOCAL_FS_ACCESS=True but filesystem is not reachable from this host"
            )
            decision["reason"] = "fs not reachable"
            return decision

    # Metadata sanity: any existing candidate with obviously broken Plex
    # metadata means analysis is incomplete — too risky to decide.
    for mid in existing_ids:
        sane, why = has_sane_metadata(parts[mid])
        if not sane:
            decision["skip"] = True
            decision["skip_reason"] = (
                f"candidate {mid!r} has invalid metadata: {why} (Plex analysis may be incomplete)"
            )
            decision["reason"] = "metadata sanity check failed"
            return decision

    # Cooldown
    min_age_hours = policy.min_file_age_hours
    if min_age_hours > 0:
        youngest: float | None = None
        offender: tuple[int, object] | None = None
        for mid, pi in parts.items():
            for ex in _existence(pi):
                age = ex.get("age_hours")
                if age is None:
                    continue
                age_val = age  # preserve raw value/type, as the legacy does
                if youngest is None or age_val < youngest:  # type: ignore[operator]
                    youngest = age_val  # type: ignore[assignment]
                    offender = (mid, ex.get("file"))
        decision["youngest_age_hours"] = youngest
        if youngest is not None and youngest < min_age_hours and offender is not None:
            decision["skip"] = True
            decision["skip_reason"] = (
                f"cooldown: {offender[1]!r} is {youngest:.2f}h old, "
                f"below threshold {min_age_hours:.2f}h"
            )
            decision["reason"] = "cooldown protection"
            return decision

    if policy.find_duplicate_filepaths_only:
        by_id = sorted(((_int(parts[mid]["id"]), mid) for mid in existing_ids), key=lambda x: x[0])
        decision["keeper_id"] = by_id[0][1]
        decision["reason"] = "lowest media id among existing entries (paths-only mode)"
        return decision

    scored = sorted(
        ((_int(parts[mid]["score"]), mid) for mid in existing_ids),
        key=lambda x: x[0],
        reverse=True,
    )
    best_score, keeper_id = scored[0]
    decision["top_score"] = best_score

    if len(scored) > 1:
        second_score = scored[1][0]
        delta = best_score - second_score
        decision["second_score"] = second_score
        decision["score_delta"] = delta
        threshold = policy.min_score_difference
        if threshold > 0 and delta < threshold:
            decision["skip"] = True
            decision["skip_reason"] = f"score delta {delta:d} below threshold {threshold:d}"
            decision["reason"] = "score delta too small"
            return decision

    # Size-ratio sanity check
    max_ratio = policy.max_size_ratio
    if max_ratio > 0:
        keeper_raw = parts[keeper_id].get("file_size", 0) or 0
        keeper_size = _num(keeper_raw, 0.0)
        if keeper_size > 0:
            worst_ratio = 0.0
            size_offender: tuple[int, object] | None = None
            for mid in existing_ids:
                if mid == keeper_id:
                    continue
                other_raw = parts[mid].get("file_size", 0) or 0
                other_size = _num(other_raw, 0.0)
                if other_size <= 0:
                    continue
                ratio = other_size / keeper_size
                if ratio > worst_ratio:
                    worst_ratio = ratio
                    size_offender = (mid, other_raw)
            decision["max_size_ratio"] = worst_ratio or None
            if worst_ratio > max_ratio and size_offender is not None:
                decision["skip"] = True
                decision["skip_reason"] = (
                    f"size ratio {worst_ratio:.1f}x exceeds threshold {max_ratio:.1f}x "
                    f"(keeper={bytes_to_string(keeper_raw)}, "
                    f"sibling id={size_offender[0]!r}={bytes_to_string(size_offender[1])})"
                )
                decision["reason"] = "size ratio protection"
                return decision

    decision["keeper_id"] = keeper_id
    decision["reason"] = f"highest score ({best_score:d}) among existing files"
    return decision
