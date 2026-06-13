#!/bin/bash
#
# izumi platform deploy wrapper — point your Unraid User Script (or cron) at THIS
# file. On every run it best-effort fast-forwards the working tree from git (a
# network/git failure NEVER blocks the run), re-execs itself once so a changed
# wrapper is read fresh, then runs run.py forwarding any args.
#
#   deploy/run.sh health
#   deploy/run.sh daily
#   deploy/run.sh plex_dupefinder --dry-run
#
# config/config.json is git-ignored and survives the update; reports/logs/
# quarantine live outside the repo (or are untracked). SAFETY: this runs the
# latest committed code unattended — keep safety.mode=dry_run until you trust a run.
#
set -uo pipefail

REPO="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
cd "$REPO" || { echo "[deploy] cannot cd to repo: $REPO" >&2; exit 1; }

if [ -z "${IZUMI_SELF_UPDATED:-}" ]; then
    if git fetch --quiet origin 2>/dev/null; then
        BRANCH="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo main)"
        LOCAL="$(git rev-parse HEAD 2>/dev/null || echo unknown)"
        REMOTE="$(git rev-parse "origin/$BRANCH" 2>/dev/null || echo unknown)"
        if [ "$LOCAL" != "$REMOTE" ] && [ "$REMOTE" != "unknown" ]; then
            echo "[deploy] $BRANCH ${LOCAL:0:7} -> ${REMOTE:0:7}, fast-forwarding..."
            git merge --ff-only "origin/$BRANCH" 2>/dev/null \
                || echo "[deploy] no fast-forward (diverged?) — using current version" >&2
        fi
    else
        echo "[deploy] git fetch failed (offline?) — using current version" >&2
    fi
    export IZUMI_SELF_UPDATED=1
    exec "$0" "$@"
fi

PY="$(command -v python3 || command -v python)"
[ -z "$PY" ] && { echo "[deploy] no python interpreter found" >&2; exit 1; }
exec "$PY" run.py "$@"
