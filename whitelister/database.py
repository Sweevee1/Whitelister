import sqlite3


def init_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS whitelist_entries (
            username   TEXT PRIMARY KEY COLLATE NOCASE,
            added_at   INTEGER NOT NULL,
            expires_at INTEGER NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_expires_at ON whitelist_entries (expires_at)"
    )
    conn.commit()
    return conn


def upsert_entry(
    conn: sqlite3.Connection, username: str, added_at: int, expires_at: int
) -> None:
    conn.execute(
        """
        INSERT INTO whitelist_entries (username, added_at, expires_at)
        VALUES (?, ?, ?)
        ON CONFLICT(username) DO UPDATE SET
            added_at   = excluded.added_at,
            expires_at = excluded.expires_at
        """,
        (username, added_at, expires_at),
    )
    conn.commit()


def get_expired_entries(conn: sqlite3.Connection, now: int) -> list:
    cursor = conn.execute(
        "SELECT username FROM whitelist_entries WHERE expires_at <= ?", (now,)
    )
    return [row[0] for row in cursor.fetchall()]


def delete_entry(conn: sqlite3.Connection, username: str) -> None:
    conn.execute(
        "DELETE FROM whitelist_entries WHERE username = ? COLLATE NOCASE", (username,)
    )
    conn.commit()


def get_all_entries(conn: sqlite3.Connection) -> list:
    cursor = conn.execute("SELECT username, added_at, expires_at FROM whitelist_entries")
    return cursor.fetchall()
