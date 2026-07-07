"""Tests for the external sentinel watchdog (pure config/state/sweep, no network)."""

from __future__ import annotations

from pathlib import Path

import sentinel

# --- config --------------------------------------------------------------------


def test_parse_env_file_tolerates_quotes_comments_blanks(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text(
        '# comment\n\nIZUMI_TELEGRAM_CHAT_ID = "5320967"\nTOK=abc\nbad line\n',
        encoding="utf-8",
    )
    data = sentinel.parse_env_file(env)
    assert data["IZUMI_TELEGRAM_CHAT_ID"] == "5320967"
    assert data["TOK"] == "abc"
    assert "bad line" not in data


def test_parse_env_file_missing_is_empty(tmp_path: Path) -> None:
    assert sentinel.parse_env_file(tmp_path / "nope.env") == {}


def test_get_conf_prefers_real_env(monkeypatch) -> None:
    monkeypatch.setenv("X_FOO", "from-env")
    assert sentinel.get_conf("X_FOO", {"X_FOO": "from-file"}) == "from-env"
    monkeypatch.delenv("X_FOO", raising=False)
    assert sentinel.get_conf("X_FOO", {"X_FOO": "from-file"}) == "from-file"
    assert sentinel.get_conf("X_MISS", {}, default="d") == "d"


# --- target parsing -------------------------------------------------------------


def test_parse_target_http() -> None:
    t = sentinel.parse_target("overseerr=https://overseerr.example.com")
    assert t is not None and t.kind == "http" and t.url == "https://overseerr.example.com"
    assert t.name == "overseerr"


def test_parse_target_tcp_with_name() -> None:
    t = sentinel.parse_target("plex=192.168.6.62:32400")
    assert t is not None and t.kind == "tcp" and t.host == "192.168.6.62" and t.port == 32400


def test_parse_target_bare_host_uses_default_port() -> None:
    t = sentinel.parse_target("192.168.6.62", default_port=443)
    assert t is not None and t.kind == "tcp" and t.port == 443


def test_parse_target_rejects_garbage() -> None:
    assert sentinel.parse_target("") is None
    assert sentinel.parse_target("no-port-no-default") is None


def test_parse_targets_skips_bad_entries() -> None:
    targets = sentinel.parse_targets("plex=10.0.0.1:32400, , broken, web=https://x")
    assert [t.name for t in targets] == ["plex", "web"]


# --- state machine --------------------------------------------------------------


def test_update_state_debounces_then_alerts_down() -> None:
    state: dict = {}
    state, ev = sentinel.update_state(state, "plex", ok=False, threshold=3)
    assert ev is None  # 1st failure, below threshold
    state, ev = sentinel.update_state(state, "plex", ok=False, threshold=3)
    assert ev is None  # 2nd
    state, ev = sentinel.update_state(state, "plex", ok=False, threshold=3)
    assert ev == "down"  # crosses threshold
    state, ev = sentinel.update_state(state, "plex", ok=False, threshold=3)
    assert ev is None  # already down -> no repeat


def test_update_state_recovers_up() -> None:
    state, _ = sentinel.update_state({}, "plex", ok=False, threshold=1)
    state, ev = sentinel.update_state(state, "plex", ok=True, threshold=1)
    assert ev == "up"
    state, ev = sentinel.update_state(state, "plex", ok=True, threshold=1)
    assert ev is None  # stable up -> silent


def test_format_event_variants() -> None:
    assert "🔴" in sentinel.format_event("plex", "down")
    assert "🟢" in sentinel.format_event("plex", "up")
    assert "🚨" in sentinel.format_event("Servidor", "down", is_server=True)
    assert sentinel.format_event("plex", None) == ""


# --- sweep ----------------------------------------------------------------------


def _prober(down_names):
    return lambda target: target.name not in down_names


def test_sweep_server_down_skips_services() -> None:
    server = sentinel.Target("Servidor", "tcp", "10.0.0.1", 443)
    targets = [sentinel.Target("plex", "tcp", "10.0.0.1", 32400)]
    state: dict = {}
    # threshold 1 -> the server's first failure trips it
    state, msgs = sweep_n(server, targets, state, down={"Servidor", "plex"}, threshold=1)
    assert len(msgs) == 1 and "🚨" in msgs[0]
    assert "plex" not in state  # services were not probed while the server is down


def test_sweep_reports_individual_service_when_server_up() -> None:
    server = sentinel.Target("Servidor", "tcp", "10.0.0.1", 443)
    targets = [
        sentinel.Target("plex", "tcp", "10.0.0.1", 32400),
        sentinel.Target("sonarr", "tcp", "10.0.0.1", 8989),
    ]
    _state, msgs = sweep_n(server, targets, {}, down={"sonarr"}, threshold=1)
    assert msgs == ["🔴 Servicio caído: sonarr"]


def sweep_n(server, targets, state, *, down, threshold):
    return sentinel.sweep(server, targets, state, threshold=threshold, prober=_prober(down))


# --- panel status pull ----------------------------------------------------------


def test_diff_module_status_alerts_new_failures_with_count() -> None:
    snap = {
        "modules": [
            {"module": "dbcheck", "ok": False, "failures": 2},
            {"module": "uptime", "ok": True, "failures": 0},
        ]
    }
    failing, msgs = sentinel.diff_module_status(set(), snap)
    assert failing == {"dbcheck"}
    assert len(msgs) == 1 and "dbcheck" in msgs[0] and "2 fallos" in msgs[0]


def test_diff_module_status_no_repeat_and_recovery() -> None:
    snap_bad = {"modules": [{"module": "dbcheck", "ok": False, "failures": 1}]}
    failing, _ = sentinel.diff_module_status(set(), snap_bad)
    # same failure next sweep -> no new alert (guardian isn't spammed every minute)
    failing, msgs = sentinel.diff_module_status(failing, snap_bad)
    assert msgs == []
    # module recovers -> one recovery line
    snap_ok = {"modules": [{"module": "dbcheck", "ok": True, "failures": 0}]}
    failing, msgs = sentinel.diff_module_status(failing, snap_ok)
    assert failing == set() and "recuperado" in msgs[0].lower()


def test_diff_module_status_handles_garbage() -> None:
    assert sentinel.diff_module_status(set(), {}) == (set(), [])
    assert sentinel.diff_module_status(set(), {"modules": None}) == (set(), [])
