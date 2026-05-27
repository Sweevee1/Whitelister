# Minecraft Twitch Whitelister

A Flask service that temporarily whitelists Twitch viewers on a Minecraft server when they redeem a channel points reward. Viewers are added for 2 hours, then automatically removed — server-side, so the timer survives the stream ending or the container restarting.

Twitch EventSub is handled directly by this service via WebSocket — no public HTTPS endpoint required. A web dashboard lets you manage the whitelist and configure everything without touching files.

## How it works

1. A viewer redeems a channel points reward and enters their Minecraft username.
2. The service receives the redemption via Twitch EventSub WebSocket.
3. It sends `whitelist add <username>` to Minecraft via AMP's REST API.
4. The username and expiry timestamp are saved to SQLite.
5. A background thread checks every 60 seconds for expired entries and sends `whitelist remove <username>` for each one.
6. On restart, the service re-adds all non-expired entries to the whitelist (idempotent).

```
Viewer redeems channel points reward
        │
        ▼
  Twitch EventSub WebSocket
        │
        ▼
  Whitelister (Docker container on Unraid)
  ├── validates username (3–16 chars, a–z/0–9/_)
  ├── calls AMP API → whitelist add <username>
  └── saves username + expires_at to SQLite
        │
        ▼  (background thread, every 60s)
  check SQLite for expired entries
  └── calls AMP API → whitelist remove <username>
      └── deletes entry from SQLite
```

## Prerequisites

- Docker (Unraid has this built in)
- AMP (CubeCoders) managing the Minecraft instance
- `white-list=true` set in Minecraft's `server.properties`
- A Twitch app registered at [dev.twitch.tv](https://dev.twitch.tv/console/apps)

## Deployment (Unraid)

The Docker image is built and published automatically to GitHub Container Registry on every push to this repo.

**1. Create the config file:**

In the Unraid web UI, go to **Files** and navigate to `/mnt/user/appdata/`. Create a new folder called `whitelister`, then inside it create a file called `config.yaml`. Paste in the contents from [config.yaml](config.yaml) in this repo and update at minimum:
- `amp.url` — your AMP instance URL for the Minecraft server (e.g. `http://192.168.1.50:8080`)
- `amp.username` / `amp.password` — your AMP credentials

**2. Add the container:**

Go to **Docker** and click **Add Container**. Fill in:

| Field | Value |
|-------|-------|
| Name | `whitelister` |
| Repository | `ghcr.io/sweevee1/whitelister:latest` |
| Port | Host: `8765` → Container: `8765` |
| Volume | Host: `/mnt/user/appdata/whitelister` → Container: `/config` |

Click **Apply**. Unraid will pull the image and start the container.

**3. Connect Twitch via the dashboard:**

Open `http://<Unraid-IP>:8765` in your browser and follow the Twitch setup flow. You'll need your Twitch app's client ID and secret — set the OAuth Redirect URL in your Twitch app to `http://<Unraid-IP>:8765/twitch/callback`.

## Configuration

`/mnt/user/appdata/whitelister/config.yaml`:

```yaml
amp:
  url: "http://192.168.1.x:8080"  # AMP instance URL for the Minecraft server
  username: "admin"
  password: "changeme"

service:
  whitelist_duration_seconds: 7200   # 2 hours
  expiry_check_interval_seconds: 60
  secret_token: ""                   # Optional Bearer token for the HTTP endpoint

twitch:
  client_id: ""       # From dev.twitch.tv
  client_secret: ""
  channel_name: ""    # Your Twitch username (lowercase)
  reward_id: ""       # Leave empty to trigger on any redemption

database:
  path: "/config/whitelist.db"   # Don't change — maps to appdata

logging:
  level: "INFO"
  file: ""   # Empty = stdout/Docker logs only (recommended)
```

Twitch tokens (`access_token`, `refresh_token`, `broadcaster_id`) are written automatically by the dashboard OAuth flow.

## Dashboard

The web dashboard at `http://<Unraid-IP>:8765` lets you:

- View and manually remove active whitelist entries
- Connect/disconnect Twitch EventSub
- Configure AMP and Twitch settings
- Check AMP connectivity

## API

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/` | Web dashboard |
| GET | `/health` | Liveness check |
| POST | `/whitelist` | Add player manually |
| DELETE | `/whitelist/<username>` | Remove player |
| GET | `/settings` | Get current config (minus tokens) |
| POST | `/settings` | Save config |
| GET | `/amp/status` | Test AMP connectivity |
| POST | `/twitch/auth` | Save Twitch credentials + return OAuth URL |
| GET | `/twitch/callback` | OAuth callback |
| GET | `/twitch/status` | EventSub connection status |
| GET | `/twitch/rewards` | List channel point rewards |
| POST | `/twitch/disconnect` | Clear tokens + stop client |

### `POST /whitelist`

```json
{"username": "Steve"}
```

Response:
```json
{"status": "ok", "username": "Steve", "expires_at": "2026-05-26T14:00:00Z"}
```

| Status | Meaning |
|--------|---------|
| 400 | Invalid or missing username |
| 401 | Missing or wrong Bearer token |
| 503 | AMP is unreachable |

If `secret_token` is set in config, include `Authorization: Bearer <token>` in the request.

## Development

```bash
python3 -m venv venv
venv/bin/pip install -r requirements.txt

# Windows (PowerShell)
$env:CONFIG_PATH="config.dev.yaml"
venv\Scripts\flask --app "whitelister.app:create_app()" run --port 8765
```

To test expiry quickly, set in your dev config:
```yaml
service:
  whitelist_duration_seconds: 10
  expiry_check_interval_seconds: 5
```

## Architecture notes

- All shared state is created in `create_app()` and passed explicitly — no module-level globals.
- Two locks: `db_lock` serialises SQLite writes; `AMPClient._lock` serialises AMP session refresh.
- SQLite uses WAL mode so the expiry thread and request handlers don't block each other.
- AMP auth is session-based (`SESSIONID` body parameter on every request). The client auto-re-authenticates on session expiry.
- The Twitch EventSub client auto-reconnects with exponential backoff and refreshes tokens on 401.
- If AMP fails during expiry removal, the entry stays in SQLite and is retried next cycle.
