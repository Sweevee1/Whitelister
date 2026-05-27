# Minecraft Twitch Whitelister

A Flask service that temporarily whitelists Twitch viewers on a Minecraft server when they redeem a channel points reward. Viewers are added for a configurable duration (default 2 hours), then automatically removed — server-side, so the timer survives the stream ending or the container restarting.

Twitch EventSub is handled directly via WebSocket — no public HTTPS endpoint or Streamer.Bot required. A web dashboard lets you manage the whitelist and configure everything without touching files.

## How it works

1. A viewer redeems a channel points reward and enters their Minecraft username.
2. The service receives the redemption via Twitch EventSub WebSocket.
3. It sends `whitelist add <username>` to Minecraft via AMP's REST API.
4. The username and expiry timestamp are saved to SQLite.
5. A background thread checks every 60 seconds for expired entries and removes them.
6. On restart, the service re-adds all non-expired entries to the whitelist (idempotent).

## Prerequisites

- Docker (Unraid has this built in)
- AMP (CubeCoders) managing the Minecraft instance
- `white-list=true` in Minecraft's `server.properties`
- A Twitch app registered at [dev.twitch.tv](https://dev.twitch.tv/console/apps)

## Deployment (Unraid)

The Docker image is built and published automatically to GitHub Container Registry on every push to `master`.

### 1. Create the config file

In Unraid, navigate to `/mnt/user/appdata/`, create a folder called `whitelister`, and inside it create `config.yaml`:

```yaml
amp:
  url: "http://192.168.1.x:8080"  # AMP ADS panel URL
  username: "admin"
  password: "changeme"
  instance_id: ""  # Set via the dashboard — don't edit manually

service:
  whitelist_duration_seconds: 7200   # 2 hours
  expiry_check_interval_seconds: 60
  secret_token: ""                   # Optional Bearer token for the HTTP endpoint

twitch:
  client_id: ""
  client_secret: ""
  channel_name: ""   # Your Twitch username
  reward_id: ""      # Leave empty to trigger on any redemption

database:
  path: "/config/whitelist.db"

logging:
  level: "INFO"
  file: ""
```

### 2. Add the container

Go to **Docker → Add Container**:

| Field | Value |
|-------|-------|
| Name | `whitelister` |
| Repository | `ghcr.io/sweevee1/whitelister:latest` |
| Port mapping 1 | Host: `8764` → Container: `8764` |
| Port mapping 2 | Host: `8765` → Container: `8765` |
| Volume | Host: `/mnt/user/appdata/whitelister` → Container: `/config` |

Port 8764 is a plain HTTP convenience port that redirects to HTTPS. Port 8765 is the main HTTPS port (self-signed cert — your browser will show a security warning on first visit; click through to accept it).

### 3. Configure AMP

Open `https://<Unraid-IP>:8765` in your browser. Go to **Settings → AMP Connection**, click **Load**, then click **Use** next to your Minecraft instance. Click **Save**.

### 4. Connect Twitch

**In the Twitch developer console** ([dev.twitch.tv/console/apps](https://dev.twitch.tv/console/apps)), open your app and add this OAuth Redirect URL:

```
https://<Unraid-IP>:8765/twitch/callback
```

**In the dashboard**, go to **Settings → Twitch Integration**. Enter your Client ID, Client Secret, and Twitch channel name. Leave the Redirect URI field blank (auto-detected). Click **Connect Twitch**, log in, and authorise. The dashboard will show the connection status once the EventSub WebSocket is established.

Twitch tokens are saved automatically to `config.yaml` — you don't need to reconnect after a container restart.

## API

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/` | Web dashboard |
| GET | `/health` | Liveness check |
| POST | `/whitelist` | Add player; optional `duration_seconds` body field |
| DELETE | `/whitelist/<username>` | Remove player |
| GET | `/settings` | Get current config (minus tokens) |
| POST | `/settings` | Save config |
| GET | `/amp/status` | Test AMP connectivity |
| POST | `/twitch/auth` | Save Twitch credentials + return OAuth URL |
| GET | `/twitch/callback` | OAuth callback |
| GET | `/twitch/status` | EventSub connection status |
| GET | `/twitch/rewards` | List channel point rewards |
| POST | `/twitch/disconnect` | Clear tokens + stop client |

`POST /whitelist` body: `{"username": "Steve"}` — optionally include `"duration_seconds": 3600` to override the config default for that entry. If `secret_token` is set in config, include `Authorization: Bearer <token>`.

## Development

```powershell
# Windows (PowerShell)
$env:CONFIG_PATH="config.dev.yaml"
venv\Scripts\flask --app "whitelister.app:create_app()" run --port 8765
```

The dev server runs plain HTTP. To test expiry quickly, set `whitelist_duration_seconds: 10` and `expiry_check_interval_seconds: 5` in the dev config.
