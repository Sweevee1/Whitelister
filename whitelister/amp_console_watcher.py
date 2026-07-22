import logging
import re
import threading
import time

from .amp_client import AMPAuthError, AMPClient, AMPCommandError

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 5
WHITELIST_REFRESH_INTERVAL_SECONDS = 60

# Vanilla/Spigot/Paper log a line like "[12:34:56] [Server thread/INFO]: Notch joined the game"
# regardless of AMP version — match the trailing "<name> joined the game" rather than the
# full line. Confirmed live against a real AMP instance that ConsoleEntries' `Contents` field
# is already the clean message text with no timestamp/thread prefix baked in, but keep the
# defensive .search() (not .match()) anyway in case other AMP versions differ.
_JOIN_RE = re.compile(r"([a-zA-Z0-9_]{3,16}) joined the game")

# Confirmed live: "whitelist list" responds with e.g.
# "There are 5 whitelisted player(s): Name1, Name2, Name3". The zero-player phrasing
# ("There are no whitelisted players") wasn't observed live — handled defensively below.
_WHITELIST_LIST_RE = re.compile(r"There are \d+ whitelisted player\(s\): (.+)")
_WHITELIST_EMPTY_RE = re.compile(r"There are no whitelisted players", re.IGNORECASE)


def start_console_watcher(
    amp_client: AMPClient,
    pending_gamemodes: dict,
    pending_lock: threading.Lock,
    whitelist_duration_seconds: int,
    whitelist_cache: list,
    whitelist_cache_lock: threading.Lock,
) -> threading.Thread:
    thread = threading.Thread(
        target=_watch_loop,
        args=(amp_client, pending_gamemodes, pending_lock, whitelist_duration_seconds,
              whitelist_cache, whitelist_cache_lock),
        daemon=True,
        name="whitelister-amp-console-watcher",
    )
    thread.start()
    logger.info("AMP console watcher thread started (interval=%ds)", POLL_INTERVAL_SECONDS)
    return thread


def _watch_loop(
    amp_client: AMPClient,
    pending_gamemodes: dict,
    pending_lock: threading.Lock,
    whitelist_duration_seconds: int,
    whitelist_cache: list,
    whitelist_cache_lock: threading.Lock,
) -> None:
    # All AMP console polling for the whole app happens in this single thread — Core/GetUpdates
    # is a since-last-poll cursor tied to the AMP session, not per-caller, so a second concurrent
    # poller (e.g. a Flask request handler) would race this thread for the same console entries.
    last_whitelist_refresh = 0.0
    while True:
        time.sleep(POLL_INTERVAL_SECONDS)
        try:
            due_for_refresh = (
                time.monotonic() - last_whitelist_refresh >= WHITELIST_REFRESH_INTERVAL_SECONDS
            )
            if due_for_refresh:
                try:
                    amp_client.send_console_command("whitelist list")
                    time.sleep(1.5)
                except (AMPAuthError, AMPCommandError) as e:
                    logger.warning("Console watcher: failed to request whitelist list: %s", e)
                last_whitelist_refresh = time.monotonic()

            try:
                entries = amp_client.get_console_updates()
            except (AMPAuthError, AMPCommandError) as e:
                logger.warning("Console watcher: failed to read console updates: %s", e)
                entries = []

            for entry in entries:
                text = entry.get("Contents") or entry.get("Message") or str(entry)

                match = _JOIN_RE.search(text)
                if match:
                    name = match.group(1)
                    key = name.lower()
                    with pending_lock:
                        pending = pending_gamemodes.pop(key, None)
                    if pending is not None:
                        gamemode, _redeemed_at = pending
                        try:
                            amp_client.set_gamemode(name, gamemode)
                            logger.info("Applied gamemode %s to %s on join", gamemode, name)
                        except (AMPAuthError, AMPCommandError) as e:
                            logger.warning("Failed to set gamemode %s for %s: %s", gamemode, name, e)
                    continue

                list_match = _WHITELIST_LIST_RE.search(text)
                if list_match:
                    names = [n.strip() for n in list_match.group(1).split(",") if n.strip()]
                    with whitelist_cache_lock:
                        whitelist_cache[:] = names
                    logger.debug("Whitelist cache refreshed: %s", names)
                elif _WHITELIST_EMPTY_RE.search(text):
                    with whitelist_cache_lock:
                        whitelist_cache[:] = []
                    logger.debug("Whitelist cache refreshed: empty")

            # Purge stale pending gamemodes — if they never joined before their whitelist
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
            logger.error("Console watcher loop error: %s", e)
