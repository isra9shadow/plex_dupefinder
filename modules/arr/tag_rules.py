"""Pure rule evaluation for the Radarr tagger — no I/O.

A rule is ``{tag, all: [conditions]}``; every condition must hold (AND) for the tag
to apply. Each condition is a one-key dict ``{predicate: param}``. The predicate
vocabulary is deliberately small and reads ONLY fields Radarr already returns
(``collection``, ``ratings``, ``movieFile.quality``, ``year``, ``added``) — no extra
provider, no own database. Add a predicate here when a real need appears; the
evaluator stays a pure function so it is fully testable offline.

Default (when config supplies no rules): tag movies that belong to any TMDb
collection with ``saga`` — the actual problem this feature solves.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime

# The one rule that ships by default: "belongs to a collection" → saga.
DEFAULT_RULES: tuple[dict[str, object], ...] = ({"tag": "saga", "all": [{"has_collection": True}]},)


# --- field extractors (tolerant: missing/odd shapes → neutral) ------------------


def _num(value: object) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _rating(movie: Mapping[str, object], source: str) -> float | None:
    ratings = movie.get("ratings")
    if isinstance(ratings, Mapping):
        entry = ratings.get(source)
        if isinstance(entry, Mapping):
            return _num(entry.get("value"))
    return None


def _max_votes(movie: Mapping[str, object]) -> int:
    ratings = movie.get("ratings")
    best = 0
    if isinstance(ratings, Mapping):
        for entry in ratings.values():
            if isinstance(entry, Mapping):
                votes = entry.get("votes")
                if isinstance(votes, int) and not isinstance(votes, bool):
                    best = max(best, votes)
    return best


def _quality(movie: Mapping[str, object]) -> Mapping[str, object]:
    mf = movie.get("movieFile")
    if isinstance(mf, Mapping):
        q = mf.get("quality")
        if isinstance(q, Mapping):
            inner = q.get("quality")
            if isinstance(inner, Mapping):
                return inner
    return {}


def _months_since_added(
    movie: Mapping[str, object], *, now: datetime | None = None
) -> float | None:
    raw = movie.get("added")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        added = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if added.tzinfo is None:
        added = added.replace(tzinfo=UTC)
    ref = now or datetime.now(UTC)
    return (ref - added).days / 30.0


# --- predicates: (movie, param) -> bool ----------------------------------------


def _has_collection(movie: Mapping[str, object], want: object) -> bool:
    return bool(movie.get("collection")) == bool(want)


def _rating_lt(source: str) -> Callable[[Mapping[str, object], object], bool]:
    def check(movie: Mapping[str, object], param: object) -> bool:
        threshold, value = _num(param), _rating(movie, source)
        return threshold is not None and value is not None and value < threshold

    return check


def _votes_gte(movie: Mapping[str, object], param: object) -> bool:
    threshold = _num(param)
    return threshold is not None and _max_votes(movie) >= threshold


def _is_remux(movie: Mapping[str, object], want: object) -> bool:
    q = _quality(movie)
    text = f"{q.get('name', '')} {q.get('source', '')}".lower()
    return ("remux" in text) == bool(want)


def _resolution_gte(movie: Mapping[str, object], param: object) -> bool:
    threshold = _num(param)
    res = _num(_quality(movie).get("resolution"))
    return threshold is not None and res is not None and res >= threshold


def _year_lt(movie: Mapping[str, object], param: object) -> bool:
    threshold, year = _num(param), _num(movie.get("year"))
    return threshold is not None and year is not None and year < threshold


def _added_before_months(movie: Mapping[str, object], param: object) -> bool:
    threshold, months = _num(param), _months_since_added(movie)
    return threshold is not None and months is not None and months >= threshold


def _title_contains_any(movie: Mapping[str, object], param: object) -> bool:
    """True if the movie title contains any of the given substrings (case-insensitive).

    Escape hatch for franchises TMDb has NO collection for (e.g. ["Hellboy",
    "Daredevil", "Blade"]) — list their names and they get the tag too.
    """
    if not isinstance(param, (list, tuple)):
        return False
    title = str(movie.get("title", "")).lower()
    return any(isinstance(s, str) and s.strip() and s.strip().lower() in title for s in param)


_PREDICATES: dict[str, Callable[[Mapping[str, object], object], bool]] = {
    "has_collection": _has_collection,
    "imdb_lt": _rating_lt("imdb"),
    "tmdb_lt": _rating_lt("tmdb"),
    "rotten_lt": _rating_lt("rottenTomatoes"),
    "votes_gte": _votes_gte,
    "is_remux": _is_remux,
    "resolution_gte": _resolution_gte,
    "year_lt": _year_lt,
    "added_before_months": _added_before_months,
    "title_contains_any": _title_contains_any,
}


def known_predicates() -> frozenset[str]:
    """The supported predicate names (for config validation / docs)."""
    return frozenset(_PREDICATES)


def _condition_holds(movie: Mapping[str, object], cond: Mapping[str, object]) -> bool:
    # A condition is one (or more) predicate keys, all of which must hold.
    for name, param in cond.items():
        fn = _PREDICATES.get(name)
        if fn is None or not fn(movie, param):  # unknown predicate → fail closed
            return False
    return True


# --- franchise clustering (Phase A) + LLM verification prompt (Phase B) ---------

_ROMAN = frozenset({"ii", "iii", "iv", "v", "vi", "vii", "viii", "ix", "x", "xi", "xii"})
_TRAIL = frozenset({"part", "parte", "chapter", "capitulo", "vol", "volume"})


def normalize_title(title: str) -> str:
    """Reduce a title to a franchise 'stem' for grouping (deterministic).

    'Hellboy II: The Golden Army' / 'Hellboy (2019)' / 'Hellboy 2' → 'hellboy'.
    Over-grouping is harmless here (it only over-*protects* from deletion).
    """
    text = title.lower()
    for sep in (":", " - ", " – ", " — "):  # noqa: RUF001 - en/em dashes are intentional
        if sep in text:
            text = text.split(sep, 1)[0]
    text = re.sub(r"[(\[]\s*(?:19|20)\d{2}\s*[)\]]", " ", text)  # drop a (2019) year
    text = re.sub(r"[^\w\s]", " ", text)  # punctuation → space
    words = text.split()
    while words and (words[-1].isdigit() or words[-1] in _ROMAN or words[-1] in _TRAIL):
        words.pop()
    return " ".join(words).strip()


def franchise_groups(
    movies: Sequence[Mapping[str, object]], min_group: int = 2
) -> list[tuple[str, list[int], list[str]]]:
    """Group movies by shared title stem; return ``(stem, ids, titles)`` for groups
    with at least ``min_group`` members (candidate franchises TMDb may lack)."""
    groups: dict[str, tuple[list[int], list[str]]] = defaultdict(lambda: ([], []))
    for movie in movies:
        mid, title = movie.get("id"), movie.get("title")
        if isinstance(mid, int) and not isinstance(mid, bool) and isinstance(title, str):
            stem = normalize_title(title)
            if len(stem) >= 2:
                groups[stem][0].append(mid)
                groups[stem][1].append(title)
    return [
        (stem, ids, titles)
        for stem, (ids, titles) in sorted(groups.items())
        if len(ids) >= min_group
    ]


def build_cluster_prompt(titles: Sequence[str]) -> str:
    """Prompt asking the local LLM to confirm a candidate group IS one franchise."""
    listed = "\n".join(f"- {t}" for t in titles)
    return (
        "¿Estas películas pertenecen a la MISMA saga/franquicia cinematográfica "
        "(secuelas, precuelas o reinicios de la misma serie)? "
        "Responde SOLO con una palabra: SI o NO.\n\n"
        f"{listed}\n\nRespuesta:"
    )


def is_affirmative(text: str) -> bool:
    """True if the LLM answer is a yes (tolerant of accents/casing/extra words)."""
    return (text or "").strip().lower().startswith(("si", "sí", "yes", "true"))


def desired_tags(movie: Mapping[str, object], rules: Sequence[Mapping[str, object]]) -> set[str]:
    """The set of (bare) tag names ``movie`` should carry per ``rules``.

    A rule with no conditions is ignored (never tags the whole library by accident).
    """
    out: set[str] = set()
    for rule in rules:
        tag = rule.get("tag")
        conds = rule.get("all")
        if not isinstance(tag, str) or not tag or not isinstance(conds, Sequence) or not conds:
            continue
        if all(_condition_holds(movie, c) for c in conds if isinstance(c, Mapping)):
            out.add(tag)
    return out
