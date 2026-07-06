# deploy/ — always-on services on Unraid

izumi is normally driven on-demand (SSH menu, nightly User Script). A couple of
pieces are meant to run **continuously**; this folder holds their launchers.

## Telegram ops bot (continuous)

`run-bot.sh` keeps `bot.py` alive on the **host** (it must run on the host, not in a
container, because it spawns `docker run` with the repo's host paths).

1. Set the real secrets in `.env` (repo root): `IZUMI_TELEGRAM_BOT_TOKEN`,
   `IZUMI_TELEGRAM_CHAT_ID`, `IZUMI_TELEGRAM_ALLOWED_IDS`.
2. Start now (SSH):
   ```bash
   nohup bash deploy/run-bot.sh >/dev/null 2>&1 &
   ```
3. Reboot-proof: Unraid → Settings → User Scripts → Add New Script, body:
   ```bash
   bash /mnt/cache/appdata/scripts/plex_dupefinder/deploy/run-bot.sh
   ```
   Schedule **"At Startup of Array"**, then **"Run in Background"**.

Logs: `<repo>/bot.out` (on cache, never `/var/log`). Stop:
`pkill -f run-bot.sh; pkill -f 'python3 bot.py'`.

## Coming (later phases)

The web dashboard (`webui.py`) and MCP server (`mcp_server.py`) are read-only /
in-process and CAN run as containers; a `docker-compose.yml` with
`restart: unless-stopped` will be added here when they land.
