"""Tests for core.configspec (pure, offline evaluation of the settings registry)."""

from __future__ import annotations

from core import configspec
from core.configspec import SettingSpec, Status, Validator


def _spec(**kw: object) -> SettingSpec:
    base: dict[str, object] = {
        "key": "k",
        "location": "env",
        "validator": Validator.NONEMPTY,
    }
    base.update(kw)
    return SettingSpec(**base)  # type: ignore[arg-type]


# --- resolution ---------------------------------------------------------------


def test_resolve_env_treats_empty_as_unset() -> None:
    spec = _spec(key="X", location="env")
    assert configspec.resolve(spec, {"X": ""}, {}) is None
    assert configspec.resolve(spec, {"X": "v"}, {}) == "v"
    assert configspec.resolve(spec, {}, {}) is None


def test_resolve_dotted_config_path() -> None:
    spec = _spec(key="radarr.url", location="integrations.radarr.url")
    config = {"integrations": {"radarr": {"url": "http://h:7878"}}}
    assert configspec.resolve(spec, {}, config) == "http://h:7878"


def test_resolve_dotted_missing_hop_is_none() -> None:
    spec = _spec(key="radarr.url", location="integrations.radarr.url")
    assert configspec.resolve(spec, {}, {"integrations": {}}) is None
    assert configspec.resolve(spec, {}, {}) is None


def test_resolve_dotted_through_list_index() -> None:
    spec = _spec(key="x", location="a.b.1.c")
    config = {"a": {"b": [{"c": "no"}, {"c": "yes"}]}}
    assert configspec.resolve(spec, {}, config) == "yes"


# --- validators ---------------------------------------------------------------


def test_nonempty_missing_required_is_missing() -> None:
    spec = _spec(key="X", location="env", required=True, validator=Validator.NONEMPTY)
    st = configspec.evaluate_setting(spec, {}, {})
    assert st.status is Status.MISSING
    assert st.value is None


def test_nonempty_optional_missing_is_ok_with_default() -> None:
    spec = _spec(key="X", location="env", required=False, default="dft")
    st = configspec.evaluate_setting(spec, {}, {})
    assert st.status is Status.OK
    assert st.value == "dft"


def test_int_validator_accepts_int_and_numeric_string() -> None:
    spec = _spec(key="n", location="a.n", validator=Validator.INT)
    assert configspec.evaluate_setting(spec, {}, {"a": {"n": 7}}).status is Status.OK
    assert configspec.evaluate_setting(spec, {}, {"a": {"n": "7"}}).status is Status.OK


def test_int_validator_accepts_integer_valued_floats() -> None:
    # core/config mirrors numeric config as floats, so 15 arrives as 15.0 / "15.0".
    spec = _spec(key="n", location="a.n", validator=Validator.INT)
    assert configspec.evaluate_setting(spec, {}, {"a": {"n": 15.0}}).status is Status.OK
    assert configspec.evaluate_setting(spec, {}, {"a": {"n": "15.0"}}).status is Status.OK


def test_int_validator_rejects_fractional_floats() -> None:
    spec = _spec(key="n", location="a.n", validator=Validator.INT)
    bad = configspec.evaluate_setting(spec, {}, {"a": {"n": 15.5}})
    assert bad.status is Status.INVALID
    assert "not an integer" in bad.detail


def test_int_validator_rejects_bool_and_text() -> None:
    spec = _spec(key="n", location="a.n", validator=Validator.INT)
    bad_bool = configspec.evaluate_setting(spec, {}, {"a": {"n": True}})
    assert bad_bool.status is Status.INVALID
    bad_text = configspec.evaluate_setting(spec, {}, {"a": {"n": "x"}})
    assert bad_text.status is Status.INVALID
    assert "not an integer" in bad_text.detail


def test_url_validator() -> None:
    spec = _spec(key="u", location="a.u", validator=Validator.URL)
    assert configspec.evaluate_setting(spec, {}, {"a": {"u": "http://h:80"}}).status is Status.OK
    assert configspec.evaluate_setting(spec, {}, {"a": {"u": "https://x.io/p"}}).status is Status.OK
    bad = configspec.evaluate_setting(spec, {}, {"a": {"u": "ftp://h"}})
    assert bad.status is Status.INVALID
    assert "URL" in bad.detail


def test_enum_validator() -> None:
    spec = _spec(key="m", location="a.m", validator=Validator.ENUM, choices=("dry_run", "live"))
    assert configspec.evaluate_setting(spec, {}, {"a": {"m": "live"}}).status is Status.OK
    bad = configspec.evaluate_setting(spec, {}, {"a": {"m": "nope"}})
    assert bad.status is Status.INVALID
    assert "one of" in bad.detail


def test_path_and_dir_exists_use_injected_checkers() -> None:
    pspec = _spec(key="p", location="a.p", validator=Validator.PATH_EXISTS)
    dspec = _spec(key="d", location="a.d", validator=Validator.DIR_EXISTS)
    cfg = {"a": {"p": "/some/file", "d": "/some/dir"}}

    def path_exists(p: str) -> bool:
        return p == "/some/file"

    def dir_exists(p: str) -> bool:
        return p == "/some/dir"

    p_ok = configspec.evaluate_setting(
        pspec, {}, cfg, path_exists=path_exists, dir_exists=dir_exists
    )
    d_ok = configspec.evaluate_setting(
        dspec, {}, cfg, path_exists=path_exists, dir_exists=dir_exists
    )
    assert p_ok.status is Status.OK
    assert d_ok.status is Status.OK

    # dir_exists falls back to path_exists when not provided
    fallback = configspec.evaluate_setting(dspec, {}, cfg, path_exists=path_exists)
    assert fallback.status is Status.INVALID  # path_exists says /some/dir does not exist


def test_dir_exists_default_checker_is_offline_false() -> None:
    spec = _spec(key="d", location="a.d", validator=Validator.DIR_EXISTS, required=False)
    st = configspec.evaluate_setting(spec, {}, {"a": {"d": "/nope"}})
    # present but the default checker treats every path as absent -> INVALID
    assert st.status is Status.INVALID
    assert "does not exist" in st.detail


# --- registry + grouping ------------------------------------------------------


def test_registry_keys_are_unique() -> None:
    keys = [s.key for s in configspec.SPECS]
    assert len(keys) == len(set(keys))


def test_registry_covers_known_settings() -> None:
    keys = {s.key for s in configspec.SPECS}
    assert "PLEX_TOKEN" in keys
    assert "RADARR_API_KEY" in keys
    assert "safety.mode" in keys
    assert "radarr.url" in keys
    assert "ollama.base_url" in keys


def test_every_spec_has_a_remediation_hint() -> None:
    assert all(s.how.strip() for s in configspec.SPECS)


def test_evaluate_all_and_group() -> None:
    statuses = configspec.evaluate({}, {})
    grouped = configspec.by_status(statuses)
    assert set(grouped) == set(Status)
    # required env secrets with no value -> MISSING
    missing_keys = {st.spec.key for st in grouped[Status.MISSING]}
    assert "PLEX_TOKEN" in missing_keys


def test_groups_and_specs_for_group() -> None:
    assert "radarr" in configspec.groups()
    radarr = configspec.specs_for_group("radarr")
    assert {s.key for s in radarr} == {"RADARR_API_KEY", "radarr.url"}
