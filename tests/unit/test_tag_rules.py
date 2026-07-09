"""Tests for modules.arr.tag_rules (pure predicate evaluation, no I/O)."""

from __future__ import annotations

from modules.arr import tag_rules

_SAGA = tag_rules.DEFAULT_RULES


def test_default_rule_tags_only_collection_members() -> None:
    in_saga = {"id": 1, "collection": {"title": "Alien Collection", "tmdbId": 8091}}
    standalone = {"id": 2, "collection": None}
    assert tag_rules.desired_tags(in_saga, _SAGA) == {"saga"}
    assert tag_rules.desired_tags(standalone, _SAGA) == set()


def test_empty_conditions_never_tag_the_whole_library() -> None:
    bad_rule = [{"tag": "everything", "all": []}]
    assert tag_rules.desired_tags({"id": 1, "collection": {"x": 1}}, bad_rule) == set()


def test_composite_candidate_delete_ands_all_conditions() -> None:
    rules = [
        {
            "tag": "candidate_delete",
            "all": [{"imdb_lt": 5.5}, {"votes_gte": 1000}, {"has_collection": False}],
        }
    ]
    bad = {
        "id": 1,
        "collection": None,
        "ratings": {"imdb": {"value": 4.2, "votes": 5000}},
    }
    keep_saga = {**bad, "collection": {"tmdbId": 1}}  # in a saga → excluded
    too_few_votes = {**bad, "ratings": {"imdb": {"value": 4.2, "votes": 10}}}
    assert tag_rules.desired_tags(bad, rules) == {"candidate_delete"}
    assert tag_rules.desired_tags(keep_saga, rules) == set()
    assert tag_rules.desired_tags(too_few_votes, rules) == set()


def test_quality_predicates_read_moviefile() -> None:
    remux_4k = {
        "id": 1,
        "movieFile": {"quality": {"quality": {"name": "Remux-2160p", "resolution": 2160}}},
    }
    rules = [
        {"tag": "remux", "all": [{"is_remux": True}]},
        {"tag": "4k", "all": [{"resolution_gte": 2160}]},
    ]
    assert tag_rules.desired_tags(remux_4k, rules) == {"remux", "4k"}
    assert tag_rules.desired_tags({"id": 2, "movieFile": {}}, rules) == set()


def test_unknown_predicate_fails_closed() -> None:
    rules = [{"tag": "x", "all": [{"no_such_predicate": 1}]}]
    assert tag_rules.desired_tags({"id": 1, "collection": {"x": 1}}, rules) == set()
