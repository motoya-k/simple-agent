"""Which mail has been handled — kept beside the transcripts, in the same database.

Two facts per folder, both keyed by ``UIDVALIDITY`` so a server that renumbers
a folder cannot make old UIDs look new or new ones look done:

* the **baseline** — the newest UID when the folder was first seen.  Mail at
  or below it is backlog and is never answered.
* the **seen** set — UIDs above the baseline that a turn has finished with.

The backend follows the transcript store: SQLite next to the agent by default,
Postgres when ``database_url`` is set.  A host that keeps its transcripts
somewhere durable and its ledger on a disk that disappears would answer every
message again after a restart.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any, Protocol

_SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS mail_cursor (
    mailbox TEXT NOT NULL,
    uidvalidity INTEGER NOT NULL,
    baseline INTEGER NOT NULL,
    PRIMARY KEY (mailbox, uidvalidity)
);
CREATE TABLE IF NOT EXISTS mail_seen (
    mailbox TEXT NOT NULL,
    uidvalidity INTEGER NOT NULL,
    uid INTEGER NOT NULL,
    PRIMARY KEY (mailbox, uidvalidity, uid)
);
"""

# UIDVALIDITY and UID are unsigned 32-bit on the wire, which overflows
# Postgres's signed INTEGER; BIGINT holds them.
_POSTGRES_SCHEMA = """
CREATE TABLE IF NOT EXISTS mail_cursor (
    mailbox TEXT NOT NULL,
    uidvalidity BIGINT NOT NULL,
    baseline BIGINT NOT NULL,
    PRIMARY KEY (mailbox, uidvalidity)
);
CREATE TABLE IF NOT EXISTS mail_seen (
    mailbox TEXT NOT NULL,
    uidvalidity BIGINT NOT NULL,
    uid BIGINT NOT NULL,
    PRIMARY KEY (mailbox, uidvalidity, uid)
);
"""

# Distinct from the transcript store's lock, so the two schemas can be created
# by different processes at once.
_SCHEMA_LOCK = 0x3A11


class Ledger(Protocol):
    def baseline(self, mailbox: str, uidvalidity: int, newest: int) -> int:
        """The UID after which mail is new; recorded on first sight."""
        ...

    def seen(self, mailbox: str, uidvalidity: int, uid: int) -> bool: ...

    def mark(self, mailbox: str, uidvalidity: int, uid: int) -> None: ...


def open_ledger(config: Any) -> Ledger:
    """The ledger that lives where this config keeps its transcripts."""
    if config.database_url:
        return PostgresLedger(config.database_url)
    return SqliteLedger(config.state_db)


class SqliteLedger:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.executescript(_SQLITE_SCHEMA)
        self._lock = threading.Lock()

    def baseline(self, mailbox: str, uidvalidity: int, newest: int) -> int:
        with self._lock, self._db:
            self._db.execute(
                "INSERT OR IGNORE INTO mail_cursor VALUES (?, ?, ?)",
                (mailbox, uidvalidity, newest),
            )
            row = self._db.execute(
                "SELECT baseline FROM mail_cursor WHERE mailbox = ? AND uidvalidity = ?",
                (mailbox, uidvalidity),
            ).fetchone()
        return row[0]

    def seen(self, mailbox: str, uidvalidity: int, uid: int) -> bool:
        with self._lock:
            row = self._db.execute(
                "SELECT 1 FROM mail_seen WHERE mailbox = ? AND uidvalidity = ? AND uid = ?",
                (mailbox, uidvalidity, uid),
            ).fetchone()
        return row is not None

    def mark(self, mailbox: str, uidvalidity: int, uid: int) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT OR IGNORE INTO mail_seen VALUES (?, ?, ?)", (mailbox, uidvalidity, uid)
            )


class PostgresLedger:
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

    def baseline(self, mailbox: str, uidvalidity: int, newest: int) -> int:
        # Two statements, not one upsert: when two hosts see a folder at the
        # same moment, the first insert wins and both must read *its* baseline.
        with self._lock:
            self._execute(
                "INSERT INTO mail_cursor VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                (mailbox, uidvalidity, newest),
            )
            row = self._execute(
                "SELECT baseline FROM mail_cursor WHERE mailbox = %s AND uidvalidity = %s",
                (mailbox, uidvalidity),
            ).fetchone()
        return row[0]

    def seen(self, mailbox: str, uidvalidity: int, uid: int) -> bool:
        with self._lock:
            row = self._execute(
                "SELECT 1 FROM mail_seen WHERE mailbox = %s AND uidvalidity = %s AND uid = %s",
                (mailbox, uidvalidity, uid),
            ).fetchone()
        return row is not None

    def mark(self, mailbox: str, uidvalidity: int, uid: int) -> None:
        with self._lock:
            self._execute(
                "INSERT INTO mail_seen VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                (mailbox, uidvalidity, uid),
            )
