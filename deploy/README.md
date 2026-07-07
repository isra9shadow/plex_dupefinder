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

## Web dashboard

`webui.py` serves the reports dir (dashboard + JSON). Read-only container:
`docker compose -f deploy/docker-compose.yml up -d` → `http://tower:8888`. The
`webdashboard` module regenerates `index.html` each health/nightly run.

### Optional: interactive buttons (opt-in, token-guarded)

To let the panel's "Ejecutar salud" / "Refrescar panel" buttons trigger read-only
runs, start webui with a token + the docker socket + a **writable** reports mount
(only read-only modules can be triggered — never apply/dbrepair):

```bash
REPO=/mnt/cache/appdata/scripts/plex_dupefinder
REPORTS=$(python3 -c "import json;print(json.load(open('$REPO/config/config.json'))['reporting']['dir'])")
docker rm -f izumi-webui 2>/dev/null
docker run -d --name izumi-webui --restart unless-stopped \
  -e IZUMI_WEB_TOKEN="pon-un-token-largo" \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v "$REPO:/app" -v "$REPORTS:/reports" -v /mnt/user:/mnt/user -v /mnt/cache:/mnt/cache \
  -w /app -p 8888:8888 izumi-organizer:local python webui.py --dir /reports --port 8888
```

The browser asks for the token once (kept in localStorage, never in the page). No
`IZUMI_WEB_TOKEN` → the API is off and the server stays purely read-only.

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

## External guardian PC (monitor from outside + operate the server)

A second always-on PC (a Windows box, a laptop, a mini-PC) on the same LAN can act as
an **external guardian**: it watches the server *from the outside* — so it can still
warn you when the server (and the on-box bot) is completely down — and it can also
*operate* the server remotely. Two halves, both stdlib, both in this repo:

### 1. Watch — `sentinel.py` (knows WHAT failed, not just that the box is up)

Drop `sentinel.py` + a `.env` on the guardian PC and run it. It probes the server and
its services and alerts over Telegram on state changes. If you also set
`IZUMI_SENTINEL_PANEL` to the web panel URL, then — while the server is up — it pulls
`GET /api/status` and alerts **per failing module with its failure count** (and on
recovery), debounced so it never spams. So a down server gives you one clear "🚨 no
responde" alarm; a *running* server with a broken module gives you "⚠️ Módulo con
fallos: dbcheck (2 fallos)".

```ini
# .env next to sentinel.py on the guardian PC
IZUMI_TELEGRAM_BOT_TOKEN=...            # reuse the main bot token (send-only, no clash)
IZUMI_TELEGRAM_CHAT_ID=...
IZUMI_SENTINEL_SERVER=192.168.6.62:443  # master up/down check
IZUMI_SENTINEL_TARGETS=plex=192.168.6.62:32400,sonarr=192.168.6.62:8989
IZUMI_SENTINEL_PANEL=http://192.168.6.62:8888   # optional: report which module fails
IZUMI_SENTINEL_PANEL_AUTH=              # "user:pass" only if the panel has Basic Auth
```

```bash
python sentinel.py          # stays open; pythonw sentinel.py / Scheduled Task = background
```

### 2. Operate — the token API (or bot) reaches back into the server

The guardian *acts* on the server through the same guarded surfaces used by the panel:

- **Web token API** (`webui.py` with `IZUMI_WEB_TOKEN`): `POST /api/run` (whitelisted
  read-only doctors), `POST /api/ask` (the assistant), `POST /api/apply` (a fix). Every
  apply is re-vetted against the `aictx.apply` allow-list, so even from off-box the
  guardian can only run `docker restart/start/stop`, `chmod`, `chown`, `mkdir` — never
  anything destructive. Example from the guardian:
  ```bash
  curl -s -XPOST http://192.168.6.62:8888/api/apply \
    -H 'content-type: application/json' \
    -d '{"token":"<IZUMI_WEB_TOKEN>","command":"docker restart sonarr"}'
  ```
- **Telegram bot** (`bot.py` on the server): same routing, driven from your phone.

So the guardian both **alerts with the error detail** (sentinel + `/api/status`) and
**operates the server** (token API / bot) — all read-only or allow-list-gated, with the
`aictx.apply` allow-list as the only action boundary in every path.
