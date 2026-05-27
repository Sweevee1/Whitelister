# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Python Flask service that runs as a Docker container on Unraid. Twitch viewers redeem a channel points reward and enter a Minecraft username — the service adds them to the Minecraft whitelist via AMP's API for a configurable duration, then removes them automatically. Twitch EventSub is handled directly by this service (no Streamer.Bot required). A web dashboard lets the streamer manage the whitelist and configure everything without touching files.

## Running locally (development)

```bash
python3 -m venv venv
venv/bin/pip install -r requirements.txt  # or venv\Scripts\pip on Windows

# Uses config.dev.yaml (local dev config with real AMP IP + credentials)
CONFIG_PATH=config.dev.yaml venv/bin/flask --app "whitelister.app:create_app()" run --port 8765
```

On Windows (PowerShell):
```powershell
$env:CONFIG_PATH="config.dev.yaml"
venv\Scripts\flask --app "whitelister.app:create_app()" run --port 8765
```

To test expiry quickly, set `whitelist_duration_seconds: 10` and `expiry_check_interval_seconds: 5` in the dev config.

## Production deployment (Docker on Unraid)

```bash
# config.yaml goes in /mnt/user/appdata/whitelister/config.yaml
docker compose up -d --build
```

The container mounts `/mnt/user/appdata/whitelister` as `/config` inside the container. Config path defaults to `/config/config.yaml` via `CONFIG_PATH` env var set in the Dockerfile.

## Architecture

All shared state lives in `create_app()` in `app.py` and is passed explicitly — no module-level globals.

**Concurrent writers share two locks:**
- `db_lock: threading.Lock` — serialises all SQLite writes (request handler + expiry thread).
- `AMPClient._lock: threading.Lock` — serialises AMP session refresh only.

**Request flow:** `POST /whitelist` → validate username regex → `AMPClient.whitelist_add()` → `upsert_entry()` in SQLite → return JSON. Accepts optional `duration_seconds` body field to override the config default for that specific add.

**Twitch redemption flow:** `_whitelist_player()` is called by the EventSub client — uses the config default duration only (no per-redemption override).

**Twitch flow:** `TwitchEventSubClient` background thread (in `twitch_client.py`) maintains a WebSocket connection to Twitch EventSub. On a channel points redemption, it calls `_whitelist_player()` directly — the same helper used by the HTTP endpoint.

**Expiry flow:** daemon thread wakes every N seconds → `get_expired_entries()` → `AMPClient.whitelist_remove()` per entry → `delete_entry()`. Failed removals stay in DB and are retried next cycle.

**Startup:** `_restore_whitelist()` re-adds every non-expired DB entry to the Minecraft whitelist (idempotent). Twitch client auto-starts if `access_token` + `broadcaster_id` are present in config.

## Modules

- `app.py` — Flask app factory, all routes, shared helpers `_whitelist_player()` and `_save_twitch_tokens()`
- `amp_client.py` — AMP session auth + `whitelist add/remove` via `Core/SendConsoleMessage`
- `twitch_client.py` — Twitch EventSub WebSocket client, OAuth helpers (`build_auth_url`, `exchange_code`, `get_broadcaster_id`, `get_rewards`)
- `database.py` — SQLite with WAL mode; `upsert_entry`, `get_expired_entries`, `delete_entry`, `get_all_entries`
- `scheduler.py` — background expiry thread

## AMP API

All Minecraft interaction goes through AMP's REST API (`Core/SendConsoleMessage`), not RCON.

**Auth is session-based.** `POST /API/Core/Login` returns a `sessionID` at the **top level** of the response (not nested in `result`). The `SESSIONID` must be included as a **body parameter** in every subsequent request — not as a cookie or header. `AMPClient._post()` handles this automatically.

**ADS proxy requires two-step auth.** When `instance_id` is configured, commands are routed through the ADS proxy at `/API/ADSModule/Servers/{INSTANCE_ID}/API/{endpoint}`. This requires logging in **twice**:
1. `POST /API/Core/Login` on the ADS panel → ADS `sessionID`
2. `POST /API/ADSModule/Servers/{INSTANCE_ID}/API/Core/Login` (passing the ADS session) → sub-instance `sessionID`

Only the sub-instance session is accepted for proxied commands. Passing the ADS session directly returns HTTP 200 with an "Unauthorized Access / Session.Exists" error body — indistinguishable from success at the HTTP level. `AMPClient._proxy_login()` handles step 2 automatically after every `_login()` call when `instance_id` is set. Both sessions are cleared together on expiry and re-acquired via `_login()`.

**Session expiry detection** is in `_is_session_expired()`. Detects: HTTP 401, `Status == 5`, "not logged in" in Title, and "Unauthorized Access" + "Session.Exists" in the body (sub-session expiry via proxy). If commands silently fail, add debug logging there first.

**Config:** Set `amp.url` to the ADS panel URL (e.g. `http://192.168.1.50:8080`) and `amp.instance_id` to the Minecraft instance GUID. Use the dashboard's **Load** button under Settings → AMP Connection to enumerate instances and pick the right GUID.

## Twitch integration

Uses EventSub WebSocket transport — no public HTTPS endpoint required.

OAuth flow: `POST /twitch/auth` saves credentials + redirects to Twitch → `GET /twitch/callback` exchanges the code for tokens + broadcaster ID + starts the client. Tokens are saved back to `config.yaml` automatically. The client auto-reconnects with exponential backoff and refreshes tokens on 401.

`reward_id` in config filters events to a specific channel points reward. Leave empty to respond to all redemptions.

## Dashboard routes

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/` | Web dashboard |
| GET | `/health` | Liveness check |
| GET | `/whitelist` | List active players as JSON `{players: [{username, expires_at, expires_str}]}` |
| POST | `/whitelist` | Add player; optional `duration_seconds` body field overrides config default |
| DELETE | `/whitelist/<username>` | Remove player |
| GET | `/settings` | Get current config (minus auth tokens) |
| POST | `/settings` | Save config; hot-reloads AMPClient immediately (no restart needed) |
| GET | `/amp/status` | Test AMP connectivity; returns `status`, `module`, `state` |
| GET | `/amp/instances` | List ADS instances with `instance_id`, `name`, `module`, `running`, `url`, `port` |
| POST | `/twitch/auth` | Save Twitch credentials + return OAuth URL |
| GET | `/twitch/callback` | OAuth callback — exchanges code, starts client |
| GET | `/twitch/status` | Current EventSub connection status |
| GET | `/twitch/rewards` | List channel point rewards (requires connected) |
| POST | `/twitch/disconnect` | Clear tokens + stop client |

## Dashboard UI

Single-file template at `whitelister/templates/index.html`. Dark grey theme (`--bg: #dde3ea`, `--surface: #eaeff4`). All CSS variables are in the `:root` block at the top of the file.

**Players panel** — username input + separate hours/minutes number fields + "Add" button. The duration fields default to the config value and are sent as `duration_seconds` in the POST body. The panel polls `GET /whitelist` every 8 seconds while the Players tab is active — no page reload needed.

**Settings panel** — Whitelist Duration is split into separate `h` and `m` inputs (`s-duration-h`, `s-duration-m`). The settings form has `autocomplete="off"` to suppress browser HTTP password-autofill warnings. Settings save hot-reloads AMPClient via `amp.reconfigure()` — interval and logging changes still require a restart.

**AMP Connection section** — has a **Load** button that calls `GET /amp/instances` and renders a picker. Clicking **Use** next to an instance writes its GUID into the hidden `s-amp-instance` input (the ADS proxy approach). The AMP URL field stays unchanged.

**Running the dev server** — always use PowerShell (not bash) to start Flask on Windows, otherwise the Windows Python process won't be killed cleanly by bash `kill` and multiple instances will stack up on port 8765. Kill stale processes with: `netstat -ano | findstr ":8765"` then `Stop-Process -Id <PID> -Force`.

## Key constraint

`white-list=true` must be set in Minecraft's `server.properties` for the whitelist to gate access. This service cannot enforce that setting itself.
