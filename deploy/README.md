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

## Web dashboard (read-only)

`webui.py` serves the reports dir (dashboard + JSON) over HTTP. Run it as a
container: `docker compose -f deploy/docker-compose.yml up -d` → `http://tower:8888`.
The `webdashboard` module regenerates `reports/index.html` each health/nightly run.

## MCP server (external assistant: Claude Desktop / Home Assistant)

`mcp_server.py` exposes izumi's read-only doctors + the guard-vetted apply as MCP
tools over stdio (stdlib JSON-RPC, no extra deps). ``apply_fix`` re-vets every
command against the same allow-list, so an external agent can't run anything outside
it. Point your MCP client at a `docker run` that launches it — e.g. Claude Desktop
`claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "izumi": {
      "command": "docker",
      "args": ["run", "--rm", "-i",
        "-v", "/var/run/docker.sock:/var/run/docker.sock",
        "-v", "/mnt/cache/appdata/scripts/plex_dupefinder:/app",
        "-v", "/mnt/user:/mnt/user", "-v", "/mnt/cache:/mnt/cache",
        "-w", "/app", "izumi-organizer:local", "python", "mcp_server.py"]
    }
  }
}
```

Then ask the assistant to run a doctor, list fixes, or apply an allow-listed one.
