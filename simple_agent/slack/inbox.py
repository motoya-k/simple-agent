"""Which Slack events have been handled — beside the transcripts, same database.

Socket Mode wants an acknowledgement within three seconds of handing an event
over, long before a turn can finish.  Acknowledging ends Slack's own retrying,
so if the event lived only in memory, a crash mid-turn would lose somebody's
question silently.

So an event is written down the moment it arrives and cleared once its turn is
over — the same shape as :mod:`simple_agent.mail.ledger`, one table instead of
two:

* ``claim`` writes the raw envelope and says whether this is the first sight of
  it.  Slack re-sends an event after a reconnect, and a duplicate here is a
  duplicate answer in the channel.
* ``pending`` is what a restart replays: claimed, never finished.
* ``mark`` is the turn finishing.  The row stays, without its payload, because
  it is still the proof that this event was handled.

Backend follows the transcript store: SQLite beside the agent, Postgres when
``database_url`` is set.  A host whose transcripts outlive the container but
whose inbox does not would answer every question twice after a deploy.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Protocol

_SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS slack_inbox (
    app TEXT NOT NULL,
    event_id TEXT NOT NULL,
    payload TEXT NOT NULL,
    done INTEGER NOT NULL DEFAULT 0,
    at REAL NOT NULL,
    PRIMARY KEY (app, event_id)
);
CREATE INDEX IF NOT EXISTS slack_inbox_pending ON slack_inbox (app, done);
"""

_POSTGRES_SCHEMA = """
CREATE TABLE IF NOT EXISTS slack_inbox (
    app TEXT NOT NULL,
    event_id TEXT NOT NULL,
    payload TEXT NOT NULL,
    done BOOLEAN NOT NULL DEFAULT FALSE,
    at DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (app, event_id)
);
CREATE INDEX IF NOT EXISTS slack_inbox_pending ON slack_inbox (app, done);
"""

# Its own advisory lock, so this schema and the transcripts' can be created by
# two processes at the same time.
_SCHEMA_LOCK = 0x3A12

#: How long a finished event is remembered.  Long enough to outlast any
#: reconnect Slack will re-deliver across, short enough that the table does not
#: grow forever.
KEEP_SECONDS = 7 * 24 * 3600


class Inbox(Protocol):
    def claim(self, app: str, event_id: str, payload: str) -> bool:
        """Record the event; ``True`` if this is the first sight of it."""
        ...

    def pending(self, app: str) -> list[tuple[str, str]]: ...

    def mark(self, app: str, event_id: str) -> None: ...

    def prune(self, app: str, keep_seconds: float = KEEP_SECONDS) -> None: ...


def open_inbox(config: Any) -> Inbox:
    """The inbox that lives where this config keeps its transcripts."""
    if config.database_url:
        return PostgresInbox(config.database_url)
    return SqliteInbox(config.state_db)


class SqliteInbox:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.executescript(_SQLITE_SCHEMA)
        self._lock = threading.Lock()

    def claim(self, app: str, event_id: str, payload: str) -> bool:
        with self._lock, self._db:
            cursor = self._db.execute(
                "INSERT OR IGNORE INTO slack_inbox (app, event_id, payload, done, at) "
                "VALUES (?, ?, ?, 0, ?)",
                (app, event_id, payload, time.time()),
            )
        return cursor.rowcount == 1

    def pending(self, app: str) -> list[tuple[str, str]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT event_id, payload FROM slack_inbox "
                "WHERE app = ? AND done = 0 ORDER BY at",
                (app,),
            ).fetchall()
        return [(row[0], row[1]) for row in rows]

    def mark(self, app: str, event_id: str) -> None:
        with self._lock, self._db:
            self._db.execute(
                "UPDATE slack_inbox SET done = 1, payload = '' WHERE app = ? AND event_id = ?",
                (app, event_id),
            )

    def prune(self, app: str, keep_seconds: float = KEEP_SECONDS) -> None:
        with self._lock, self._db:
            self._db.execute(
                "DELETE FROM slack_inbox WHERE app = ? AND done = 1 AND at < ?",
                (app, time.time() - keep_seconds),
            )


class PostgresInbox:
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
        # Caller holds the lock. Reconnect after a failover or idle timeout.
        if self._conn.closed:
            self._conn = self._connect()
        return self._conn.execute(sql, params)

    def claim(self, app: str, event_id: str, payload: str) -> bool:
        with self._lock:
            cursor = self._execute(
                "INSERT INTO slack_inbox (app, event_id, payload, done, at) "
                "VALUES (%s, %s, %s, FALSE, %s) ON CONFLICT DO NOTHING",
                (app, event_id, payload, time.time()),
            )
        # Two hosts on one workspace: the insert that did nothing is the one
        # whose caller must not answer.
        return cursor.rowcount == 1

    def pending(self, app: str) -> list[tuple[str, str]]:
        with self._lock:
            rows = self._execute(
                "SELECT event_id, payload FROM slack_inbox "
                "WHERE app = %s AND done = FALSE ORDER BY at",
                (app,),
            ).fetchall()
        return [(row[0], row[1]) for row in rows]

    def mark(self, app: str, event_id: str) -> None:
        with self._lock:
            self._execute(
                "UPDATE slack_inbox SET done = TRUE, payload = '' "
                "WHERE app = %s AND event_id = %s",
                (app, event_id),
            )

    def prune(self, app: str, keep_seconds: float = KEEP_SECONDS) -> None:
        with self._lock:
            self._execute(
                "DELETE FROM slack_inbox WHERE app = %s AND done = TRUE AND at < %s",
                (app, time.time() - keep_seconds),
            )
