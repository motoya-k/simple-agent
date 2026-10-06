"""Which scheduled runs have already happened.

One row per job per firing, which buys two things:

* **A firing happens once**, even when two hosts share a database — both compute
  the same 09:00, and only the one whose insert won sends it.  Without this,
  scaling the container to two tasks doubles every scheduled run.
* **A missed firing can be made up.**  A job with ``catch_up`` asks on startup
  whether its most recent slot ran; a deploy that spanned 09:00 is why it might
  not have.

Memory is the default, because the useful default for one host on a laptop is
no new file.  :func:`open_cursor` is what a deployment uses: a run recorded
only in memory is a run repeated after every restart.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Protocol

_SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS cron_run (
    job TEXT NOT NULL,
    at REAL NOT NULL,
    PRIMARY KEY (job, at)
);
"""

_POSTGRES_SCHEMA = """
CREATE TABLE IF NOT EXISTS cron_run (
    job TEXT NOT NULL,
    at DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (job, at)
);
"""

_SCHEMA_LOCK = 0x3A13

#: Long enough that a restart cannot re-run anything recent, short enough that
#: the table stays small.
KEEP_SECONDS = 30 * 24 * 3600


class Cursor(Protocol):
    def claim(self, job: str, at: float) -> bool:
        """Record that this firing is being run; ``False`` if it already was."""
        ...

    def prune(self, keep_seconds: float = KEEP_SECONDS) -> None: ...


class MemoryCursor:
    """Remembers firings for as long as the process lives, and no longer."""

    def __init__(self) -> None:
        self._seen: set[tuple[str, float]] = set()
        self._lock = threading.Lock()

    def claim(self, job: str, at: float) -> bool:
        with self._lock:
            if (job, at) in self._seen:
                return False
            self._seen.add((job, at))
            return True

    def prune(self, keep_seconds: float = KEEP_SECONDS) -> None:
        cutoff = time.time() - keep_seconds
        with self._lock:
            self._seen = {entry for entry in self._seen if entry[1] >= cutoff}


def open_cursor(config: Any) -> Cursor:
    """The cursor that lives where this config keeps its transcripts."""
    if config.database_url:
        return PostgresCursor(config.database_url)
    return SqliteCursor(config.state_db)


class SqliteCursor:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.executescript(_SQLITE_SCHEMA)
        self._lock = threading.Lock()

    def claim(self, job: str, at: float) -> bool:
        with self._lock, self._db:
            cursor = self._db.execute(
                "INSERT OR IGNORE INTO cron_run (job, at) VALUES (?, ?)", (job, at)
            )
        return cursor.rowcount == 1

    def prune(self, keep_seconds: float = KEEP_SECONDS) -> None:
        with self._lock, self._db:
            self._db.execute("DELETE FROM cron_run WHERE at < ?", (time.time() - keep_seconds,))


class PostgresCursor:
    def __init__(self, url: str) -> None:
        import psycopg  # optional dependency: pip install 'simple-agent[postgres]'

        self._url = url
        self._psycopg = psycopg
        self._lock = threading.Lock()
        self._conn = self._connect()
        with self._lock, self._conn.transaction():
            self._conn.execute("SELECT pg_advisory_xact_lock(%s)", (_SCHEMA_LOCK,))
            self._conn.execute(_POSTGRES_SCHEMA)

    def _connect(self):
        return self._psycopg.connect(self._url, autocommit=True)

    def _execute(self, sql: str, params: tuple):
        if self._conn.closed:
            self._conn = self._connect()
        return self._conn.execute(sql, params)

    def claim(self, job: str, at: float) -> bool:
        with self._lock:
            cursor = self._execute(
                "INSERT INTO cron_run (job, at) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                (job, at),
            )
        # The host whose insert did nothing is the host that must not run it.
        return cursor.rowcount == 1

    def prune(self, keep_seconds: float = KEEP_SECONDS) -> None:
        with self._lock:
            self._execute("DELETE FROM cron_run WHERE at < %s", (time.time() - keep_seconds,))
