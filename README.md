# Minecraft Twitch Whitelister

A Flask service that temporarily whitelists Twitch viewers on a Minecraft server when they redeem a channel points reward. Viewers are added for 2 hours, then automatically removed — server-side, so the timer survives the stream ending or Streamer.Bot restarting.

## How it works

1. A viewer redeems a channel points reward in Twitch chat and enters their Minecraft username.
2. Streamer.Bot POSTs the username to this service's `/whitelist` endpoint.
3. The service sends `whitelist add <username>` to Minecraft via AMP's REST API.
4. The username and expiry timestamp are saved to SQLite.
5. A background thread checks every 60 seconds for expired entries and sends `whitelist remove <username>` for each one.
6. On restart, the service re-adds all non-expired entries to the whitelist (idempotent — Minecraft ignores duplicates).

```
Twitch viewer redeems reward
        │
        ▼
  Streamer.Bot
  POST /whitelist {"username": "Steve"}
        │
        ▼
  whitelister (Docker container on Unraid)
  ├── validates username (3–16 chars, a–z/0–9/_)
  ├── calls AMP API on Debian VM → whitelist add Steve
  └── saves Steve + expires_at to SQLite
        │
        ▼  (background thread, every 60s)
  check SQLite for expired entries
  └── calls AMP API → whitelist remove Steve
      └── deletes entry from SQLite
```

## Prerequisites

- Docker (Unraid has this built in)
- AMP (CubeCoders) managing the Minecraft instance on your Debian VM
- `white-list=true` set in Minecraft's `server.properties`
- Streamer.Bot configured to POST to this service on reward redemption

## Deployment (Unraid)

**1. Create the appdata directory and drop in your config:**

```bash
mkdir -p /mnt/user/appdata/whitelister
cp config.yaml /mnt/user/appdata/whitelister/config.yaml
```

Edit `/mnt/user/appdata/whitelister/config.yaml` — at minimum set:
- `amp.url` — your Debian VM's LAN IP and AMP port (e.g. `http://192.168.1.50:8080`)
- `amp.username` / `amp.password` — your AMP credentials

**2. Build and start the container:**

```bash
docker compose up -d --build
```

Check it's running:
```bash
docker compose logs -f
curl http://localhost:8765/health
```

The SQLite database is created automatically at `/mnt/user/appdata/whitelister/whitelist.db`.

## Configuration

`/mnt/user/appdata/whitelister/config.yaml`:

```yaml
amp:
  url: "http://192.168.1.x:8080"  # Debian VM's LAN IP and AMP port
  username: "admin"
  password: "changeme"

service:
  whitelist_duration_seconds: 7200   # 2 hours
  expiry_check_interval_seconds: 60
  secret_token: ""                   # Optional Bearer token for auth

database:
  path: "/config/whitelist.db"       # Don't change — maps to appdata

logging:
  level: "INFO"
  file: ""   # Empty = stdout/Docker logs only (recommended)
```

If `secret_token` is set, Streamer.Bot must send `Authorization: Bearer <token>` with every request.

## Streamer.Bot setup

Add a "POST Request" action triggered by the channel points reward:

- **URL:** `http://<Unraid-IP>:8765/whitelist`
- **Method:** POST
- **Headers:** `Content-Type: application/json` (add `Authorization: Bearer <token>` if using `secret_token`)
- **Body:** `{"username": "%rewardMessage%"}`

`%rewardMessage%` is the text the viewer typed when redeeming the reward.

## API

### `GET /health`
Returns `{"status": "ok"}`. Use this to verify the service is up.

### `POST /whitelist`
Adds a username to the whitelist.

**Request:**
```json
{"username": "Steve"}
```

**Response (success):**
```json
{"status": "ok", "username": "Steve", "expires_at": "2026-05-26T14:00:00Z"}
```

**Response (errors):**
| Status | Meaning |
|--------|---------|
| 400 | Invalid or missing username |
| 401 | Missing or wrong Bearer token |
| 503 | AMP is unreachable |

## Development

```bash
python3 -m venv venv
venv/bin/pip install -r requirements.txt
venv/bin/flask --app "whitelister.app:create_app()" run --port 8765
```

To test expiry without waiting 2 hours, set in `config.yaml`:
```yaml
service:
  whitelist_duration_seconds: 10
  expiry_check_interval_seconds: 5
```

## Architecture notes

- All shared state is created in `create_app()` and passed explicitly — no module-level globals.
- SQLite uses WAL mode so the expiry thread and request handlers don't block each other on reads.
- AMP auth is session-based (login once, store `SESSIONID` cookie). The client auto-re-authenticates on session expiry.
- If AMP fails during an expiry removal, the entry stays in SQLite and is retried on the next cycle.
- The config path defaults to `CONFIG_PATH` env var, falling back to `config.yaml`. The Docker image sets `CONFIG_PATH=/config/config.yaml`.
