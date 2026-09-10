"""Session store: every message in SQLite, searchable with FTS5.

Two full-text indexes, kept in sync by triggers so no write path can forget to
update them:

* ``messages_fts``          — the default unicode61 tokenizer, word-based.
* ``messages_fts_trigram``  — a trigram index, which is what makes Japanese,
  Chinese and Korean searchable at all: those languages do not put spaces
  between words, so a word tokenizer indexes an entire sentence as one token.

Search hits are returned as *windows* — the messages either side of the hit,
plus the session's opening request and closing answer — so a match comes back
with enough context to be recognized rather than as an orphan line.
"""

from __future__ import annotations

import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id         TEXT PRIMARY KEY,
    started_at REAL NOT NULL,
    cwd        TEXT,
    title      TEXT
);

CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    role       TEXT NOT NULL,
    content    TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS messages_by_session ON messages(session_id, id);

CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(content);

CREATE TRIGGER IF NOT EXISTS messages_fts_insert AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
END;

CREATE TRIGGER IF NOT EXISTS messages_fts_delete AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content)
        VALUES ('delete', old.id, old.content);
END;
"""

_TRIGRAM_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts_trigram
    USING fts5(content, tokenize='trigram');

CREATE TRIGGER IF NOT EXISTS messages_trigram_insert AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts_trigram(rowid, content) VALUES (new.id, new.content);
END;

CREATE TRIGGER IF NOT EXISTS messages_trigram_delete AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts_trigram(messages_fts_trigram, rowid, content)
        VALUES ('delete', old.id, old.content);
END;
"""


class Store:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        # The background reviewer writes from another thread.
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(_SCHEMA)
        try:
            self.db.executescript(_TRIGRAM_SCHEMA)
            self.trigram = True
        except sqlite3.OperationalError:
            self.trigram = False  # SQLite older than 3.34: word search only
        self.db.commit()

    # -- writing --------------------------------------------------------
    def new_session(self, cwd: str) -> str:
        session_id = uuid.uuid4().hex[:12]
        self.db.execute(
            "INSERT INTO sessions (id, started_at, cwd) VALUES (?, ?, ?)",
            (session_id, time.time(), cwd),
        )
        self.db.commit()
        return session_id

    def add_message(self, session_id: str, role: str, content: str) -> None:
        if not content.strip():
            return
        self.db.execute(
            "INSERT INTO messages (session_id, role, content, created_at) VALUES (?, ?, ?, ?)",
            (session_id, role, content, time.time()),
        )
        self.db.execute(
            "UPDATE sessions SET title = COALESCE(title, ?) WHERE id = ?",
            (content[:120] if role == "user" else None, session_id),
        )
        self.db.commit()

    # -- reading --------------------------------------------------------
    def search(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        rows = self._match("messages_fts", query, limit)
        if len(rows) < limit and self.trigram:
            seen = {r["id"] for r in rows}
            rows += [r for r in self._match("messages_fts_trigram", query, limit) if r["id"] not in seen]
        return [self._window(row) for row in rows[:limit]]

    def _match(self, table: str, query: str, limit: int) -> list[sqlite3.Row]:
        sql = f"""
            SELECT m.id, m.session_id, m.role, m.content, m.created_at
            FROM {table} f JOIN messages m ON m.id = f.rowid
            WHERE {table} MATCH ?
            ORDER BY rank LIMIT ?
        """
        try:
            return list(self.db.execute(sql, (query, limit)))
        except sqlite3.OperationalError:
            # A query FTS5 cannot parse (bare punctuation, unbalanced quotes).
            escaped = '"' + query.replace('"', '""') + '"'
            try:
                return list(self.db.execute(sql, (escaped, limit)))
            except sqlite3.OperationalError:
                return []

    def _window(self, row: sqlite3.Row, radius: int = 1) -> dict[str, Any]:
        session_id = row["session_id"]
        around = list(
            self.db.execute(
                "SELECT role, content FROM messages "
                "WHERE session_id = ? AND id BETWEEN ? AND ? ORDER BY id",
                (session_id, row["id"] - radius, row["id"] + radius),
            )
        )
        opening = self.db.execute(
            "SELECT content FROM messages WHERE session_id = ? AND role = 'user' ORDER BY id LIMIT 1",
            (session_id,),
        ).fetchone()
        closing = self.db.execute(
            "SELECT content FROM messages WHERE session_id = ? AND role = 'assistant' ORDER BY id DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        return {
            "session_id": session_id,
            "when": time.strftime("%Y-%m-%d %H:%M", time.localtime(row["created_at"])),
            "opened_with": _clip(opening["content"] if opening else ""),
            "window": [{"role": r["role"], "text": _clip(r["content"])} for r in around],
            "ended_with": _clip(closing["content"] if closing else ""),
        }

    def recent_sessions(self, limit: int = 10) -> list[sqlite3.Row]:
        return list(
            self.db.execute(
                "SELECT id, started_at, title FROM sessions ORDER BY started_at DESC LIMIT ?",
                (limit,),
            )
        )


def _clip(text: str, limit: int = 400) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + "…"
