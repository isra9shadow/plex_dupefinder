"""Tests for core.configedit (pure logic behind the menu config editor)."""

from __future__ import annotations

from core.configedit import coerce_value, is_secret, redact, upsert_env_line
from core.configspec import SettingSpec, Validator


def _spec(key: str, validator: Validator, **kw: object) -> SettingSpec:
    location = kw.pop("location", key)
    return SettingSpec(key=key, location=str(location), validator=validator, **kw)  # type: ignore[arg-type]


# --- coerce_value --------------------------------------------------------------


def test_coerce_int_ok_and_bad() -> None:
    spec = _spec("uptime.timeout", Validator.INT)
    assert coerce_value(spec, " 5 ") == (True, 5, "")
    ok, value, detail = coerce_value(spec, "abc")
    assert ok is False and value is None and "entero" in detail


def test_coerce_enum() -> None:
    spec = _spec("safety.mode", Validator.ENUM, choices=("dry_run", "audit", "live"))
    assert coerce_value(spec, "live") == (True, "live", "")
    ok, _, detail = coerce_value(spec, "nope")
    assert ok is False and "uno de" in detail


def test_coerce_url() -> None:
    spec = _spec("radarr.url", Validator.URL)
    assert coerce_value(spec, "http://host:7878")[0] is True
    ok, _, detail = coerce_value(spec, "host:7878")
    assert ok is False and "URL" in detail


def test_coerce_empty_rejected() -> None:
    spec = _spec("x", Validator.NONEMPTY)
    ok, value, detail = coerce_value(spec, "   ")
    assert ok is False and value is None and "vacío" in detail


def test_coerce_nonempty_trims() -> None:
    spec = _spec("ollama.model", Validator.NONEMPTY)
    assert coerce_value(spec, "  qwen3:8b ") == (True, "qwen3:8b", "")


def test_coerce_path_is_advisory() -> None:
    spec = _spec("paths.media_root", Validator.DIR_EXISTS)
    # Missing path: accepted but warned.
    ok, value, detail = coerce_value(spec, "/nope", path_exists=lambda p: False)
    assert ok is True and value == "/nope" and "no existe" in detail
    # Existing path: accepted, no warning.
    assert coerce_value(spec, "/data", path_exists=lambda p: True) == (True, "/data", "")
    # No checker injected: accepted silently.
    assert coerce_value(spec, "/data") == (True, "/data", "")


# --- upsert_env_line -----------------------------------------------------------


def test_upsert_replaces_existing_key() -> None:
    text = "# creds\nRADARR_API_KEY=old\nSONARR_API_KEY=keep\n"
    out = upsert_env_line(text, "RADARR_API_KEY", "new")
    assert "RADARR_API_KEY=new" in out
    assert "RADARR_API_KEY=old" not in out
    assert "SONARR_API_KEY=keep" in out  # untouched
    assert "# creds" in out  # comment preserved
    assert out.endswith("\n")


def test_upsert_appends_when_absent() -> None:
    out = upsert_env_line("FOO=1\n", "BAR", "2")
    assert out == "FOO=1\nBAR=2\n"


def test_upsert_ignores_commented_key() -> None:
    text = "# RADARR_API_KEY=commented\n"
    out = upsert_env_line(text, "RADARR_API_KEY", "real")
    assert "# RADARR_API_KEY=commented" in out  # comment kept
    assert "RADARR_API_KEY=real" in out  # appended, not overwritten


def test_upsert_replaces_only_first_match() -> None:
    text = "K=a\nK=b\n"
    out = upsert_env_line(text, "K", "c")
    assert out == "K=c\nK=b\n"


def test_upsert_empty_file() -> None:
    assert upsert_env_line("", "K", "v") == "K=v\n"


# --- secrets -------------------------------------------------------------------


def test_is_secret_only_for_env_credentials() -> None:
    assert is_secret(_spec("RADARR_API_KEY", Validator.NONEMPTY, location="env")) is True
    assert is_secret(_spec("IZUMI_TELEGRAM_BOT_TOKEN", Validator.NONEMPTY, location="env")) is True
    assert is_secret(_spec("QBIT_PASSWORD", Validator.NONEMPTY, location="env")) is True
    # Non-secret env var and any config.json path are not secrets.
    assert is_secret(_spec("IZUMI_SENTINEL_SERVER", Validator.NONEMPTY, location="env")) is False
    url_spec = _spec("radarr.url", Validator.URL, location="integrations.radarr.url")
    assert is_secret(url_spec) is False


def test_redact_keeps_last_four() -> None:
    assert redact("") == ""
    assert redact("ab") == "••"
    assert redact("abcdefgh") == "••••efgh"
