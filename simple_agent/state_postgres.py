"""Session store on Postgres — the same contract as :class:`~simple_agent.state.SqliteStore`.

For a host whose disk does not outlive it (a container, a serverless task) or
that runs as more than one process.  SQLite's write-ahead log assumes every
connection is on the same machine; on a network filesystem that assumption
fails silently.  Postgres makes it someone else's problem.

What maps across, and what changes:

* **Word search** is a ``tsvector`` column with the ``simple`` configuration:
  lowercase and split on whitespace, no stemming — the same behaviour as
  SQLite's unicode61 tokenizer.
* **Japanese search** is ``pg_trgm``.  Like the SQLite trigram index it is what
  makes languages without spaces findable at all; unlike it, a query shorter
  than three characters still works (it scans instead of using the index).
* **No triggers.**  The word index is a generated column and the trigram index
  sits on ``search_text`` directly, so there is nothing to keep in sync.

Needs ``psycopg`` (``pip install 'simple-agent[postgres]'``).  The core stays
dependency-free; only this module imports it.
"""

from __future__ import annotations

import threading
import time
import uuid
from typing import Any

import psycopg
from psycopg.rows import dict_row

from .state import _clip, _decode, _encode, searchable_text

# Any constant works; it only has to be the same in every process that runs
# the schema, so two hosts starting at once do not race on CREATE.
_SCHEMA_LOCK = 0x51A7E

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id          TEXT PRIMARY KEY,
    started_at  DOUBLE PRECISION NOT NULL,
    updated_at  DOUBLE PRECISION,
    cwd         TEXT,
    title       TEXT,
    session_key TEXT,
    parent_id   TEXT
);

CREATE TABLE IF NOT EXISTS messages (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    session_id   TEXT NOT NULL REFERENCES sessions(id),
    role         TEXT NOT NULL,
    content      TEXT NOT NULL,
    content_kind TEXT NOT NULL DEFAULT 'text',
    search_text  TEXT NOT NULL DEFAULT '',
    created_at   DOUBLE PRECISION NOT NULL,
    active       BOOLEAN NOT NULL DEFAULT TRUE,
    search_tsv   TSVECTOR GENERATED ALWAYS AS (to_tsvector('simple', search_text)) STORED
);

CREATE INDEX IF NOT EXISTS messages_by_session ON messages(session_id, id);
CREATE INDEX IF NOT EXISTS sessions_by_key ON sessions(session_key, started_at DESC);
CREATE INDEX IF NOT EXISTS messages_fts ON messages USING GIN (search_tsv);
"""

_TRIGRAM_SCHEMA = """
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE INDEX IF NOT EXISTS messages_fts_trigram
    ON messages USING GIN (search_text gin_trgm_ops);
"""


class PostgresStore:
    def __init__(self, url: str) -> None:
        self.url = url
        # One connection behind a lock, like the SQLite store: every call here
        # is a single short statement, so a pool would buy little. The lock is
        # what makes the connection safe to share across agent threads.
        self._lock = threading.RLock()
        self._conn = self._connect()
        with self._lock, self._conn.transaction():
            self._conn.execute("SELECT pg_advisory_xact_lock(%s)", (_SCHEMA_LOCK,))
            self._conn.execute(_SCHEMA)
        try:
            with self._lock, self._conn.transaction():
                self._conn.execute("SELECT pg_advisory_xact_lock(%s)", (_SCHEMA_LOCK,))
                self._conn.execute(_TRIGRAM_SCHEMA)
            self.trigram = True
        except psycopg.Error:
            # CREATE EXTENSION needs a privileged role. Without it the store
            # still works; only languages without spaces lose search.
            self.trigram = False

    def _connect(self) -> psycopg.Connection:
        return psycopg.connect(self.url, autocommit=True, row_factory=dict_row)

    def _ensure(self) -> None:
        # A failover or an idle timeout drops the connection; the next call
        # reconnects instead of failing every call after it.
        if self._conn.closed:
            self._conn = self._connect()

    def _execute(self, sql: str, params: tuple = ()) -> psycopg.Cursor:
        with self._lock:
            self._ensure()
            return self._conn.execute(sql, params)

    def _query(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        with self._lock:
            return self._execute(sql, params).fetchall()

    def _query_one(self, sql: str, params: tuple = ()) -> dict[str, Any] | None:
        with self._lock:
            return self._execute(sql, params).fetchone()

    # -- writing --------------------------------------------------------
    def new_session(self, cwd: str, session_key: str = "", parent_id: str = "") -> str:
        session_id = uuid.uuid4().hex[:12]
        now = time.time()
        self._execute(
            "INSERT INTO sessions (id, started_at, updated_at, cwd, session_key, parent_id) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (session_id, now, now, cwd, session_key or None, parent_id or None),
        )
        return session_id

    def add_message(self, session_id: str, role: str, content: Any) -> int | None:
        kind, stored = _encode(content)
        searchable = searchable_text(content)
        if not stored.strip():
            return None
        now = time.time()
        with self._lock:
            self._ensure()
            with self._conn.transaction():
                row = self._execute(
                    "INSERT INTO messages "
                    "(session_id, role, content, content_kind, search_text, created_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
                    (session_id, role, stored, kind, searchable, now),
                ).fetchone()
                self._execute(
                    "UPDATE sessions SET updated_at = %s, title = COALESCE(title, %s) "
                    "WHERE id = %s",
                    (now, searchable[:120] if role == "user" and searchable else None, session_id),
                )
        return row["id"]

    def replace_message(self, session_id: str, row_id: int, content: Any) -> int | None:
        self._execute("UPDATE messages SET active = FALSE WHERE id = %s", (row_id,))
        return self.add_message(session_id, "user", content)

    def deactivate_from(self, session_id: str, first_message_id: int) -> int:
        cursor = self._execute(
            "UPDATE messages SET active = FALSE WHERE session_id = %s AND id >= %s",
            (session_id, first_message_id),
        )
        return cursor.rowcount

    # -- reading --------------------------------------------------------
    def conversation(self, session_id: str) -> list[dict[str, Any]]:
        rows = self._query(
            "SELECT role, content, content_kind FROM messages "
            "WHERE session_id = %s AND active ORDER BY id",
            (session_id,),
        )
        return [
            {"role": row["role"], "content": _decode(row["content"], row["content_kind"])}
            for row in rows
        ]

    def message_count(self, session_id: str) -> int:
        row = self._query_one(
            "SELECT COUNT(*) AS n FROM messages WHERE session_id = %s AND active",
            (session_id,),
        )
        return int(row["n"]) if row else 0

    def latest_session_for_key(self, session_key: str) -> str | None:
        if not session_key:
            return None
        row = self._query_one(
            "SELECT id FROM sessions WHERE session_key = %s ORDER BY started_at DESC LIMIT 1",
            (session_key,),
        )
        return row["id"] if row else None

    def search(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        # websearch_to_tsquery accepts any input, so unlike FTS5 there is no
        # "query could not be parsed" fallback to write.
        rows = self._query(
            "SELECT id, session_id, created_at FROM messages, "
            "websearch_to_tsquery('simple', %s) q "
            "WHERE search_tsv @@ q AND active ORDER BY ts_rank(search_tsv, q) DESC LIMIT %s",
            (query, limit),
        )
        if len(rows) < limit and self.trigram:
            seen = {r["id"] for r in rows}
            rows += [
                r
                for r in self._query(
                    "SELECT id, session_id, created_at FROM messages "
                    "WHERE search_text ILIKE %s AND active ORDER BY id DESC LIMIT %s",
                    ("%" + _escape_like(query) + "%", limit),
                )
                if r["id"] not in seen
            ]
        return [self._window(row) for row in rows[:limit]]

    def _window(self, row: dict[str, Any], radius: int = 1) -> dict[str, Any]:
        session_id = row["session_id"]
        around = self._query(
            "SELECT role, search_text FROM messages "
            "WHERE session_id = %s AND id BETWEEN %s AND %s AND active ORDER BY id",
            (session_id, row["id"] - radius, row["id"] + radius),
        )
        opening = self._query_one(
            "SELECT search_text FROM messages WHERE session_id = %s AND role = 'user' "
            "AND active ORDER BY id LIMIT 1",
            (session_id,),
        )
        closing = self._query_one(
            "SELECT search_text FROM messages WHERE session_id = %s AND role = 'assistant' "
            "AND active ORDER BY id DESC LIMIT 1",
            (session_id,),
        )
        return {
            "session_id": session_id,
            "when": time.strftime("%Y-%m-%d %H:%M", time.localtime(row["created_at"])),
            "opened_with": _clip(opening["search_text"] if opening else ""),
            "window": [{"role": r["role"], "text": _clip(r["search_text"])} for r in around],
            "ended_with": _clip(closing["search_text"] if closing else ""),
        }

    def recent_sessions(self, limit: int = 10) -> list[dict[str, Any]]:
        return self._query(
            "SELECT id, started_at, title, session_key FROM sessions "
            "ORDER BY started_at DESC LIMIT %s",
            (limit,),
        )


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
