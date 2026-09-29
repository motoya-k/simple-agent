"""Session store: every message in SQLite, searchable, and *replayable*.

Two jobs that pull in different directions:

**Search** wants plain text.  A conversation is only findable later if what
went into the index reads like language, not like a payload.

**Replay** wants the whole message.  A conversation can only be resumed if the
tool calls and their results come back too — an assistant turn that asked to
run a command, restored without the command's output, is a transcript the
provider refuses.

So a message is stored twice over: ``content`` keeps the exact structure
(``content_kind`` says whether it is plain text or a list of blocks), and
``search_text`` keeps the readable projection that the full-text indexes see.

Two full-text indexes, kept in sync by triggers so no write path can forget:

* ``messages_fts``          — the default unicode61 tokenizer, word-based.
* ``messages_fts_trigram``  — a trigram index, which is what makes Japanese,
  Chinese and Korean searchable at all: those languages do not put spaces
  between words, so a word tokenizer indexes an entire sentence as one token.

Search hits come back as *windows* — the messages either side of the hit, plus
the session's opening request and closing answer — so a match arrives with
enough context to be recognized rather than as an orphan line.

Two backends implement :class:`Store`.  :class:`SqliteStore` is the default: a
file next to the agent, nothing to run.  :class:`~simple_agent.state_postgres.PostgresStore`
is for a host that outlives its disk or runs as more than one process — set
``SIMPLE_AGENT_DATABASE_URL`` and :func:`open_store` picks it.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Protocol

SCHEMA_VERSION = 2

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id          TEXT PRIMARY KEY,
    started_at  REAL NOT NULL,
    updated_at  REAL,
    cwd         TEXT,
    title       TEXT,
    session_key TEXT,
    parent_id   TEXT
);

CREATE TABLE IF NOT EXISTS messages (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id   TEXT NOT NULL REFERENCES sessions(id),
    role         TEXT NOT NULL,
    content      TEXT NOT NULL,
    content_kind TEXT NOT NULL DEFAULT 'text',
    search_text  TEXT NOT NULL DEFAULT '',
    created_at   REAL NOT NULL,
    active       INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS messages_by_session ON messages(session_id, id);
CREATE INDEX IF NOT EXISTS sessions_by_key ON sessions(session_key, started_at DESC);

CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(content);

CREATE TRIGGER IF NOT EXISTS messages_fts_insert AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.search_text);
END;

CREATE TRIGGER IF NOT EXISTS messages_fts_delete AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content)
        VALUES ('delete', old.id, old.search_text);
END;
"""

_TRIGRAM_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts_trigram
    USING fts5(content, tokenize='trigram');

CREATE TRIGGER IF NOT EXISTS messages_trigram_insert AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts_trigram(rowid, content) VALUES (new.id, new.search_text);
END;

CREATE TRIGGER IF NOT EXISTS messages_trigram_delete AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts_trigram(messages_fts_trigram, rowid, content)
        VALUES ('delete', old.id, old.search_text);
END;
"""


class Store(Protocol):
    """What the agent needs from a transcript store — and nothing else."""

    def new_session(self, cwd: str, session_key: str = "", parent_id: str = "") -> str: ...
    def add_message(self, session_id: str, role: str, content: Any) -> int | None: ...
    def replace_message(self, session_id: str, row_id: int, content: Any) -> int | None: ...
    def deactivate_from(self, session_id: str, first_message_id: int) -> int: ...
    def conversation(self, session_id: str) -> list[dict[str, Any]]: ...
    def message_count(self, session_id: str) -> int: ...
    def latest_session_for_key(self, session_key: str) -> str | None: ...
    def search(self, query: str, limit: int = 5) -> list[dict[str, Any]]: ...
    def recent_sessions(self, limit: int = 10) -> list[dict[str, Any]]: ...


def open_store(config: Any) -> Store:
    """The store a config asks for: Postgres when a URL is set, else SQLite."""
    if config.database_url:
        from .state_postgres import PostgresStore

        return PostgresStore(config.database_url)
    return SqliteStore(config.state_db)


class SqliteStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Several conversations can be in flight in one process, each on its
        # own thread. SQLite tolerates that with a shared connection only if
        # writes are serialized, which is what the lock below is for.
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        # Every statement goes through _lock. `check_same_thread=False` only
        # silences Python's guard; the connection object itself is not safe to
        # drive from two threads at once, and doing so crashes the interpreter
        # rather than raising. Reads need the lock exactly as much as writes.
        #
        # Write-ahead logging is for the *other* kind of sharing: a second
        # connection to the same file — another host, a dashboard, an agent
        # built without a shared store — reads while this one writes instead
        # of failing with "database is locked".
        try:
            self.db.execute("PRAGMA journal_mode = WAL")
        except sqlite3.OperationalError:
            pass  # filesystem without shared-memory support; rollback journal
        self._migrate()
        self.db.executescript(_SCHEMA)
        try:
            self.db.executescript(_TRIGRAM_SCHEMA)
            self.trigram = True
        except sqlite3.OperationalError:
            self.trigram = False  # SQLite older than 3.34: word search only
        self.db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        self.db.commit()

    def _query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self.db.execute(sql, params))

    def _query_one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        with self._lock:
            return self.db.execute(sql, params).fetchone()

    # -- schema ----------------------------------------------------------
    def _migrate(self) -> None:
        """Bring a database written by an older build up to date.

        Only additive steps: new columns get defaults, and ``search_text`` is
        backfilled from ``content`` — correct for old rows, which only ever
        held plain text.
        """
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version >= SCHEMA_VERSION:
            return
        if not self._table_exists("messages"):
            return  # fresh database; _SCHEMA creates everything

        added = self._add_columns(
            "messages",
            {
                "content_kind": "TEXT NOT NULL DEFAULT 'text'",
                "search_text": "TEXT NOT NULL DEFAULT ''",
                "active": "INTEGER NOT NULL DEFAULT 1",
            },
        )
        self._add_columns(
            "sessions",
            {"updated_at": "REAL", "session_key": "TEXT", "parent_id": "TEXT"},
        )
        if "search_text" in added:
            self.db.execute("UPDATE messages SET search_text = content")
        # The triggers still reference the old column; drop them so the
        # definitions in _SCHEMA replace them.
        for trigger in (
            "messages_fts_insert",
            "messages_fts_delete",
            "messages_trigram_insert",
            "messages_trigram_delete",
        ):
            self.db.execute(f"DROP TRIGGER IF EXISTS {trigger}")
        self.db.commit()

    def _table_exists(self, name: str) -> bool:
        row = self.db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        return row is not None

    def _add_columns(self, table: str, columns: dict[str, str]) -> list[str]:
        existing = {r["name"] for r in self.db.execute(f"PRAGMA table_info({table})")}
        added = []
        for name, decl in columns.items():
            if name not in existing:
                self.db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
                added.append(name)
        return added

    # -- writing --------------------------------------------------------
    def new_session(self, cwd: str, session_key: str = "", parent_id: str = "") -> str:
        session_id = uuid.uuid4().hex[:12]
        now = time.time()
        with self._lock:
            self.db.execute(
                "INSERT INTO sessions (id, started_at, updated_at, cwd, session_key, parent_id) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (session_id, now, now, cwd, session_key or None, parent_id or None),
            )
            self.db.commit()
        return session_id

    def add_message(self, session_id: str, role: str, content: Any) -> int | None:
        """Persist one message exactly as the loop holds it.

        ``content`` is either a string or the block list the provider format
        uses. Both are stored losslessly; only the searchable projection is
        flattened.
        """
        kind, stored = _encode(content)
        searchable = searchable_text(content)
        if not stored.strip():
            return None
        now = time.time()
        with self._lock:
            cursor = self.db.execute(
                "INSERT INTO messages "
                "(session_id, role, content, content_kind, search_text, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (session_id, role, stored, kind, searchable, now),
            )
            self.db.execute(
                "UPDATE sessions SET updated_at = ?, title = COALESCE(title, ?) WHERE id = ?",
                (now, searchable[:120] if role == "user" and searchable else None, session_id),
            )
            self.db.commit()
            return cursor.lastrowid

    def replace_message(self, session_id: str, row_id: int, content: Any) -> int | None:
        """Supersede a stored message with a revised version.

        Implemented as hide-then-append rather than an in-place update, for the
        same reason rewinding is: what was actually said stays on disk, and the
        full-text triggers only fire on insert and delete.
        """
        with self._lock:
            self.db.execute("UPDATE messages SET active = 0 WHERE id = ?", (row_id,))
            self.db.commit()
        return self.add_message(session_id, "user", content)

    def deactivate_from(self, session_id: str, first_message_id: int) -> int:
        """Mark messages as no longer part of the live transcript.

        Rewinding hides rather than deletes, for the same reason skills age
        instead of being removed: a judgment call should be reversible.
        """
        with self._lock:
            cursor = self.db.execute(
                "UPDATE messages SET active = 0 WHERE session_id = ? AND id >= ?",
                (session_id, first_message_id),
            )
            self.db.commit()
            return cursor.rowcount

    # -- reading --------------------------------------------------------
    def conversation(self, session_id: str) -> list[dict[str, Any]]:
        """Rebuild the live transcript, ready to hand straight to the loop."""
        rows = self._query(
            "SELECT role, content, content_kind FROM messages "
            "WHERE session_id = ? AND active = 1 ORDER BY id",
            (session_id,),
        )
        return [
            {"role": row["role"], "content": _decode(row["content"], row["content_kind"])}
            for row in rows
        ]

    def message_count(self, session_id: str) -> int:
        row = self._query_one(
            "SELECT COUNT(*) AS n FROM messages WHERE session_id = ? AND active = 1",
            (session_id,),
        )
        return int(row["n"]) if row else 0

    def latest_session_for_key(self, session_key: str) -> str | None:
        """The conversation a chat thread should pick back up.

        Without this a restarted host starts every thread over, which reads to
        the people in it as the agent having forgotten the last hour.
        """
        if not session_key:
            return None
        row = self._query_one(
            "SELECT id FROM sessions WHERE session_key = ? ORDER BY started_at DESC LIMIT 1",
            (session_key,),
        )
        return row["id"] if row else None

    def search(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        rows = self._match("messages_fts", query, limit)
        if len(rows) < limit and self.trigram:
            seen = {r["id"] for r in rows}
            rows += [
                r for r in self._match("messages_fts_trigram", query, limit)
                if r["id"] not in seen
            ]
        return [self._window(row) for row in rows[:limit]]

    def _match(self, table: str, query: str, limit: int) -> list[sqlite3.Row]:
        sql = f"""
            SELECT m.id, m.session_id, m.role, m.search_text, m.created_at
            FROM {table} f JOIN messages m ON m.id = f.rowid
            WHERE {table} MATCH ? AND m.active = 1
            ORDER BY rank LIMIT ?
        """
        try:
            return self._query(sql, (query, limit))
        except sqlite3.OperationalError:
            # A query FTS5 cannot parse (bare punctuation, unbalanced quotes).
            escaped = '"' + query.replace('"', '""') + '"'
            try:
                return self._query(sql, (escaped, limit))
            except sqlite3.OperationalError:
                return []

    def _window(self, row: sqlite3.Row, radius: int = 1) -> dict[str, Any]:
        session_id = row["session_id"]
        around = self._query(
            "SELECT role, search_text FROM messages "
            "WHERE session_id = ? AND id BETWEEN ? AND ? AND active = 1 ORDER BY id",
            (session_id, row["id"] - radius, row["id"] + radius),
        )
        opening = self._query_one(
            "SELECT search_text FROM messages WHERE session_id = ? AND role = 'user' "
            "AND active = 1 ORDER BY id LIMIT 1",
            (session_id,),
        )
        closing = self._query_one(
            "SELECT search_text FROM messages WHERE session_id = ? AND role = 'assistant' "
            "AND active = 1 ORDER BY id DESC LIMIT 1",
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
        rows = self._query(
            "SELECT id, started_at, title, session_key FROM sessions "
            "ORDER BY started_at DESC LIMIT ?",
            (limit,),
        )
        return [dict(row) for row in rows]


# -- message encoding -----------------------------------------------------
def _encode(content: Any) -> tuple[str, str]:
    if isinstance(content, str):
        return "text", content
    return "blocks", json.dumps(content, ensure_ascii=False)


def _decode(stored: str, kind: str) -> Any:
    if kind != "blocks":
        return stored
    try:
        return json.loads(stored)
    except (json.JSONDecodeError, TypeError):
        # A row we cannot parse is worse than a row we drop: it would be
        # replayed into the provider as a malformed turn. Degrade to text.
        return stored


def searchable_text(content: Any) -> str:
    """The readable projection of a message — what search sees, and what a
    search result shows the reader.

    Tool results are included because "when did I last see this error" is a
    real question. Their arguments are not: a JSON blob of parameters buries
    the surrounding sentences in the index.
    """
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content)

    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text":
            parts.append(str(block.get("text", "")))
        elif kind == "tool_use":
            parts.append(f"[tool: {block.get('name', '?')}]")
        elif kind == "tool_result":
            body = block.get("content")
            parts.append(body if isinstance(body, str) else "[tool result]")
    return "\n".join(p for p in parts if p).strip()


def _clip(text: str, limit: int = 400) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit] + "…"
