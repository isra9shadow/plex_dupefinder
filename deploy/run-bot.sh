#!/bin/bash
# izumi Telegram ops bot — keep-alive wrapper.
#
# Runs on the Unraid HOST (NOT in a container): the bot spawns `docker run` for each
# operation with the repo's HOST paths, so it must run where those paths resolve.
#
# Reboot-proof: add an Unraid User Script (Settings -> User Scripts) whose body is
#   bash /mnt/cache/appdata/scripts/plex_dupefinder/deploy/run-bot.sh
# set to schedule "At Startup of Array" (and "Run in Background" to start it now).
#
# Manage:
#   tail -f <repo>/bot.out              # live logs (on cache, never /var/log)
#   pkill -f run-bot.sh; pkill -f 'python3 bot.py'   # stop
#
# The pgrep guard prevents two pollers on the same token (Telegram returns 409).

REPO="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
cd "$REPO" || exit 1

if pgrep -f "python3 bot.py" >/dev/null; then
  echo "izumi bot already running"
  exit 0
fi

LOG="$REPO/bot.out"
: >"$LOG"  # truncate on (re)start so the log can't grow unbounded

while true; do
  echo "[$(date)] starting izumi bot" >>"$LOG"
  python3 bot.py >>"$LOG" 2>&1
  echo "[$(date)] bot exited rc=$? — restart in 10s" >>"$LOG"
  sleep 10
done
