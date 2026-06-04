#!/bin/bash
#
# run.sh — auto-update wrapper for plex_dupefinder.
#
# Point your Unraid User Script (or cron) at THIS file instead of calling
# plex_dupefinder.py directly. On every run it:
#   1. fetches origin/master and fast-forwards the working tree to it
#      (best-effort: a network/git failure NEVER blocks the run — it just
#       proceeds with the version already on disk);
#   2. re-execs itself once after updating, so a changed run.sh is always read
#      fresh and bash can never execute a half-rewritten script;
#   3. runs plex_dupefinder.py, forwarding any CLI args (e.g. --diagnose-paths).
#
# Your local config.json is git-ignored, so the update never touches it.
# Reports/plans/quarantine live outside the repo (or are untracked) and survive
# the reset. Any arguments you pass to run.sh are forwarded to the script.
#
# SAFETY NOTE: this runs the latest committed code unattended. Keep
# QUARANTINE_MODE=true and a sane QUARANTINE_RETENTION_DAYS so a bad commit is
# always recoverable within the retention window.
#
set -uo pipefail

# Resolve the repo dir from this script's own location (path-independent).
REPO="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
cd "$REPO" || { echo "[run.sh] cannot cd to repo: $REPO" >&2; exit 1; }

if [ -z "${DF_SELF_UPDATED:-}" ]; then
    if git fetch --quiet origin master 2>/dev/null; then
        LOCAL="$(git rev-parse HEAD 2>/dev/null || echo unknown)"
        REMOTE="$(git rev-parse origin/master 2>/dev/null || echo unknown)"
        if [ "$LOCAL" != "$REMOTE" ] && [ "$REMOTE" != "unknown" ]; then
            echo "[run.sh] master changed ${LOCAL:0:7} -> ${REMOTE:0:7}, updating..."
            if ! git reset --hard origin/master; then
                echo "[run.sh] git reset failed — proceeding with current version" >&2
            fi
        else
            echo "[run.sh] already up to date (${LOCAL:0:7})"
        fi
    else
        echo "[run.sh] git fetch failed (offline?) — proceeding with current version" >&2
    fi
    # Re-exec once with the (possibly updated) wrapper. The guard env var stops
    # this from looping.
    export DF_SELF_UPDATED=1
    exec "$0" "$@"
fi

# Prefer python3, fall back to python.
PY="$(command -v python3 || command -v python)"
if [ -z "$PY" ]; then
    echo "[run.sh] no python interpreter found" >&2
    exit 1
fi

exec "$PY" plex_dupefinder.py "$@"
