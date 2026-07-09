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


def test_title_contains_any_covers_tmdb_collection_gaps() -> None:
    rules = [{"tag": "saga", "all": [{"title_contains_any": ["Hellboy", "Daredevil"]}]}]
    assert tag_rules.desired_tags({"title": "Hellboy II: The Golden Army"}, rules) == {"saga"}
    assert tag_rules.desired_tags({"title": "daredevil"}, rules) == {"saga"}  # case-insensitive
    assert tag_rules.desired_tags({"title": "The Matrix"}, rules) == set()
    assert (
        tag_rules.desired_tags(
            {"title": "X"}, [{"tag": "s", "all": [{"title_contains_any": "no"}]}]
        )
        == set()
    )


def test_unknown_predicate_fails_closed() -> None:
    rules = [{"tag": "x", "all": [{"no_such_predicate": 1}]}]
    assert tag_rules.desired_tags({"id": 1, "collection": {"x": 1}}, rules) == set()


def test_normalize_title_reduces_to_franchise_stem() -> None:
    n = tag_rules.normalize_title
    assert n("Hellboy II: The Golden Army") == "hellboy"
    assert n("Hellboy (2019)") == "hellboy"
    assert n("Rocky IV") == "rocky"
    assert n("Scream 2") == "scream"


def test_franchise_groups_finds_shared_stems() -> None:
    movies = [
        {"id": 1, "title": "Hellboy"},
        {"id": 2, "title": "Hellboy II: The Golden Army"},
        {"id": 3, "title": "The Matrix"},  # alone → not a group
    ]
    groups = tag_rules.franchise_groups(movies, 2)
    assert {stem for stem, _ids, _titles in groups} == {"hellboy"}
    ids = next(ids for stem, ids, _t in groups if stem == "hellboy")
    assert sorted(ids) == [1, 2]


def test_cluster_prompt_and_affirmative_parsing() -> None:
    prompt = tag_rules.build_cluster_prompt(["Hellboy", "Hellboy II"])
    assert "Hellboy II" in prompt and "SI o NO" in prompt
    assert tag_rules.is_affirmative("SÍ, son la misma saga")
    assert tag_rules.is_affirmative("yes")
    assert not tag_rules.is_affirmative("No, no lo son")
