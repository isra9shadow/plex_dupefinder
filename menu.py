#!/usr/bin/env python3
"""Interactive terminal menu for plex_dupefinder + the izumi organizer.

A thin, dependency-free launcher so the common operations are point-and-pick
instead of long, error-prone commands. It ONLY launches the existing
entrypoints (and, for a "simulate" choice, temporarily flips a config flag and
restores it afterwards) — it never moves or deletes anything itself, so every
safety guarantee of the underlying tools is preserved.

Run it on the host:  python3 menu.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from contextlib import contextmanager

ROOT = os.path.dirname(os.path.realpath(__file__))
LEGACY_CFG = os.path.join(ROOT, "config.json")
IZUMI_CFG = os.path.join(ROOT, "config", "config.json")
DOCKER_IMAGE = "python:3.12-slim"


# --- config helpers ------------------------------------------------------------


def _load(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _save(path, data):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)


def set_legacy_dry_run(data, dry):
    """Override the legacy dupefinder mode (simulate vs real quarantine)."""
    data["DRY_RUN"] = bool(dry)
    return data


def set_izumi_organizer(data, *, live, apply_moves):
    """Override the izumi organizer mode + apply flag for one run."""
    data.setdefault("safety", {})["mode"] = "live" if live else "dry_run"
    data.setdefault("integrations", {}).setdefault("gemini", {})["apply"] = bool(apply_moves)
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


# --- command builders (pure, testable) -----------------------------------------


def dupefinder_command():
    """Argv to run the legacy duplicate finder in its configured mode."""
    return [sys.executable, os.path.join(ROOT, "plex_dupefinder.py")]


def dupefinder_diagnose_command():
    return [sys.executable, os.path.join(ROOT, "plex_dupefinder.py"), "--diagnose-paths"]


def organizer_command(*, dry):
    """Argv to run the izumi organizer inside a Python 3.12 container (the host
    Python is too old for the platform). Mounts the repo + media + cache so paths
    resolve exactly as configured."""
    inner = ["python", "run.py", "organizer"]
    if dry:
        inner.append("--dry-run")
    return [
        "docker",
        "run",
        "--rm",
        "-v",
        f"{ROOT}:/app",
        "-v",
        "/mnt/user:/mnt/user",
        "-v",
        "/mnt/cache:/mnt/cache",
        "-w",
        "/app",
        DOCKER_IMAGE,
        *inner,
    ]


def _izumi_reports_dir():
    try:
        reporting = _load(IZUMI_CFG).get("reporting", {})
        if isinstance(reporting, dict) and isinstance(reporting.get("dir"), str):
            return reporting["dir"]
    except (OSError, ValueError):
        pass
    return "/mnt/cache/appdata/izumi/reports"


# --- actions -------------------------------------------------------------------


def _run(argv):
    print("\n$ " + " ".join(argv) + "\n")
    return subprocess.call(argv)


def action_dupefinder_simulate():
    with temp_config(LEGACY_CFG, lambda d: set_legacy_dry_run(d, True)):
        _run(dupefinder_command())


def action_dupefinder_real():
    _run(dupefinder_command())


def action_organizer_plan():
    _run(organizer_command(dry=True))


def action_organizer_full():
    with temp_config(IZUMI_CFG, lambda d: set_izumi_organizer(d, live=True, apply_moves=True)):
        _run(organizer_command(dry=False))


def action_organizer_cleanup_only():
    with temp_config(IZUMI_CFG, lambda d: set_izumi_organizer(d, live=True, apply_moves=False)):
        _run(organizer_command(dry=False))


def action_show_organizer_plan():
    plan = os.path.join(_izumi_reports_dir(), "organizer", "plan.md")
    if os.path.isfile(plan):
        with open(plan, encoding="utf-8") as fh:
            print("\n" + fh.read())
    else:
        print(f"\n(no hay plan todavía en {plan})")


def action_diagnose_paths():
    _run(dupefinder_diagnose_command())


MENU = [
    ("Buscar duplicados — SIMULAR (no borra nada)", action_dupefinder_simulate),
    ("Buscar duplicados — EJECUTAR (mueve a cuarentena, real)", action_dupefinder_real),
    ("Organizar — Ver plan IA (no toca nada)", action_organizer_plan),
    ("Organizar — Limpiar basura + MOVER ficheros (real)", action_organizer_full),
    ("Organizar — Solo limpiar basura (no mueve ficheros)", action_organizer_cleanup_only),
    ("Ver último plan del organizador", action_show_organizer_plan),
    ("Diagnóstico de rutas (dupefinder)", action_diagnose_paths),
]


def render_menu():
    lines = ["", "=" * 56, "  plex_dupefinder · menú", "=" * 56]
    for i, (label, _) in enumerate(MENU, start=1):
        lines.append(f"  {i}) {label}")
    lines.append("  0) Salir")
    lines.append("-" * 56)
    return "\n".join(lines)


def main(argv=None):
    while True:
        print(render_menu())
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
            print("Opción no válida.")


if __name__ == "__main__":
    sys.exit(main())
