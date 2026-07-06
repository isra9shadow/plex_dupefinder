#!/usr/bin/env python3
"""Interactive terminal menu for plex_dupefinder + the izumi organizer.

A thin, dependency-free launcher so the common operations are point-and-pick
instead of long, error-prone commands. On start it best-effort updates itself
from git (showing the running version + date), it can toggle the main config
options from a submenu, and it ONLY launches the existing entrypoints (real
actions confirm first) — it never moves or deletes anything itself.

Run it on the host:  python3 menu.py
"""

from __future__ import annotations

import calendar
import json
import os
import shlex
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

from aictx.apply import apply_action, collect_actions, default_runner
from core.cache import SqliteCache

ROOT = os.path.dirname(os.path.realpath(__file__))
LEGACY_CFG = os.path.join(ROOT, "config.json")
IZUMI_CFG = os.path.join(ROOT, "config", "config.json")
DOCKER_IMAGE = "python:3.12-slim"  # fallback (no ffprobe)
LOCAL_IMAGE = "izumi-organizer:local"  # built from Dockerfile.organizer (has ffmpeg)
RUN_AS = "99:100"  # Unraid nobody:users — moved files stay manipulable by *arr/user
_W = 60  # menu width


# --- colours (ANSI, only when attached to a TTY) -------------------------------

_TTY = sys.stdout.isatty()


def _c(code, text):
    return f"\033[{code}m{text}\033[0m" if _TTY else text


def _title(text):
    return _c("96;1", text)


def _dim(text):
    return _c("90", text)


def _ok(text):
    return _c("92", text)


def _warn(text):
    return _c("93", text)


def _danger(text):
    return _c("91", text)


# --- config helpers ------------------------------------------------------------


def _load(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _save(path, data):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)


def _cfg_get(path, *keys, default=None):
    """Nested config read; returns ``default`` on any miss or unreadable file."""
    try:
        node = _load(path)
    except (OSError, ValueError):
        return default
    for key in keys:
        if not isinstance(node, dict) or key not in node:
            return default
        node = node[key]
    return node


def _cfg_set(path, value, *keys):
    """Nested config write (creates intermediate objects), persisted."""
    try:
        data = _load(path)
    except (OSError, ValueError):
        data = {}
    node = data
    for key in keys[:-1]:
        sub = node.get(key)
        if not isinstance(sub, dict):
            sub = {}
            node[key] = sub
        node = sub
    node[keys[-1]] = value
    _save(path, data)
    return value


def set_legacy_dry_run(data, dry):
    """Override the legacy dupefinder mode (simulate vs real quarantine)."""
    data["DRY_RUN"] = bool(dry)
    return data


def set_izumi_organizer(data, *, live, apply_moves):
    """Override the izumi organizer mode + apply flag for one run."""
    data.setdefault("safety", {})["mode"] = "live" if live else "dry_run"
    data.setdefault("integrations", {}).setdefault("gemini", {})["apply"] = bool(apply_moves)
    return data


def set_izumi_live(data):
    """Flip the izumi platform into LIVE mode (acts) for one run, leaving the rest
    of the config untouched. Used by modules that act through core/fs (extractor)."""
    data.setdefault("safety", {})["mode"] = "live"
    return data


def set_izumi_push(data):
    """Force LIVE + notify.enabled for a one-off Telegram push (notifypush). The
    on-demand 'enviar informe ahora' should send regardless of the persisted
    notify.enabled (which gates only the passive nightly cron)."""
    data.setdefault("safety", {})["mode"] = "live"
    data.setdefault("notify", {})["enabled"] = True
    return data


@contextmanager
def temp_config(path, mutate):
    """Apply ``mutate(copy)`` to the JSON config for the duration of the block,
    then restore the original file — even on Ctrl-C or error (try/finally)."""
    original = _load(path)
    working = json.loads(json.dumps(original))
    _save(path, mutate(working))
    try:
        yield
    finally:
        _save(path, original)


# --- git self-update + version -------------------------------------------------


def _git(*args):
    """Run a git command in the repo; return CompletedProcess or None on failure.

    Injects ``-c safe.directory=ROOT`` on EVERY call so git never refuses with
    "dubious ownership". On Unraid /root/.gitconfig lives in RAM and is wiped on
    every reboot, so a one-off ``git config --global`` does not persist — doing it
    inline makes the menu's self-update reboot-proof without any global state."""
    try:
        return subprocess.run(
            ["git", "-c", f"safe.directory={ROOT}", "-C", ROOT, *args],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def parse_version(line):
    """Parse a ``<sha>|<date>`` git line into ``(sha, date)`` (pure, testable)."""
    sha, _, date = line.partition("|")
    return (sha.strip() or "?", date.strip() or "?")


def current_version():
    """``(short_sha, date)`` of HEAD, or ``('?', '?')``."""
    out = _git("log", "-1", "--format=%h|%cd", "--date=short")
    if out is not None and out.returncode == 0 and "|" in out.stdout:
        return parse_version(out.stdout.strip())
    return ("?", "?")


def git_update():
    """Best-effort fast-forward to origin/master. Never blocks the menu.

    Returns a short status string: 'updated <a>-><b>' | 'up-to-date' | 'offline'.
    """
    fetched = _git("fetch", "--quiet", "origin", "master")
    if fetched is None or fetched.returncode != 0:
        return "offline"
    before = current_version()[0]
    pulled = _git("pull", "--ff-only")
    if pulled is None or pulled.returncode != 0:
        return "offline"
    after = current_version()[0]
    return f"updated {before}->{after}" if before != after else "up-to-date"


# --- command builders (pure, testable) -----------------------------------------


def dupefinder_command(image=DOCKER_IMAGE):
    """Argv to run the legacy duplicate finder in its configured mode.

    Runs INSIDE the container (not host python3) so it gets tabulate/requests/
    PlexAPI from the image — the host's pip packages are wiped by Unraid on every
    reboot. The mounted config.json/media/cache make paths resolve as configured."""
    return _docker_run("python", "plex_dupefinder.py", image=image)


def dupefinder_diagnose_command(image=DOCKER_IMAGE):
    return _docker_run("python", "plex_dupefinder.py", "--diagnose-paths", image=image)


def _docker_run(*inner, image=DOCKER_IMAGE, user=RUN_AS, extra_args=()):
    """Wrap an izumi entrypoint in a Python 3.12 container (the host Python is too
    old for the platform). Mounts repo + media + cache so paths resolve as
    configured. ``user`` can be set to None to run as root (e.g. logwatch needs
    access to the root-owned docker socket); ``extra_args`` injects extra docker
    flags (e.g. an extra volume mount) before the image."""
    args = ["docker", "run", "--rm"]
    if user:
        args += ["--user", user]
    args += [
        "-v",
        f"{ROOT}:/app",
        "-v",
        "/mnt/user:/mnt/user",
        "-v",
        "/mnt/cache:/mnt/cache",
        *extra_args,
        "-w",
        "/app",
        image,
        *inner,
    ]
    return args


def organizer_command(*, dry, image=DOCKER_IMAGE):
    """Argv to run the izumi organizer in a container (optionally --dry-run)."""
    inner = ["python", "run.py", "organizer"]
    if dry:
        inner.append("--dry-run")
    return _docker_run(*inner, image=image)


def extractor_command(*, dry, image=DOCKER_IMAGE):
    """Argv to run the izumi extractor in a container (optionally --dry-run)."""
    inner = ["python", "run.py", "extractor"]
    if dry:
        inner.append("--dry-run")
    return _docker_run(*inner, image=image)


def analyst_command(image=DOCKER_IMAGE):
    """Argv to run the results analyst (reads the organizer plan, summarizes with
    the local AI why files were not moved). Read-only."""
    return _docker_run("python", "run.py", "analyst", image=image)


def logwatch_command(image=DOCKER_IMAGE):
    """Argv to run the docker-log AI analyst. Runs as root with the docker socket
    mounted so it can read `docker logs` (read-only; moves/deletes nothing)."""
    return _docker_run(
        "python",
        "run.py",
        "logwatch",
        image=image,
        user=None,
        extra_args=["-v", "/var/run/docker.sock:/var/run/docker.sock"],
    )


def health_command(image=DOCKER_IMAGE):
    """Argv to run the izumi platform healthcheck in a container."""
    return _docker_run("python", "run.py", "health", image=image)


def notifypush_command(image=DOCKER_IMAGE):
    """Argv to run the proactive Telegram digest (reads the latest module reports
    and pushes a consolidated summary). No docker socket needed; the token/chat id
    come from .env via core/secrets (repo mounted at /app)."""
    return _docker_run("python", "run.py", "notifypush", image=image)


def uptime_command(image=DOCKER_IMAGE):
    """Argv to run the uptime check. Runs as root with the docker socket mounted
    so it can list containers (read-only); TCP probes reach the LAN."""
    return _docker_run(
        "python",
        "run.py",
        "uptime",
        image=image,
        user=None,
        extra_args=["-v", "/var/run/docker.sock:/var/run/docker.sock"],
    )


def dbcheck_command(image=DOCKER_IMAGE):
    """Argv to run the SQLite corruption detector (read-only; reads DB files under
    the mounted /mnt). Runs as root so it can read app-owned DB files."""
    return _docker_run("python", "run.py", "dbcheck", image=image, user=None)


def dbrepair_command(image=DOCKER_IMAGE):
    """Argv to run the operator-confirmed SQLite auto-repair. Needs the docker
    socket (to stop/start the app container through the apply allow-list) plus
    /mnt for the DB + native backups. The LIVE flag is set by the caller via
    temp_config; DRY_RUN only plans."""
    return _docker_run(
        "python",
        "run.py",
        "dbrepair",
        image=image,
        user=None,
        extra_args=["-v", "/var/run/docker.sock:/var/run/docker.sock"],
    )


def diskwatch_command(image=DOCKER_IMAGE):
    """Argv to run the SMART disk watcher. Needs smartctl (in the image) plus raw
    device access, so it runs privileged with /dev mounted (read-only checks)."""
    return _docker_run(
        "python",
        "run.py",
        "diskwatch",
        image=image,
        user=None,
        extra_args=["--privileged", "-v", "/dev:/dev"],
    )


def configcheck_command(image=DOCKER_IMAGE):
    """Argv to run the config-doctor (checks env vars + config + paths). Read-only."""
    return _docker_run("python", "run.py", "configcheck", image=image, user=None)


def status_command(image=DOCKER_IMAGE):
    """Argv to run the status snapshot. Runs as root with the docker socket so it
    can list containers; CPU/RAM/GPU degrade gracefully if unavailable."""
    return _docker_run(
        "python",
        "run.py",
        "status",
        image=image,
        user=None,
        extra_args=["-v", "/var/run/docker.sock:/var/run/docker.sock"],
    )


def autoheal_command(image=DOCKER_IMAGE):
    """Argv to run autoheal (proposes restarts for down services; read-only)."""
    return _docker_run("python", "run.py", "autoheal", image=image, user=None)


def plexrefresh_command(image=DOCKER_IMAGE):
    """Argv to run the tdarr->Plex targeted refresh (clears false duplicates)."""
    return _docker_run("python", "run.py", "plexrefresh", image=image)


def bot_command():
    """Argv to run the Telegram operations bot in the foreground (host)."""
    return [sys.executable, os.path.join(ROOT, "bot.py")]


def _image_created_epoch():
    """UTC epoch when LOCAL_IMAGE was built, or None if it does not exist."""
    out = subprocess.run(
        ["docker", "image", "inspect", "--format", "{{.Created}}", LOCAL_IMAGE],
        capture_output=True,
        text=True,
    )
    if out.returncode != 0:
        return None
    try:  # docker prints RFC3339 like 2026-06-25T08:00:00.123456789Z (UTC)
        return calendar.timegm(time.strptime(out.stdout.strip()[:19], "%Y-%m-%dT%H:%M:%S"))
    except (ValueError, OverflowError):
        return 0  # exists but unparseable -> treat as old enough, never rebuild-loop


def _image_is_stale(created_epoch):
    """True if Dockerfile.organizer is newer than the built image (needs rebuild)."""
    try:
        return os.path.getmtime(os.path.join(ROOT, "Dockerfile.organizer")) > created_epoch
    except OSError:
        return False


def _runtime_dirs():
    """``(recursive, own)`` host dirs the --user 99:100 workers must write to.

    Read from the izumi + legacy dupefinder config (with sane Unraid defaults) so
    the prepare step matches exactly what the modules use. ``recursive`` are small
    izumi-owned trees (chown -R); ``own`` are big/shared dirs we only take at the
    directory level so their existing contents keep their owners."""
    izumi_logs = _cfg_get(IZUMI_CFG, "logging", "dir") or "/mnt/cache/appdata/izumi/logs"
    izumi_reports = _izumi_reports_dir()
    quarantine = _cfg_get(LEGACY_CFG, "QUARANTINE_DIR") or "/mnt/cache/Downloads/DF/QUARANTINE"
    df_root = os.path.dirname(quarantine)  # holds QUARANTINE + REPORTS + plans
    recursive = [izumi_logs, izumi_reports, "/app/plans"]  # /app/plans = legacy plans
    own = [df_root, quarantine]
    return recursive, own


def prepare_dirs_command(image=DOCKER_IMAGE):
    """Argv for a ROOT container that creates + gives to 99:100 the dirs the
    --user 99:100 workers write to. It runs with the SAME mounts/uid view as the
    workers (a host-side chown can disagree with the container's mount view), so
    it is the authoritative permissions step. Reboot-safe (ownership on /mnt/cache
    persists)."""
    recursive, own = _runtime_dirs()
    quoted_all = " ".join(shlex.quote(d) for d in (*recursive, *own))
    quoted_rec = " ".join(shlex.quote(d) for d in recursive)
    quoted_own = " ".join(shlex.quote(d) for d in own)
    script = (
        f"mkdir -p {quoted_all} 2>/dev/null; "
        f"chown -R 99:100 {quoted_rec} 2>/dev/null; "
        f"chown 99:100 {quoted_own} 2>/dev/null; true"
    )
    return _docker_run("sh", "-c", script, image=image, user=None)


def _prepare_dirs(image):
    """Best-effort: run the prepare-dirs container (silent; never blocks the menu)."""
    try:
        subprocess.run(prepare_dirs_command(image), capture_output=True)
    except (OSError, subprocess.SubprocessError):
        pass


def _build_image():
    """Build/return the local image (ffmpeg + unar + docker client), AUTO-REBUILT
    when Dockerfile.organizer changes. Falls back to the slim image if it cannot
    be built (e.g. offline)."""
    created = _image_created_epoch()
    if created is not None and not _image_is_stale(created):
        return LOCAL_IMAGE
    if created is None:
        print(_dim("[docker] building local image (ffmpeg + unar + docker client)..."))
    else:
        print(_dim("[docker] Dockerfile changed — rebuilding local image..."))
    build = subprocess.run(
        [
            "docker",
            "build",
            "-t",
            LOCAL_IMAGE,
            "-f",
            os.path.join(ROOT, "Dockerfile.organizer"),
            ROOT,
        ]
    )
    if build.returncode == 0:
        return LOCAL_IMAGE
    if created is not None:
        print(_warn("[docker] rebuild failed — using the existing (older) local image."))
        return LOCAL_IMAGE
    print(_warn("[docker] build failed — using slim image (no ffprobe/unar/docker)."))
    return DOCKER_IMAGE


def ensure_image():
    """Return the image to use and ensure the runtime output dirs exist + are
    writable by the container's uid 99:100 (via a root container with the same
    mounts — the authoritative permissions step)."""
    image = _build_image()
    _prepare_dirs(image)
    return image


def _izumi_reports_dir():
    reporting = _cfg_get(IZUMI_CFG, "reporting", "dir")
    return reporting if isinstance(reporting, str) else "/mnt/cache/appdata/izumi/reports"


# --- actions -------------------------------------------------------------------


def _run(argv):
    print("\n" + _dim("$ " + " ".join(argv)) + "\n")
    return subprocess.call(argv)


def confirm(prompt):
    """Ask for explicit confirmation before a real (acting) operation."""
    try:
        return input(_warn(prompt + " [s/N]: ")).strip().lower() in ("s", "si", "sí", "y", "yes")
    except (EOFError, KeyboardInterrupt):
        print()
        return False


def _organizer_real(*, apply_moves):
    image = ensure_image()
    with temp_config(
        IZUMI_CFG, lambda d: set_izumi_organizer(d, live=True, apply_moves=apply_moves)
    ):
        _run(organizer_command(dry=False, image=image))


def action_dupefinder_simulate():
    image = ensure_image()
    with temp_config(LEGACY_CFG, lambda d: set_legacy_dry_run(d, True)):
        _run(dupefinder_command(image=image))


def action_dupefinder_real():
    if confirm("Esto MOVERÁ los duplicados a cuarentena (real). ¿Continuar?"):
        _run(dupefinder_command(image=ensure_image()))


def action_organizer_plan():
    _run(organizer_command(dry=True, image=ensure_image()))


def action_organizer_full():
    if confirm("Esto limpiará basura y MOVERÁ ficheros a su sitio (real). ¿Continuar?"):
        _organizer_real(apply_moves=True)


def action_organizer_cleanup_only():
    if confirm("Esto moverá la basura a cuarentena (real, recuperable). ¿Continuar?"):
        _organizer_real(apply_moves=False)


def action_extract():
    """Extract finished rar/zip/7z archives; on success the compressed volumes are
    moved to quarantine (recoverable, NOT rm'd). Incomplete .part downloads are
    skipped. Requires LIVE mode for the run (restored afterwards)."""
    if confirm(
        "Descomprimir rar/zip/7z y mandar los comprimidos a cuarentena si va bien (real). "
        "¿Continuar?"
    ):
        image = ensure_image()
        with temp_config(IZUMI_CFG, set_izumi_live):
            _run(extractor_command(dry=False, image=image))


def _show_report(subdir, label):
    """Print a module's latest summary.md (or a friendly note if it is missing)."""
    summary = os.path.join(_izumi_reports_dir(), subdir, "summary.md")
    if os.path.isfile(summary):
        with open(summary, encoding="utf-8") as fh:
            print("\n" + _title(label) + "\n" + fh.read())
    else:
        print(_dim(f"\n(no hay {label} todavía en {summary})"))


def action_analyst():
    """Full AI diagnosis: last-week Docker logs (errors/warnings) + the organizer's
    needs_review, each summarized by the local AI."""
    image = ensure_image()
    print("\n[1/2] Revisando logs de Docker (última semana)...")
    _run(logwatch_command(image=image))
    print("\n[2/2] Analizando resultados del organizador...")
    _run(analyst_command(image=image))
    _show_report("logwatch", "Logs Docker (IA)")
    _show_report("analyst", "Resultados organizer (IA)")


def _ai_plan_paths():
    """The module plan.json files that carry an AI diagnosis (logwatch + analyst)."""
    reports = _izumi_reports_dir()
    return [Path(reports) / sub / "plan.json" for sub in ("logwatch", "analyst")]


def _mark_applied(actions):
    """Mark each applied action's incident resolved so the AI stops re-suggesting it."""
    try:
        cache = SqliteCache(Path(_izumi_reports_dir()) / "cache" / "incidents.db")
    except Exception:  # the memory loop is best-effort; never block applying
        return
    try:
        for action in actions:
            cache.resolve_incident(action.fingerprint, applied=[action.command])
        cache.save()
    finally:
        cache.close()


def action_apply_solutions():
    """Apply the AI-proposed fixes, confirming each one (allow-list + guard).

    Reads the latest analyst/logwatch diagnoses and offers ONLY the commands that
    are safe to apply (docker restart/start/stop, docker logs, chmod, chown,
    mkdir). Each runs only after you confirm; destructive ops are never offered
    (they go through quarantine). Applied fixes are marked resolved in memory."""
    actions = collect_actions(_ai_plan_paths())
    if not actions:
        print(_dim("\n(no hay acciones aplicables; lanza antes el Analista IA — opción 9)"))
        return
    print("\n" + _title("Soluciones IA aplicables") + "\n" + _dim("─" * _W))
    for i, action in enumerate(actions, start=1):
        print(f"  {_c('96', f'{i:>2}')}) [{action.severity}] {_ok(action.command)}")
        print(f"      {_dim(action.finding_title)}")
    print(_dim("─" * _W))
    print(_dim("Se aplican una a una, confirmando cada una.\n"))
    applied = []
    for action in actions:
        if not confirm(f"Aplicar: {action.command} ?"):
            continue
        print("\n" + _dim("$ " + action.command))
        outcome = apply_action(action, runner=default_runner)
        if outcome.ok:
            print(_ok("OK (rc=0)"))
            applied.append(action)
        elif outcome.ran:
            print(_danger(f"Falló (rc={outcome.returncode})"))
        else:
            print(_danger(f"No aplicado: {outcome.error}"))
        body = outcome.output.strip()
        if body:
            print(body[:2000])
    if applied:
        _mark_applied(applied)
        print(_ok(f"\n{len(applied)} acción(es) aplicada(s) y marcada(s) como resueltas."))


def action_configcheck():
    """Config-doctor: what env vars / config / paths are set, missing or invalid."""
    _run(configcheck_command(image=ensure_image()))
    _show_report("configcheck", "Config-doctor")


def action_status():
    """On-demand snapshot: CPU/RAM/GPU/disks + containers not running."""
    _run(status_command(image=ensure_image()))
    _show_report("status", "Estado del sistema")


def action_autoheal():
    """Propose restarts for down services (read-only plan; apply with confirmation)."""
    _run(autoheal_command(image=ensure_image()))
    _show_report("autoheal", "Autoheal (reinicios propuestos)")


def action_plexrefresh():
    """Targeted Plex refresh for tdarr-re-encoded items (clears false duplicates)."""
    _run(plexrefresh_command(image=ensure_image()))
    _show_report("plexrefresh", "Refresco Plex (tdarr)")


def action_health_checks():
    """Read-only monitoring sweep: services up, disks (SMART), DB integrity."""
    image = ensure_image()
    print("\n[1/3] Comprobando servicios/contenedores (uptime)...")
    _run(uptime_command(image=image))
    print("\n[2/3] Revisando salud de discos (SMART)...")
    _run(diskwatch_command(image=image))
    print("\n[3/3] Verificando integridad de bases de datos...")
    _run(dbcheck_command(image=image))
    _show_report("uptime", "Servicios (uptime)")
    _show_report("diskwatch", "Discos (SMART)")
    _show_report("dbcheck", "Integridad de DB")


def action_dbrepair():
    """Repair corrupt SQLite DBs with confirmation (snapshot → stop → repair →
    start → verify; rolls back on any failure). Always shows the read-only
    dbcheck first; only acts after explicit confirmation, and the corrupt DB is
    copied to quarantine before anything is touched (recoverable)."""
    image = ensure_image()
    print("\nRevisando integridad de bases de datos (solo lectura)...")
    _run(dbcheck_command(image=image))
    _show_report("dbcheck", "Integridad de DB")
    if not confirm(
        "Reparar bases corruptas (real): copia a cuarentena → para contenedor → "
        "restaura backup nativo o reconstruye → reinicia → verifica. ¿Continuar?"
    ):
        return
    with temp_config(IZUMI_CFG, set_izumi_live):
        _run(dbrepair_command(image=image))
    _show_report("dbrepair", "Reparación de DB")


def action_notifypush():
    """Send a consolidated health/AI report to Telegram right now (push).

    Runs the digest in LIVE with notify enabled for this one run, so it sends
    regardless of the persisted notify.enabled (that flag gates the nightly cron).
    Reads whatever reports already exist — run the health checks / analyst first
    for a fuller digest."""
    image = ensure_image()
    with temp_config(IZUMI_CFG, set_izumi_push):
        _run(notifypush_command(image=image))
    _show_report("notifypush", "Informe (Telegram)")


def action_full_maintenance():
    """Recommended order: extract archives, remove duplicates, then clean+organize."""
    if not confirm(
        "Mantenimiento completo REAL (descomprimir → duplicados → organizar). ¿Continuar?"
    ):
        return
    image = ensure_image()
    print("\n[1/3] Descomprimiendo archivos (cuarentena de comprimidos si va bien)...")
    with temp_config(IZUMI_CFG, set_izumi_live):
        _run(extractor_command(dry=False, image=image))
    print("\n[2/3] Quitando duplicados (cuarentena real)...")
    _run(dupefinder_command(image=image))
    print("\n[3/3] Limpiando basura + organizando ficheros (real)...")
    _organizer_real(apply_moves=True)
    print("\nMantenimiento completo terminado.")


def action_health():
    _run(health_command(image=ensure_image()))


def action_bot():
    """Start the Telegram operations bot (foreground; Ctrl-C to stop). Lets you
    launch the same operations remotely. Reads its token + allowed chat id(s)
    from .env (IZUMI_TELEGRAM_BOT_TOKEN / IZUMI_TELEGRAM_CHAT_ID)."""
    print(_dim("Iniciando bot de Telegram (Ctrl-C para parar)..."))
    _run(bot_command())


def action_show_organizer_plan():
    plan = os.path.join(_izumi_reports_dir(), "organizer", "plan.md")
    if os.path.isfile(plan):
        with open(plan, encoding="utf-8") as fh:
            print("\n" + fh.read())
    else:
        print(_dim(f"\n(no hay plan todavía en {plan})"))


def action_diagnose_paths():
    _run(dupefinder_diagnose_command(image=ensure_image()))


# --- configuration submenu -----------------------------------------------------


def _onoff(value):
    return _ok("ON") if value else _dim("off")


def _config_lines():
    """Human-readable current state of the main config options."""
    dry = bool(_cfg_get(LEGACY_CFG, "DRY_RUN", default=True))
    mode = _cfg_get(IZUMI_CFG, "safety", "mode", default="dry_run")
    apply_moves = bool(_cfg_get(IZUMI_CFG, "integrations", "gemini", "apply", default=False))
    fallback = bool(_cfg_get(IZUMI_CFG, "integrations", "gemini", "ai_fallback", default=True))
    providers = _cfg_get(IZUMI_CFG, "integrations", "ai", "providers", default=["gemini"])
    thr = _cfg_get(IZUMI_CFG, "integrations", "gemini", "confidence_threshold", default=90)
    return [
        f"  1) Dupefinder DRY_RUN ............ {_onoff(dry)}  "
        + _dim("(ON=simula, off=borra real)"),
        f"  2) Organizer modo LIVE .......... {_onoff(mode == 'live')}",
        f"  3) Organizer apply (mover) ...... {_onoff(apply_moves)}",
        f"  4) IA fallback .................. {_onoff(fallback)}",
        f"  5) Proveedores IA ............... {_ok(','.join(providers) or 'ninguno')}",
        f"  6) Umbral de confianza .......... {_ok(str(thr))}%",
        "  0) Volver",
    ]


def _cycle_providers(current):
    """Cycle the AI provider order through the sensible presets."""
    presets = [["ollama", "gemini"], ["ollama"], ["gemini"], []]
    try:
        idx = presets.index(list(current))
    except ValueError:
        idx = -1
    return presets[(idx + 1) % len(presets)]


def action_config():
    while True:
        print("\n" + _title("Configuración") + "\n" + _dim("-" * _W))
        print("\n".join(_config_lines()))
        print(_dim("-" * _W))
        try:
            choice = input("Opción a cambiar: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if choice == "0":
            return
        if choice == "1":
            new = not bool(_cfg_get(LEGACY_CFG, "DRY_RUN", default=True))
            _cfg_set(LEGACY_CFG, new, "DRY_RUN")
        elif choice == "2":
            live = _cfg_get(IZUMI_CFG, "safety", "mode", default="dry_run") == "live"
            _cfg_set(IZUMI_CFG, "dry_run" if live else "live", "safety", "mode")
        elif choice == "3":
            new = not bool(_cfg_get(IZUMI_CFG, "integrations", "gemini", "apply", default=False))
            _cfg_set(IZUMI_CFG, new, "integrations", "gemini", "apply")
        elif choice == "4":
            new = not bool(
                _cfg_get(IZUMI_CFG, "integrations", "gemini", "ai_fallback", default=True)
            )
            _cfg_set(IZUMI_CFG, new, "integrations", "gemini", "ai_fallback")
        elif choice == "5":
            cur = _cfg_get(IZUMI_CFG, "integrations", "ai", "providers", default=["gemini"])
            _cfg_set(IZUMI_CFG, _cycle_providers(cur), "integrations", "ai", "providers")
        elif choice == "6":
            raw = input("Nuevo umbral 0-100 (enter=cancela): ").strip()
            if raw.isdigit():
                _cfg_set(
                    IZUMI_CFG,
                    max(0, min(100, int(raw))),
                    "integrations",
                    "gemini",
                    "confidence_threshold",
                )
        else:
            print(_danger("Opción no válida."))


MENU = [
    ("Buscar duplicados — SIMULAR (no borra nada)", action_dupefinder_simulate),
    ("Buscar duplicados — EJECUTAR (cuarentena real)", action_dupefinder_real),
    ("Organizar — Ver plan IA (no toca nada)", action_organizer_plan),
    ("Organizar — Limpiar basura + MOVER ficheros (real)", action_organizer_full),
    ("Organizar — Solo limpiar basura (no mueve ficheros)", action_organizer_cleanup_only),
    ("Descomprimir archivos rar/zip/7z + cuarentena (real)", action_extract),
    (
        "Mantenimiento completo (orden: descomprimir → duplicados → organizar)",
        action_full_maintenance,
    ),
    ("Ver último plan del organizador", action_show_organizer_plan),
    ("Analista IA — todo (logs Docker semana + organizer + duplicados)", action_analyst),
    ("Aplicar soluciones IA (con confirmación)", action_apply_solutions),
    ("Chequeos de salud (servicios + discos + DB)", action_health_checks),
    ("Reparar base de datos corrupta (con confirmación)", action_dbrepair),
    ("Estado del sistema (CPU/RAM/GPU/discos)", action_status),
    ("Config-doctor (qué falta por configurar)", action_configcheck),
    ("Proponer reinicios de servicios caídos (autoheal)", action_autoheal),
    ("Refrescar Plex tras tdarr (falsos duplicados)", action_plexrefresh),
    ("Enviar informe ahora por Telegram (push)", action_notifypush),
    ("Configuración (activar/desactivar opciones)", action_config),
    ("Healthcheck de la plataforma", action_health),
    ("Diagnóstico de rutas (dupefinder)", action_diagnose_paths),
    ("Bot de Telegram (lanzar ejecuciones por chat)", action_bot),
]


def render_menu(version_line=""):
    bar = "═" * _W
    lines = [
        "",
        _title("╔" + bar + "╗"),
        _title("║") + "  plex_dupefinder · menú",
        _title("╚" + bar + "╝"),
    ]
    if version_line:
        lines.append("  " + _dim(version_line))
    lines.append("")
    for i, (label, _action) in enumerate(MENU, start=1):
        lines.append(f"  {_c('96', f'{i:>2}')}) {label}")
    lines.append(f"  {_c('96', ' 0')}) Salir")
    lines.append(_dim("─" * (_W + 2)))
    return "\n".join(lines)


def _startup_update():
    """Self-update once (guarded against an exec loop), then re-exec if changed."""
    if os.environ.get("MENU_NO_UPDATE") == "1" or os.environ.get("_MENU_REEXEC") == "1":
        return
    status = git_update()
    print(_dim(f"[git] {status}"))
    if status.startswith("updated"):
        os.environ["_MENU_REEXEC"] = "1"
        print(_dim("recargando menú con la versión nueva..."))
        try:
            os.execv(sys.executable, [sys.executable, os.path.abspath(__file__)])
        except OSError:
            pass  # fall through and run the current version


def main(argv=None):
    _startup_update()
    sha, date = current_version()
    version_line = f"versión {sha} ({date})"
    while True:
        print(render_menu(version_line))
        try:
            choice = input("Elige una opción: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if choice == "0":
            return 0
        if choice.isdigit() and 1 <= int(choice) <= len(MENU):
            MENU[int(choice) - 1][1]()
        else:
            print(_danger("Opción no válida."))


if __name__ == "__main__":
    sys.exit(main())
