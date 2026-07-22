import logging
import re
import threading
import time

from .amp_client import AMPAuthError, AMPClient, AMPCommandError

logger = logging.getLogger(__name__)

GAMEMODE_POLL_INTERVAL_SECONDS = 5

# Vanilla/Spigot/Paper log a line like "[12:34:56] [Server thread/INFO]: Notch joined the game"
# regardless of AMP version — match the trailing "<name> joined the game" rather than the
# full line, since AMP may or may not include the timestamp/thread prefix in ConsoleEntries.
_JOIN_RE = re.compile(r"([a-zA-Z0-9_]{3,16}) joined the game")


def start_gamemode_watcher(
    amp_client: AMPClient,
    pending_gamemodes: dict,
    pending_lock: threading.Lock,
    whitelist_duration_seconds: int,
) -> threading.Thread:
    thread = threading.Thread(
        target=_watch_loop,
        args=(amp_client, pending_gamemodes, pending_lock, whitelist_duration_seconds),
        daemon=True,
        name="whitelister-gamemode-watcher",
    )
    thread.start()
    logger.info("Gamemode watcher thread started (interval=%ds)", GAMEMODE_POLL_INTERVAL_SECONDS)
    return thread


def _watch_loop(
    amp_client: AMPClient,
    pending_gamemodes: dict,
    pending_lock: threading.Lock,
    whitelist_duration_seconds: int,
) -> None:
    while True:
        time.sleep(GAMEMODE_POLL_INTERVAL_SECONDS)
        try:
            with pending_lock:
                if not pending_gamemodes:
                    continue

            try:
                entries = amp_client.get_console_updates()
            except (AMPAuthError, AMPCommandError) as e:
                logger.warning("Gamemode watcher: failed to read console updates: %s", e)
                continue

            for entry in entries:
                text = entry.get("Contents") or entry.get("Message") or str(entry)
                match = _JOIN_RE.search(text)
                if not match:
                    continue
                name = match.group(1)
                key = name.lower()
                with pending_lock:
                    pending = pending_gamemodes.pop(key, None)
                if pending is None:
                    continue
                gamemode, _redeemed_at = pending
                try:
                    amp_client.set_gamemode(name, gamemode)
                    logger.info("Applied gamemode %s to %s on join", gamemode, name)
                except (AMPAuthError, AMPCommandError) as e:
                    logger.warning("Failed to set gamemode %s for %s: %s", gamemode, name, e)

            # Purge stale pending entries — if they never joined before their whitelist
            # itself would expire, there's nothing meaningful left to apply.
            now = int(time.time())
            with pending_lock:
                stale = [
                    key for key, (_, redeemed_at) in pending_gamemodes.items()
                    if now - redeemed_at > whitelist_duration_seconds
                ]
                for key in stale:
                    logger.info("Dropping stale pending gamemode for %s (never joined)", key)
                    pending_gamemodes.pop(key, None)
        except Exception as e:
            logger.error("Gamemode watcher loop error: %s", e)
