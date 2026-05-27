import logging
import threading
import time

from .amp_client import AMPClient, AMPCommandError
from .database import delete_entry, get_expired_entries

logger = logging.getLogger(__name__)


def start_expiry_thread(
    conn,
    amp_client: AMPClient,
    db_lock: threading.Lock,
    interval_seconds: int,
) -> threading.Thread:
    thread = threading.Thread(
        target=_expiry_loop,
        args=(conn, amp_client, db_lock, interval_seconds),
        daemon=True,
        name="whitelister-expiry",
    )
    thread.start()
    logger.info("Expiry thread started (interval=%ds)", interval_seconds)
    return thread


def _expiry_loop(
    conn,
    amp_client: AMPClient,
    db_lock: threading.Lock,
    interval_seconds: int,
) -> None:
    while True:
        time.sleep(interval_seconds)
        try:
            now = int(time.time())
            with db_lock:
                expired = get_expired_entries(conn, now)

            if expired:
                logger.info("Processing %d expired whitelist entries", len(expired))

            for username in expired:
                try:
                    amp_client.whitelist_remove(username)
                    with db_lock:
                        delete_entry(conn, username)
                    logger.info("Removed %s from whitelist (expired)", username)
                except AMPCommandError as e:
                    logger.error(
                        "Failed to remove %s: %s — will retry next cycle", username, e
                    )
                except Exception as e:
                    logger.error("Unexpected error removing %s: %s", username, e)
        except Exception as e:
            logger.error("Expiry loop error: %s", e)
