import json
import logging
import threading
import time
import urllib.parse

import requests
import websocket

logger = logging.getLogger(__name__)

EVENTSUB_WS_URL = "wss://eventsub.wss.twitch.tv/ws"
TWITCH_TOKEN_URL = "https://id.twitch.tv/oauth2/token"
HELIX = "https://api.twitch.tv/helix"


class TwitchEventSubClient:
    def __init__(self, *, client_id, client_secret, access_token, refresh_token,
                 broadcaster_id, rewards, on_redemption, on_token_refresh):
        self._client_id = client_id
        self._client_secret = client_secret
        self._access_token = access_token
        self._refresh_token = refresh_token
        self._broadcaster_id = broadcaster_id
        self._rewards = dict(rewards or {})  # reward_id -> gamemode
        self._on_redemption = on_redemption
        self._on_token_refresh = on_token_refresh
        self._status = "connecting"
        self._stop = threading.Event()
        self._reconnect_url = None

    def start(self):
        t = threading.Thread(target=self._run_loop, daemon=True, name="twitch-eventsub")
        t.start()
        return t

    def stop(self):
        self._stop.set()

    @property
    def status(self):
        return self._status

    def set_rewards(self, rewards):
        self._rewards = dict(rewards or {})

    # ── Internal loop ──────────────────────────────────────────────────────

    def _run_loop(self):
        backoff = 1
        while not self._stop.is_set():
            url = self._reconnect_url or EVENTSUB_WS_URL
            self._reconnect_url = None
            try:
                self._connect(url)
            except Exception as e:
                logger.error("EventSub connection error: %s", e)
            if not self._stop.is_set():
                self._status = "reconnecting"
                logger.info("Reconnecting in %ds", backoff)
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 120)

    def _connect(self, url):
        ws = websocket.WebSocketApp(
            url,
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
        )
        ws.run_forever(ping_interval=30, ping_timeout=10)

    def _on_open(self, ws):
        logger.debug("Twitch WS opened")

    def _on_close(self, ws, code, msg):
        if not self._stop.is_set():
            self._status = "disconnected"
            logger.info("Twitch WS closed: %s %s", code, msg)

    def _on_error(self, ws, error):
        logger.error("Twitch WS error: %s", error)
        self._status = "error"

    def _on_message(self, ws, raw):
        try:
            msg = json.loads(raw)
        except Exception:
            return

        msg_type = msg.get("metadata", {}).get("message_type", "")
        payload = msg.get("payload", {})

        if msg_type == "session_welcome":
            session_id = payload["session"]["id"]
            if self._subscribe(ws, session_id):
                self._status = "connected"
            else:
                self._status = "error"
                ws.close()

        elif msg_type == "session_keepalive":
            pass

        elif msg_type == "notification":
            self._handle_notification(payload)

        elif msg_type == "session_reconnect":
            self._reconnect_url = payload["session"]["reconnect_url"]
            logger.info("Twitch requested reconnect to %s", self._reconnect_url)
            ws.close()

        elif msg_type == "revocation":
            logger.warning("Twitch subscription revoked: %s", payload)
            self._status = "error"

    def _handle_notification(self, payload):
        event = payload.get("event", {})
        reward_id = event.get("reward", {}).get("id", "")
        gamemode = None
        if self._rewards:
            if reward_id not in self._rewards:
                return
            gamemode = self._rewards[reward_id]
        username = event.get("user_input", "").strip()
        if username:
            logger.info("Redemption received for: %s (reward=%s, gamemode=%s)",
                        username, reward_id, gamemode)
            self._on_redemption(username, gamemode)

    # ── API helpers ─────────────────────────────────────────────────────────

    def _headers(self):
        return {
            "Authorization": f"Bearer {self._access_token}",
            "Client-Id": self._client_id,
            "Content-Type": "application/json",
        }

    def _subscribe(self, ws, session_id):
        payload = {
            "type": "channel.channel_points_custom_reward_redemption.add",
            "version": "1",
            "condition": {"broadcaster_user_id": self._broadcaster_id},
            "transport": {"method": "websocket", "session_id": session_id},
        }
        resp = requests.post(f"{HELIX}/eventsub/subscriptions",
                             headers=self._headers(), json=payload, timeout=10)
        if resp.status_code == 401 and self._refresh_tokens():
            resp = requests.post(f"{HELIX}/eventsub/subscriptions",
                                 headers=self._headers(), json=payload, timeout=10)
        if resp.status_code in (200, 202):
            logger.info("Subscribed to channel points redemptions (broadcaster %s)",
                        self._broadcaster_id)
            return True
        logger.error("Subscribe failed: %s %s", resp.status_code, resp.text)
        return False

    def _refresh_tokens(self):
        try:
            resp = requests.post(TWITCH_TOKEN_URL, data={
                "grant_type": "refresh_token",
                "refresh_token": self._refresh_token,
                "client_id": self._client_id,
                "client_secret": self._client_secret,
            }, timeout=10)
            if resp.ok:
                data = resp.json()
                self._access_token = data["access_token"]
                self._refresh_token = data.get("refresh_token", self._refresh_token)
                self._on_token_refresh(self._access_token, self._refresh_token)
                logger.info("Twitch tokens refreshed")
                return True
            logger.error("Token refresh failed: %s", resp.text)
        except Exception as e:
            logger.error("Token refresh error: %s", e)
        return False


# ── Standalone OAuth helpers ─────────────────────────────────────────────────

def build_auth_url(client_id, redirect_uri):
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "channel:read:redemptions",
    }
    return "https://id.twitch.tv/oauth2/authorize?" + urllib.parse.urlencode(params)


def exchange_code(client_id, client_secret, code, redirect_uri):
    resp = requests.post(TWITCH_TOKEN_URL, data={
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
    }, timeout=10)
    resp.raise_for_status()
    return resp.json()


def get_broadcaster_id(client_id, access_token, channel_name):
    resp = requests.get(f"{HELIX}/users",
                        headers={"Authorization": f"Bearer {access_token}",
                                 "Client-Id": client_id},
                        params={"login": channel_name}, timeout=10)
    resp.raise_for_status()
    users = resp.json().get("data", [])
    if not users:
        raise ValueError(f"Twitch channel '{channel_name}' not found")
    return users[0]["id"]


def get_rewards(client_id, access_token, broadcaster_id):
    resp = requests.get(f"{HELIX}/channel_points/custom_rewards",
                        headers={"Authorization": f"Bearer {access_token}",
                                 "Client-Id": client_id},
                        params={"broadcaster_id": broadcaster_id}, timeout=10)
    resp.raise_for_status()
    return [{"id": r["id"], "title": r["title"]}
            for r in resp.json().get("data", [])]
