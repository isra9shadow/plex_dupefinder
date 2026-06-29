#!/usr/bin/env python3
"""Telegram bot to drive the izumi / plex_dupefinder operations remotely.

Long-polls Telegram and exposes the same operations as the interactive menu
(``menu.py``) both as chat commands and as inline-button taps. It is an OPERATOR
launcher, exactly like ``menu.py``: it only spawns the existing trusted
entrypoints (it never moves or deletes anything itself), it reuses ``menu.py``'s
command builders, and it runs real (acting) operations only after an explicit
inline-keyboard confirmation.

Security:
  * The bot token and the authorized chat id(s) are read via ``core/secrets``
    (env / ``.env``). Messages from any chat that is NOT in the allow-list are
    silently ignored (so the bot's existence is not leaked to strangers).
  * Destructive actions (real quarantine moves) require a "Confirmar" tap.
  * ``/apply`` runs AI-proposed fixes through ``aictx.apply`` (positive allow-list
    + guard), and only after a per-command "Aplicar" confirmation tap.

Run on the host:  python3 bot.py
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import menu
from aictx.apply import apply_action, collect_actions, default_runner
from core.cache import SqliteCache
from core.secrets import get_secret

_API = "https://api.telegram.org/bot{token}/{method}"
_POLL_TIMEOUT = 50  # Telegram long-poll seconds
_HTTP_TIMEOUT = _POLL_TIMEOUT + 15  # urllib timeout must outlast the long-poll
_MSG_LIMIT = 3500  # stay under Telegram's 4096-char hard cap, with margin
_RUN_LOCK = threading.Lock()  # only ONE execution at a time across all chats
_PENDING_APPLY: dict[int, list] = {}  # chat_id -> list[ApplyAction] offered by /apply


# --- action catalogue (mirrors the menu) ---------------------------------------


@dataclass(frozen=True)
class Action:
    key: str
    label: str
    destructive: bool  # real moves to quarantine -> require confirmation


ACTIONS = {
    a.key: a
    for a in (
        Action("dupes_sim", "Duplicados — SIMULAR (no borra)", False),
        Action("analyst", "Analista IA (logs Docker + organizer + duplicados)", False),
        Action("sysstatus", "Estado del sistema (CPU/RAM/GPU/discos)", False),
        Action("configdoctor", "Config-doctor (qué falta por configurar)", False),
        Action("organize_plan", "Organizar — Ver plan IA (no toca nada)", False),
        Action("health", "Healthcheck de la plataforma", False),
        Action("extract", "Descomprimir rar/zip/7z + cuarentena (real)", True),
        Action("organize", "Organizar — Limpiar basura + MOVER ficheros (real)", True),
        Action("cleanup", "Organizar — Solo limpiar basura (real)", True),
        Action("dupes_real", "Duplicados — EJECUTAR (cuarentena real)", True),
        Action("maintenance", "Mantenimiento completo (descomprimir→dupes→organizar)", True),
    )
}

# Slash-command aliases -> action key.
COMMANDS = {
    "/dupes": "dupes_sim",
    "/dupes_real": "dupes_real",
    "/analyst": "analyst",
    "/estado": "sysstatus",
    "/configdoctor": "configdoctor",
    "/plan": "organize_plan",
    "/organize": "organize",
    "/cleanup": "cleanup",
    "/extract": "extract",
    "/maintenance": "maintenance",
    "/health": "health",
}

HELP_TEXT = (
    "🤖 izumi · bot de operaciones\n\n"
    "Pulsa un botón del menú o usa estos comandos:\n"
    "/dupes — buscar duplicados (SIMULAR, no borra)\n"
    "/analyst — analista IA (logs Docker semana + organizer + duplicados)\n"
    "/plan — ver plan del organizador (IA, no toca nada)\n"
    "/extract — descomprimir + cuarentena (real)\n"
    "/organize — limpiar basura + mover ficheros (real)\n"
    "/cleanup — solo limpiar basura (real)\n"
    "/dupes_real — mover duplicados a cuarentena (real)\n"
    "/maintenance — todo: descomprimir → duplicados → organizar (real)\n"
    "/health — healthcheck\n"
    "/estado — estado del sistema (CPU/RAM/GPU/discos/contenedores)\n"
    "/configdoctor — qué variables/rutas faltan por configurar\n"
    "/apply — aplicar soluciones IA (allow-list segura, con confirmación)\n"
    "/status — estado del bot\n\n"
    "Las acciones REALES piden confirmación antes de ejecutarse."
)


# --- pure helpers (unit-tested) -------------------------------------------------


def parse_allowlist(*values):
    """Build a set of int chat ids from comma/space separated string(s)."""
    out: set[int] = set()
    for value in values:
        if not value:
            continue
        for token in value.replace(",", " ").split():
            try:
                out.add(int(token))
            except ValueError:
                continue
    return out


def is_authorized(chat_id, allowed):
    return chat_id in allowed


def command_for(text):
    """Map a message to an action key, or None. Handles '/cmd@botname args'."""
    if not text or not text.strip():
        return None
    token = text.strip().split()[0].lower().split("@", 1)[0]
    return COMMANDS.get(token)


@dataclass(frozen=True)
class Decision:
    # deny | help | status | run | confirm | cancel | unknown
    # | apply_list | apply_confirm | apply_run
    kind: str
    action: str = ""  # action key (run/confirm) or action index (apply_confirm/apply_run)


def decide(*, message_text=None, callback_data=None, authorized):
    """Pure routing: turn an incoming message/callback into a Decision."""
    if not authorized:
        return Decision("deny")
    if callback_data is not None:
        verb, _, key = callback_data.partition(":")
        if callback_data == "cancel":
            return Decision("cancel")
        if verb == "act" and key in ACTIONS:
            return Decision("confirm" if ACTIONS[key].destructive else "run", key)
        if verb == "confirm" and key in ACTIONS:
            return Decision("run", key)
        if verb == "apply":  # tap an applicable AI action -> ask confirmation
            return Decision("apply_confirm", key)
        if verb == "doapply":  # confirmed -> run it
            return Decision("apply_run", key)
        return Decision("unknown")
    text = (message_text or "").strip()
    first = text.split()[0].lower().split("@", 1)[0] if text else ""
    if first in ("/start", "/help", "/menu"):
        return Decision("help")
    if first == "/status":
        return Decision("status")
    if first == "/apply":
        return Decision("apply_list")
    key = command_for(text)
    if key:
        return Decision("confirm" if ACTIONS[key].destructive else "run", key)
    return Decision("unknown")


def _action_at(actions, index_str):
    """Return ``actions[int(index_str)]`` or None (parse/bounds-safe)."""
    try:
        idx = int(index_str)
    except (TypeError, ValueError):
        return None
    return actions[idx] if 0 <= idx < len(actions) else None


def main_keyboard():
    """Inline keyboard mirroring the menu (one button per row; labels are long)."""
    rows = [
        [{"text": ("⚠️ " if act.destructive else "") + act.label, "callback_data": f"act:{act.key}"}]
        for act in ACTIONS.values()
    ]
    return {"inline_keyboard": rows}


def confirm_keyboard(key):
    return {
        "inline_keyboard": [
            [
                {"text": "✅ Confirmar", "callback_data": f"confirm:{key}"},
                {"text": "✖️ Cancelar", "callback_data": "cancel"},
            ]
        ]
    }


def apply_list_keyboard(actions):
    """One button per applicable AI action (callback ``apply:<index>``)."""
    rows = [
        [{"text": f"▶ {action.command}"[:60], "callback_data": f"apply:{index}"}]
        for index, action in enumerate(actions)
    ]
    return {"inline_keyboard": rows}


def apply_confirm_keyboard(index):
    return {
        "inline_keyboard": [
            [
                {"text": "✅ Aplicar", "callback_data": f"doapply:{index}"},
                {"text": "✖️ Cancelar", "callback_data": "cancel"},
            ]
        ]
    }


def split_message(text, limit=_MSG_LIMIT):
    """Split text into Telegram-sized chunks on line boundaries (lossless)."""
    chunks: list[str] = []
    buf = ""
    for line in text.splitlines(keepends=True):
        if buf and len(buf) + len(line) > limit:
            chunks.append(buf)
            buf = ""
        while len(line) > limit:  # a single over-long line: hard-split it
            chunks.append(line[:limit])
            line = line[limit:]
        buf += line
    if buf:
        chunks.append(buf)
    return chunks or [""]


def format_result(label, rc, text, *, cap=6000):
    """Header + (tail-capped) body for an execution result."""
    head = ("✅ " if rc == 0 else "❌ ") + f"{label} — rc={rc}"
    body = text.strip()
    if len(body) > cap:  # keep the END (summaries/totals live there)
        body = "…(recortado)…\n" + body[-cap:]
    return head + ("\n\n" + body if body else "")


# --- Telegram API (urllib; api.telegram.org is the only host) -------------------


def _tg(token, method, payload, *, timeout=_HTTP_TIMEOUT):
    url = _API.format(token=token, method=method)
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url, data=data, headers={"content-type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def send_message(token, chat_id, text, *, keyboard=None):
    chunks = split_message(text)
    last = None
    for index, chunk in enumerate(chunks):
        payload = {"chat_id": chat_id, "text": chunk, "disable_web_page_preview": True}
        if keyboard is not None and index == len(chunks) - 1:
            payload["reply_markup"] = keyboard
        last = _tg(token, "sendMessage", payload)
    return last


def answer_callback(token, callback_id):
    return _tg(token, "answerCallbackQuery", {"callback_query_id": callback_id})


def get_updates(token, offset):
    payload = {
        "timeout": _POLL_TIMEOUT,
        "offset": offset,
        "allowed_updates": ["message", "callback_query"],
    }
    response = _tg(token, "getUpdates", payload)
    return response.get("result", []) if isinstance(response, dict) else []


# --- execution layer (spawns the existing entrypoints; reuses menu builders) ----


def _exec(argv):
    """Run a command, capturing combined stdout+stderr; return (rc, text)."""
    proc = subprocess.run(argv, capture_output=True, text=True)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def _read_summary(subdir):
    """Latest summary.md for a module, or '' if missing."""
    path = os.path.join(menu._izumi_reports_dir(), subdir, "summary.md")
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except OSError:
        return ""


def execute_action(key, image):
    """Run one action (captured) and return (rc, text). Mirrors menu.action_*."""
    if key == "dupes_sim":
        with menu.temp_config(menu.LEGACY_CFG, lambda d: menu.set_legacy_dry_run(d, True)):
            return _exec(menu.dupefinder_command(image=image))
    if key == "dupes_real":
        return _exec(menu.dupefinder_command(image=image))
    if key == "sysstatus":
        _exec(menu.status_command(image=image))
        return 0, (_read_summary("status") or "(sin datos de estado)")
    if key == "configdoctor":
        _exec(menu.configcheck_command(image=image))
        return 0, (_read_summary("configcheck") or "(sin informe de config)")
    if key == "organize_plan":
        return _exec(menu.organizer_command(dry=True, image=image))
    if key == "organize":
        with menu.temp_config(
            menu.IZUMI_CFG, lambda d: menu.set_izumi_organizer(d, live=True, apply_moves=True)
        ):
            return _exec(menu.organizer_command(dry=False, image=image))
    if key == "cleanup":
        with menu.temp_config(
            menu.IZUMI_CFG, lambda d: menu.set_izumi_organizer(d, live=True, apply_moves=False)
        ):
            return _exec(menu.organizer_command(dry=False, image=image))
    if key == "extract":
        with menu.temp_config(menu.IZUMI_CFG, menu.set_izumi_live):
            return _exec(menu.extractor_command(dry=False, image=image))
    if key == "analyst":
        rc_logs, _ = _exec(menu.logwatch_command(image=image))
        rc_org, _ = _exec(menu.analyst_command(image=image))
        body = (
            "🩺 LOGS DOCKER (IA)\n\n"
            + (_read_summary("logwatch") or "(sin resumen)")
            + "\n\n———\n\n📦 ORGANIZER + DUPLICADOS (IA)\n\n"
            + (_read_summary("analyst") or "(sin resumen)")
        )
        return (rc_logs or rc_org), body
    if key == "maintenance":
        steps = []
        with menu.temp_config(menu.IZUMI_CFG, menu.set_izumi_live):
            steps.append(("descomprimir", *_exec(menu.extractor_command(dry=False, image=image))))
        steps.append(("duplicados", *_exec(menu.dupefinder_command(image=image))))
        with menu.temp_config(
            menu.IZUMI_CFG, lambda d: menu.set_izumi_organizer(d, live=True, apply_moves=True)
        ):
            steps.append(("organizar", *_exec(menu.organizer_command(dry=False, image=image))))
        rc = 0
        for _, step_rc, _out in steps:
            rc = step_rc or rc
        text = "\n\n".join(f"[{name}] rc={step_rc}\n{out[-1500:]}" for name, step_rc, out in steps)
        return rc, text
    if key == "health":
        return _exec(menu.health_command(image=image))
    return 1, f"acción desconocida: {key}"


def run_action(token, chat_id, key):
    """Worker (run in a thread): execute one action and report the result."""
    act = ACTIONS[key]
    if not _RUN_LOCK.acquire(blocking=False):
        send_message(token, chat_id, "⏳ Ya hay una ejecución en curso. Espera a que termine.")
        return
    try:
        send_message(token, chat_id, f"🚀 Lanzando: {act.label}…")
        image = menu.ensure_image()
        rc, out = execute_action(key, image)
        cap = 20000 if key == "analyst" else 6000
        send_message(token, chat_id, format_result(act.label, rc, out, cap=cap))
    except Exception as exc:  # a failed run must never kill the bot
        send_message(token, chat_id, f"❌ Error ejecutando {act.label}: {exc}")
    finally:
        _RUN_LOCK.release()


# --- apply layer (operator-confirmed AI fixes; allow-list + guard) --------------


def _apply_plan_paths():
    """The module plan.json files that carry an AI diagnosis (logwatch + analyst)."""
    reports = menu._izumi_reports_dir()
    return [Path(reports) / sub / "plan.json" for sub in ("logwatch", "analyst")]


def _mark_applied(action):
    """Best-effort: mark the applied action's incident resolved (memory loop)."""
    try:
        cache = SqliteCache(Path(menu._izumi_reports_dir()) / "cache" / "incidents.db")
    except Exception:
        return
    try:
        cache.resolve_incident(action.fingerprint, applied=[action.command])
        cache.save()
    finally:
        cache.close()


def run_apply(token, chat_id, action):
    """Worker (thread): apply ONE confirmed AI action (re-vetted) and report."""
    if not _RUN_LOCK.acquire(blocking=False):
        send_message(token, chat_id, "⏳ Ya hay una ejecución en curso. Espera a que termine.")
        return
    try:
        send_message(token, chat_id, f"🔧 Aplicando: {action.command}")
        outcome = apply_action(action, runner=default_runner)
        if not outcome.ran:
            send_message(token, chat_id, f"🚫 No aplicado: {outcome.error}")
            return
        if outcome.ok:
            _mark_applied(action)
        send_message(
            token, chat_id, format_result(action.command, outcome.returncode, outcome.output)
        )
    except Exception as exc:  # a failed apply must never kill the bot
        send_message(token, chat_id, f"❌ Error aplicando {action.command}: {exc}")
    finally:
        _RUN_LOCK.release()


# --- dispatch + serve loop ------------------------------------------------------


def _status_text():
    sha, date = menu.current_version()
    return f"versión {sha} ({date})\nejecución en curso: {'sí' if _RUN_LOCK.locked() else 'no'}"


def _dispatch(decision, token, chat_id):
    if decision.kind == "deny":
        return  # silently ignore unauthorized chats
    if decision.kind == "help":
        send_message(token, chat_id, HELP_TEXT, keyboard=main_keyboard())
    elif decision.kind == "status":
        send_message(token, chat_id, _status_text())
    elif decision.kind == "cancel":
        send_message(token, chat_id, "✖️ Cancelado.")
    elif decision.kind == "confirm":
        act = ACTIONS[decision.action]
        send_message(
            token,
            chat_id,
            f"⚠️ Operación REAL: {act.label}.\n¿Confirmas?",
            keyboard=confirm_keyboard(act.key),
        )
    elif decision.kind == "run":
        threading.Thread(
            target=run_action, args=(token, chat_id, decision.action), daemon=True
        ).start()
    elif decision.kind == "apply_list":
        actions = collect_actions(_apply_plan_paths())
        _PENDING_APPLY[chat_id] = actions
        if not actions:
            send_message(token, chat_id, "No hay soluciones IA aplicables. Lanza antes /analyst.")
        else:
            lines = [f"{i + 1}. [{a.severity}] {a.command}" for i, a in enumerate(actions)]
            send_message(
                token,
                chat_id,
                "🛠️ Soluciones IA aplicables (pulsa una; pedirá confirmación):\n\n"
                + "\n".join(lines),
                keyboard=apply_list_keyboard(actions),
            )
    elif decision.kind == "apply_confirm":
        action = _action_at(_PENDING_APPLY.get(chat_id, []), decision.action)
        if action is None:
            send_message(token, chat_id, "Esa acción ya no está disponible. Usa /apply de nuevo.")
        else:
            send_message(
                token,
                chat_id,
                f"⚠️ Aplicar de verdad:\n{action.command}\n({action.finding_title})\n¿Confirmas?",
                keyboard=apply_confirm_keyboard(decision.action),
            )
    elif decision.kind == "apply_run":
        action = _action_at(_PENDING_APPLY.get(chat_id, []), decision.action)
        if action is None:
            send_message(token, chat_id, "Esa acción ya no está disponible. Usa /apply de nuevo.")
        else:
            threading.Thread(target=run_apply, args=(token, chat_id, action), daemon=True).start()
    else:  # unknown
        send_message(
            token, chat_id, "No entiendo ese comando. Usa /menu.", keyboard=main_keyboard()
        )


def process_update(update, token, allowed):
    callback = update.get("callback_query")
    if callback is not None:
        chat_id = callback.get("message", {}).get("chat", {}).get("id")
        decision = decide(
            callback_data=callback.get("data", ""),
            authorized=is_authorized(chat_id, allowed),
        )
        answer_callback(token, callback.get("id", ""))
        _dispatch(decision, token, chat_id)
        return
    message = update.get("message")
    if message is not None:
        chat_id = message.get("chat", {}).get("id")
        decision = decide(
            message_text=message.get("text", ""),
            authorized=is_authorized(chat_id, allowed),
        )
        _dispatch(decision, token, chat_id)


def serve(token, allowed):  # pragma: no cover - network loop
    offset = 0
    print(f"[bot] escuchando (chats autorizados: {sorted(allowed)})")
    while True:
        try:
            updates = get_updates(token, offset)
        except (urllib.error.URLError, OSError, TimeoutError, ValueError) as exc:
            print(f"[bot] error de red: {exc}; reintento en 5s")
            time.sleep(5)
            continue
        for update in updates:
            offset = max(offset, update.get("update_id", 0) + 1)
            try:
                process_update(update, token, allowed)
            except Exception as exc:  # one bad update must not kill the loop
                print(f"[bot] error procesando update: {exc}")


def main():  # pragma: no cover - entrypoint wiring
    try:
        token = get_secret("IZUMI_TELEGRAM_BOT_TOKEN")
    except Exception as exc:
        raise SystemExit(f"Falta IZUMI_TELEGRAM_BOT_TOKEN en .env: {exc}") from exc
    allowed = parse_allowlist(
        get_secret("IZUMI_TELEGRAM_CHAT_ID", required=False),
        get_secret("IZUMI_TELEGRAM_ALLOWED_IDS", required=False),
    )
    if not allowed:
        raise SystemExit(
            "Define IZUMI_TELEGRAM_CHAT_ID (tu chat id) en .env para autorizar el bot."
        )
    serve(token, allowed)


if __name__ == "__main__":
    raise SystemExit(main())
