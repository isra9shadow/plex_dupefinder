#!/usr/bin/env python3
"""izumi sentinel — external dead-man's-switch watchdog (runs OFF the server).

Meant to run on a secondary, always-on PC on the SAME LAN as the Unraid server
(e.g. a Windows box). It periodically probes the server and its key services and
alerts you over Telegram on state changes — crucially, when the SERVER ITSELF is
unreachable, so you still get told even when the main bot (which lives on the
server) is down too.

It only SENDS Telegram messages (never getUpdates), so it can safely reuse the
same bot token as ``bot.py`` without clashing with the bot's long-poll.

Self-contained: standard library only (urllib + socket), so you can drop this one
file on the secondary PC. Configuration comes from the environment or a ``.env``
file sitting next to this script.

Config (env / .env next to this file):
  IZUMI_TELEGRAM_BOT_TOKEN   (required) reusing the main bot's token is fine
  IZUMI_TELEGRAM_CHAT_ID     (required) where to send alerts
  IZUMI_SENTINEL_SERVER      the server's master check, "host:port"
                             (default port 443 — the Unraid web UI)
  IZUMI_SENTINEL_TARGETS     comma list of services:
                               "name=host:port" (TCP)  or  "name=https://url" (HTTP)
  IZUMI_SENTINEL_INTERVAL    seconds between sweeps (default 60)
  IZUMI_SENTINEL_FAILS       consecutive failures before a DOWN alert (default 3)
  IZUMI_SENTINEL_TIMEOUT     per-probe timeout in seconds (default 8)
  IZUMI_SENTINEL_PANEL       optional webui base URL (e.g. http://192.168.6.62:8888);
                             when set and the server is up, the sentinel pulls
                             ``/api/status`` and tells you WHICH modules are failing
  IZUMI_SENTINEL_PANEL_AUTH  optional "user:pass" for the panel's HTTP Basic Auth

Run on Windows (stays open):   python sentinel.py
Background / at logon:         pythonw sentinel.py   (or a Scheduled Task)
"""

from __future__ import annotations

import base64
import json
import os
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

_API = "https://api.telegram.org/bot{token}/{method}"
_DEFAULT_INTERVAL = 60.0
_DEFAULT_FAILS = 3
_DEFAULT_TIMEOUT = 8.0
_SERVER_NAME = "Servidor Unraid"


# --- configuration (env / .env) -------------------------------------------------


def parse_env_file(path):
    """Parse a simple KEY=VALUE ``.env`` (quotes/space tolerant); {} if missing."""
    data: dict[str, str] = {}
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError:
        return data
    for line in raw.splitlines():
        text = line.strip()
        if not text or text.startswith("#") or "=" not in text:
            continue
        key, _, value = text.partition("=")
        cleaned = value.strip().strip('"').strip("'")
        if key.strip():
            data[key.strip()] = cleaned
    return data


def get_conf(name, env_file, *, default=None):
    """Resolve a setting: real environment first, then the ``.env`` fallback."""
    if os.environ.get(name):
        return os.environ[name]
    return env_file.get(name, default)


# --- targets --------------------------------------------------------------------


@dataclass(frozen=True)
class Target:
    """One thing to probe: a TCP host:port or an HTTP(S) URL."""

    name: str
    kind: str  # "tcp" | "http"
    host: str = ""
    port: int = 0
    url: str = ""


def parse_target(spec, *, default_port=0):
    """Parse one "name=host:port" / "name=https://url" / "host:port" spec -> Target.

    Returns None if it cannot be understood (so a typo never crashes the loop).
    """
    text = (spec or "").strip()
    if not text:
        return None
    if "=" in text:
        name, _, rest = text.partition("=")
        name = name.strip()
        rest = rest.strip()
    else:
        name, rest = text, text
    if not rest:
        return None
    if rest.startswith(("http://", "https://")):
        return Target(name=name or rest, kind="http", url=rest)
    host, sep, port_s = rest.rpartition(":")
    if sep and host and port_s.isdigit():
        return Target(name=name or rest, kind="tcp", host=host, port=int(port_s))
    if default_port:  # bare host -> use the default port
        return Target(name=name or rest, kind="tcp", host=rest, port=default_port)
    return None


def parse_targets(raw):
    """Parse the comma-separated IZUMI_SENTINEL_TARGETS list (skips bad entries)."""
    out: list[Target] = []
    for chunk in (raw or "").split(","):
        target = parse_target(chunk)
        if target is not None:
            out.append(target)
    return out


# --- state machine (pure, unit-tested) -----------------------------------------


def update_state(state, name, ok, *, threshold):
    """Fold one probe result into the per-target state; return (state, event).

    ``event`` is "down" on the failing sweep that crosses ``threshold`` (debounced),
    "up" the first time a down target recovers, else None. ``state`` maps each
    name to ``{"fails": int, "down": bool}``; a fresh copy is returned (pure).
    """
    new_state = {key: dict(value) for key, value in state.items()}
    entry = new_state.setdefault(name, {"fails": 0, "down": False})
    event = None
    if ok:
        if entry["down"]:
            event = "up"
        entry["fails"] = 0
        entry["down"] = False
    else:
        entry["fails"] += 1
        if not entry["down"] and entry["fails"] >= threshold:
            entry["down"] = True
            event = "down"
    return new_state, event


def format_event(name, event, *, is_server=False):
    """Human Telegram line for a state transition."""
    if event == "down":
        if is_server:
            return (
                f"🚨 {name} NO RESPONDE — el servidor (y por tanto el bot) parece caído. "
                "Revisa alimentación/red del servidor."
            )
        return f"🔴 Servicio caído: {name}"
    if event == "up":
        return f"🟢 Recuperado: {name}"
    return ""


# --- panel status pull (know WHAT failed, not just that the box is up) ----------


def format_module_alert(module, failures):
    """Human Telegram line for a module that started failing (with its count)."""
    n = _int(failures, 0)
    detail = f" ({n} fallo{'s' if n != 1 else ''})" if n else ""
    return f"⚠️ Módulo con fallos: {module}{detail} — pídele el arreglo en el panel/bot."


def diff_module_status(known_failing, snapshot):
    """Compare a ``/api/status`` snapshot against already-alerted modules (pure).

    ``snapshot`` is the JSON from ``webui``'s ``/api/status`` (``{modules:[{module,
    ok, failures}], ...}``). Returns ``(failing_now, messages)``: one message per
    NEWLY failing module (with its failure count) and one per recovered module, so
    the guardian is told exactly what broke without re-alerting every sweep.
    """
    modules = snapshot.get("modules") if isinstance(snapshot, dict) else None
    by_name: dict[str, dict[str, object]] = {}
    for item in modules or []:
        if isinstance(item, dict) and item.get("module"):
            by_name[str(item["module"])] = item
    failing_now = {name for name, item in by_name.items() if not item.get("ok")}
    messages: list[str] = []
    for name in sorted(failing_now - set(known_failing)):
        messages.append(format_module_alert(name, by_name[name].get("failures")))
    for name in sorted(set(known_failing) - failing_now):
        messages.append(f"🟢 Módulo recuperado: {name}")
    return failing_now, messages


def fetch_status(panel_url, *, timeout, auth=None):
    """GET ``<panel_url>/api/status`` and return the parsed dict, or None on failure.

    ``auth`` is an optional ``"user:pass"`` for the panel's HTTP Basic Auth.
    """
    url = panel_url.rstrip("/") + "/api/status"
    request = urllib.request.Request(url, method="GET")
    if auth:
        request.add_header(
            "Authorization", "Basic " + base64.b64encode(auth.encode("utf-8")).decode("ascii")
        )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if response.status != 200:
                return None
            data = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


# --- probes (I/O) ---------------------------------------------------------------


def probe_tcp(host, port, *, timeout):
    """True if a TCP connection to host:port succeeds within ``timeout``."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def probe_http(url, *, timeout):
    """True if an HTTP(S) GET returns a status below 500 (the service answered)."""
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status < 500
    except urllib.error.HTTPError as exc:
        return exc.code < 500  # a 4xx still means the service is up and answering
    except (urllib.error.URLError, OSError, ValueError):
        return False


def probe(target, *, timeout):
    """Dispatch a probe by target kind."""
    if target.kind == "http":
        return probe_http(target.url, timeout=timeout)
    return probe_tcp(target.host, target.port, timeout=timeout)


# --- Telegram (send-only) -------------------------------------------------------


def send_telegram(token, chat_id, text, *, timeout=15.0):
    """Send one Telegram message (best-effort; returns True on success)."""
    url = _API.format(token=token, method="sendMessage")
    data = json.dumps({"chat_id": chat_id, "text": text}).encode("utf-8")
    request = urllib.request.Request(
        url, data=data, headers={"content-type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout):
            return True
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print(f"[sentinel] no pude enviar a Telegram: {exc}")
        return False


# --- sweep + serve --------------------------------------------------------------


def sweep(server, targets, state, *, threshold, prober):
    """One monitoring pass. Returns (state, messages).

    The server is checked first; if it is down, services are NOT probed (they are
    all unreachable anyway) so you get ONE clear alarm instead of a flood.
    """
    messages: list[str] = []
    server_down = False
    if server is not None:
        state, event = update_state(state, server.name, prober(server), threshold=threshold)
        if event:
            messages.append(format_event(server.name, event, is_server=True))
        server_down = state[server.name]["down"]
    if server_down:
        return state, messages
    for target in targets:
        state, event = update_state(state, target.name, prober(target), threshold=threshold)
        if event:
            messages.append(format_event(target.name, event))
    return state, messages


def _float(value, default):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def serve(  # pragma: no cover
    token, chat_id, server, targets, *, interval, threshold, timeout, panel=None, panel_auth=None
):
    names = [server.name] if server else []
    names += [t.name for t in targets]
    print(f"[sentinel] vigilando {len(names)} objetivo(s): {', '.join(names)}")
    hint = f" (+ panel {panel})" if panel else ""
    send_telegram(
        token, chat_id, f"👁️ izumi sentinel arrancado — vigilando: {', '.join(names)}{hint}"
    )
    state: dict[str, dict[str, object]] = {}
    known_failing: set[str] = set()

    def prober(target):
        return probe(target, timeout=timeout)

    while True:
        try:
            state, messages = sweep(server, targets, state, threshold=threshold, prober=prober)
            for message in messages:
                send_telegram(token, chat_id, message)
            server_down = bool(server and state.get(server.name, {}).get("down"))
            if panel and not server_down:  # server up → ask WHAT is failing inside it
                snapshot = fetch_status(panel, timeout=timeout, auth=panel_auth)
                if snapshot is not None:
                    known_failing, module_msgs = diff_module_status(known_failing, snapshot)
                    for message in module_msgs:
                        send_telegram(token, chat_id, message)
        except Exception as exc:  # a bad sweep must never kill the watchdog
            print(f"[sentinel] error en la pasada: {exc}")
        time.sleep(interval)


def main():  # pragma: no cover - entrypoint wiring
    here = Path(__file__).resolve().parent
    env_file = parse_env_file(here / ".env")
    token = get_conf("IZUMI_TELEGRAM_BOT_TOKEN", env_file)
    chat_id = get_conf("IZUMI_TELEGRAM_CHAT_ID", env_file)
    if not token or not chat_id:
        raise SystemExit("Faltan IZUMI_TELEGRAM_BOT_TOKEN / IZUMI_TELEGRAM_CHAT_ID (env o .env).")
    server = parse_target(
        get_conf("IZUMI_SENTINEL_SERVER", env_file, default="") or "", default_port=443
    )
    if server is not None:
        server = Target(
            name=_SERVER_NAME, kind=server.kind, host=server.host, port=server.port, url=server.url
        )
    targets = parse_targets(get_conf("IZUMI_SENTINEL_TARGETS", env_file, default=""))
    if server is None and not targets:
        raise SystemExit(
            "Define IZUMI_SENTINEL_SERVER (host:port) o IZUMI_SENTINEL_TARGETS (qué vigilar)."
        )
    serve(
        token,
        chat_id,
        server,
        targets,
        interval=_float(get_conf("IZUMI_SENTINEL_INTERVAL", env_file), _DEFAULT_INTERVAL),
        threshold=_int(get_conf("IZUMI_SENTINEL_FAILS", env_file), _DEFAULT_FAILS),
        timeout=_float(get_conf("IZUMI_SENTINEL_TIMEOUT", env_file), _DEFAULT_TIMEOUT),
        panel=get_conf("IZUMI_SENTINEL_PANEL", env_file, default="") or None,
        panel_auth=get_conf("IZUMI_SENTINEL_PANEL_AUTH", env_file, default="") or None,
    )


if __name__ == "__main__":
    raise SystemExit(main())
