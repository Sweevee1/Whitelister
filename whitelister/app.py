import logging
import logging.handlers
import os
import re
import threading
import time
from datetime import datetime, timezone

import yaml
from flask import Flask, jsonify, redirect, render_template, request

from .amp_client import AMPAuthError, AMPCommandError, AMPClient
from .database import delete_entry, get_all_entries, init_db, upsert_entry
from .scheduler import start_expiry_thread
from .twitch_client import (
    TwitchEventSubClient,
    build_auth_url,
    exchange_code,
    get_broadcaster_id,
    get_rewards,
)

USERNAME_RE = re.compile(r"^[a-zA-Z0-9_]{3,16}$")


def _setup_logging(config: dict) -> None:
    level_name = config.get("logging", {}).get("level", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    handlers = [logging.StreamHandler()]
    log_file = config.get("logging", {}).get("file", "")
    if log_file:
        os.makedirs(os.path.dirname(log_file), exist_ok=True)
        handlers.append(
            logging.handlers.RotatingFileHandler(
                log_file, maxBytes=10_000_000, backupCount=3
            )
        )
    logging.basicConfig(
        level=level,
        handlers=handlers,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )


def _restore_whitelist(conn, amp_client: AMPClient, db_lock: threading.Lock) -> None:
    logger = logging.getLogger(__name__)
    with db_lock:
        entries = get_all_entries(conn)
    now = int(time.time())
    for username, _, expires_at in entries:
        if expires_at > now:
            try:
                amp_client.whitelist_add(username)
                exp_iso = datetime.fromtimestamp(expires_at, tz=timezone.utc).isoformat()
                logger.info("Restored whitelist entry: %s (expires %s)", username, exp_iso)
            except (AMPAuthError, AMPCommandError) as e:
                logger.error("Failed to restore %s: %s", username, e)


def create_app(config_path: str = None) -> Flask:
    if config_path is None:
        config_path = os.environ.get("CONFIG_PATH", "config.yaml")
    with open(config_path) as f:
        config = yaml.safe_load(f)

    _setup_logging(config)
    logger = logging.getLogger(__name__)

    conn = init_db(config["database"]["path"])
    db_lock = threading.Lock()
    amp = AMPClient(
        base_url=config["amp"]["url"],
        username=config["amp"]["username"],
        password=config["amp"]["password"],
    )
    secret_token: str = config["service"].get("secret_token", "") or ""
    duration: int = int(config["service"]["whitelist_duration_seconds"])

    _restore_whitelist(conn, amp, db_lock)
    start_expiry_thread(
        conn, amp, db_lock, int(config["service"]["expiry_check_interval_seconds"])
    )

    # Mutable container so nested functions can reassign the client reference
    twitch_state = {"client": None}

    app = Flask(__name__)

    # ── Shared helpers ───────────────────────────────────────────────────────

    def _whitelist_player(username: str) -> bool:
        if not USERNAME_RE.match(username):
            logger.warning("Ignored invalid username from Twitch: %r", username)
            return False
        try:
            amp.whitelist_add(username)
        except (AMPAuthError, AMPCommandError) as e:
            logger.error("AMP error whitelisting %s: %s", username, e)
            return False
        now = int(time.time())
        expires_at = now + duration
        with db_lock:
            upsert_entry(conn, username, now, expires_at)
        dt = datetime.fromtimestamp(expires_at, tz=timezone.utc)
        logger.info("Whitelisted %s via Twitch until %s", username, dt.isoformat())
        return True

    def _save_twitch_tokens(access_token: str, refresh_token: str) -> None:
        with open(config_path) as f:
            cfg = yaml.safe_load(f)
        cfg.setdefault("twitch", {})
        cfg["twitch"]["access_token"] = access_token
        cfg["twitch"]["refresh_token"] = refresh_token
        with open(config_path, "w") as f:
            yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True)

    def _start_twitch_client(twitch_cfg: dict) -> None:
        if twitch_state["client"]:
            twitch_state["client"].stop()
            twitch_state["client"] = None
        client = TwitchEventSubClient(
            client_id=twitch_cfg["client_id"],
            client_secret=twitch_cfg["client_secret"],
            access_token=twitch_cfg["access_token"],
            refresh_token=twitch_cfg["refresh_token"],
            broadcaster_id=twitch_cfg["broadcaster_id"],
            reward_id=twitch_cfg.get("reward_id", ""),
            on_redemption=_whitelist_player,
            on_token_refresh=_save_twitch_tokens,
        )
        client.start()
        twitch_state["client"] = client
        logger.info("Twitch EventSub client started")

    def _twitch_redirect_uri() -> str:
        return request.url_root.rstrip("/") + "/twitch/callback"

    # Auto-start Twitch if fully configured
    twitch_cfg = config.get("twitch", {})
    if (twitch_cfg.get("client_id") and twitch_cfg.get("access_token")
            and twitch_cfg.get("broadcaster_id")):
        try:
            _start_twitch_client(twitch_cfg)
        except Exception as e:
            logger.error("Failed to start Twitch client: %s", e)

    # ── Routes ───────────────────────────────────────────────────────────────

    @app.route("/", methods=["GET"])
    def dashboard():
        now = int(time.time())
        with db_lock:
            entries = get_all_entries(conn)
        players = []
        for username, added_at, expires_at in entries:
            if expires_at > now:
                dt = datetime.fromtimestamp(expires_at, tz=timezone.utc)
                players.append({
                    "username": username,
                    "expires_at": expires_at,
                    "expires_str": f"{dt.hour}:{dt.minute:02d} UTC",
                })
        players.sort(key=lambda p: p["expires_at"])

        with open(config_path) as f:
            cfg = yaml.safe_load(f)

        client = twitch_state["client"]
        twitch_status = client.status if client else "disconnected"

        return render_template(
            "index.html",
            players=players,
            duration_hours=duration // 3600,
            config=cfg,
            secret_token=secret_token,
            twitch_status=twitch_status,
        )

    @app.route("/health", methods=["GET"])
    def health():
        return jsonify({"status": "ok"})

    @app.route("/amp/status")
    def amp_status():
        try:
            amp.ensure_session()
            return jsonify({"status": "connected"})
        except AMPAuthError as e:
            msg = str(e)
            reason = "unreachable" if "request failed" in msg else "auth_failed"
            return jsonify({"status": "error", "reason": reason, "message": msg})
        except Exception as e:
            return jsonify({"status": "error", "reason": "unreachable", "message": str(e)})

    # ── Settings ─────────────────────────────────────────────────────────────

    @app.route("/settings", methods=["GET"])
    def get_settings():
        with open(config_path) as f:
            cfg = yaml.safe_load(f)
        return jsonify({
            "amp": cfg.get("amp", {}),
            "service": cfg.get("service", {}),
            "logging": cfg.get("logging", {}),
            "twitch": {k: v for k, v in cfg.get("twitch", {}).items()
                       if k not in ("access_token", "refresh_token", "broadcaster_id")},
        })

    @app.route("/settings", methods=["POST"])
    def save_settings():
        data = request.get_json(silent=True)
        if data is None:
            return jsonify({"status": "error", "message": "invalid JSON"}), 400
        try:
            assert data.get("amp", {}).get("url"), "AMP URL is required"
            assert data.get("amp", {}).get("username"), "AMP username is required"
            int(data["service"]["whitelist_duration_seconds"])
            int(data["service"]["expiry_check_interval_seconds"])
        except (KeyError, TypeError, AssertionError) as e:
            return jsonify({"status": "error", "message": str(e)}), 400

        with open(config_path) as f:
            existing = yaml.safe_load(f)

        existing["amp"] = data["amp"]
        existing["service"] = data["service"]
        if "logging" in data:
            existing["logging"] = data["logging"]

        # Twitch: only update user-visible fields; preserve auth tokens
        if "twitch" in data:
            existing.setdefault("twitch", {})
            for field in ("client_id", "client_secret", "channel_name", "reward_id"):
                if field in data["twitch"]:
                    existing["twitch"][field] = data["twitch"][field]
            if twitch_state["client"]:
                twitch_state["client"].set_reward_id(
                    data["twitch"].get("reward_id", ""))

        with open(config_path, "w") as f:
            yaml.dump(existing, f, default_flow_style=False, allow_unicode=True)

        logger.info("Settings updated via dashboard")
        return jsonify({"status": "ok"})

    # ── Whitelist API ─────────────────────────────────────────────────────────

    @app.route("/whitelist", methods=["POST"])
    def add_to_whitelist():
        if secret_token:
            auth = request.headers.get("Authorization", "")
            if auth != f"Bearer {secret_token}":
                return jsonify({"status": "error", "message": "unauthorized"}), 401

        data = request.get_json(silent=True)
        if data is None:
            return jsonify({"status": "error", "message": "invalid JSON"}), 400

        username = str(data.get("username", "")).strip()
        if not USERNAME_RE.match(username):
            return jsonify({
                "status": "error",
                "message": "invalid username (3-16 chars, letters/numbers/underscore only)",
            }), 400

        try:
            amp.whitelist_add(username)
        except (AMPAuthError, AMPCommandError) as e:
            logger.error("AMP error adding %s: %s", username, e)
            return jsonify({"status": "error", "message": "AMP unreachable"}), 503

        now = int(time.time())
        expires_at = now + duration
        with db_lock:
            upsert_entry(conn, username, now, expires_at)

        expires_iso = (
            datetime.fromtimestamp(expires_at, tz=timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )
        logger.info("Whitelisted %s until %s", username, expires_iso)
        return jsonify({"status": "ok", "username": username, "expires_at": expires_iso})

    @app.route("/whitelist/<username>", methods=["DELETE"])
    def remove_from_whitelist(username):
        try:
            amp.whitelist_remove(username)
        except (AMPAuthError, AMPCommandError) as e:
            logger.error("AMP error removing %s: %s", username, e)
            return jsonify({"status": "error", "message": "AMP unreachable"}), 503
        with db_lock:
            delete_entry(conn, username)
        logger.info("Manually removed %s from whitelist", username)
        return jsonify({"status": "ok", "username": username})

    # ── Twitch OAuth + EventSub ───────────────────────────────────────────────

    @app.route("/twitch/auth", methods=["POST"])
    def twitch_auth():
        data = request.get_json(silent=True) or {}
        client_id = data.get("client_id", "").strip()
        client_secret = data.get("client_secret", "").strip()
        channel_name = data.get("channel_name", "").strip()

        if not client_id or not client_secret or not channel_name:
            return jsonify({
                "status": "error",
                "message": "Client ID, Client Secret and Channel Name are required",
            }), 400

        with open(config_path) as f:
            cfg = yaml.safe_load(f)
        cfg.setdefault("twitch", {})
        cfg["twitch"]["client_id"] = client_id
        cfg["twitch"]["client_secret"] = client_secret
        cfg["twitch"]["channel_name"] = channel_name
        with open(config_path, "w") as f:
            yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True)

        auth_url = build_auth_url(client_id, _twitch_redirect_uri())
        return jsonify({"auth_url": auth_url})

    @app.route("/twitch/callback")
    def twitch_callback():
        error = request.args.get("error")
        if error:
            desc = request.args.get("error_description", error)
            logger.error("Twitch OAuth error: %s", desc)
            return redirect("/?twitch=error")

        code = request.args.get("code")
        if not code:
            return redirect("/?twitch=error")

        with open(config_path) as f:
            cfg = yaml.safe_load(f)
        twitch_cfg = cfg.get("twitch", {})

        try:
            tokens = exchange_code(
                twitch_cfg["client_id"],
                twitch_cfg["client_secret"],
                code,
                _twitch_redirect_uri(),
            )
            broadcaster_id = get_broadcaster_id(
                twitch_cfg["client_id"],
                tokens["access_token"],
                twitch_cfg["channel_name"],
            )
        except Exception as e:
            logger.error("Twitch OAuth callback error: %s", e)
            return redirect("/?twitch=error")

        cfg["twitch"]["access_token"] = tokens["access_token"]
        cfg["twitch"]["refresh_token"] = tokens["refresh_token"]
        cfg["twitch"]["broadcaster_id"] = broadcaster_id
        with open(config_path, "w") as f:
            yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True)

        _start_twitch_client(cfg["twitch"])
        return redirect("/?twitch=connected")

    @app.route("/twitch/status")
    def twitch_status():
        client = twitch_state["client"]
        with open(config_path) as f:
            cfg = yaml.safe_load(f)
        twitch_cfg = cfg.get("twitch", {})
        return jsonify({
            "status": client.status if client else "disconnected",
            "channel_name": twitch_cfg.get("channel_name", ""),
            "reward_id": twitch_cfg.get("reward_id", ""),
        })

    @app.route("/twitch/rewards")
    def twitch_rewards():
        client = twitch_state["client"]
        if not client:
            return jsonify({"status": "error", "message": "Not connected to Twitch"}), 400
        with open(config_path) as f:
            cfg = yaml.safe_load(f)
        twitch_cfg = cfg.get("twitch", {})
        try:
            rewards = get_rewards(
                twitch_cfg["client_id"],
                twitch_cfg["access_token"],
                twitch_cfg["broadcaster_id"],
            )
        except Exception as e:
            logger.error("Failed to fetch rewards: %s", e)
            return jsonify({"status": "error", "message": "Failed to fetch rewards"}), 502
        return jsonify({"rewards": rewards})

    @app.route("/twitch/disconnect", methods=["POST"])
    def twitch_disconnect():
        if twitch_state["client"]:
            twitch_state["client"].stop()
            twitch_state["client"] = None
        with open(config_path) as f:
            cfg = yaml.safe_load(f)
        for key in ("access_token", "refresh_token", "broadcaster_id"):
            cfg.get("twitch", {}).pop(key, None)
        with open(config_path, "w") as f:
            yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True)
        logger.info("Twitch disconnected via dashboard")
        return jsonify({"status": "ok"})

    return app
