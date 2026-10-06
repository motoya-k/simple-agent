"""Memory, in two layers that must not be confused — and skills, which are neither.

**Short-term memory is this conversation.**  The transcript in
``Agent.messages``, persisted by the :class:`~simple_agent.state.Store` and
trimmed by the :class:`~simple_agent.compaction.Compactor`.  Its scope is one
session key, it is written automatically as the conversation happens, and it
holds what is true *right now*: the task, the files just read, the plan, the
half-finished result.  Nothing in this module manages it.

**Long-term memory is what everyone on this team already knows.**  The
conventions, who owns what, what the systems are called, which decisions were
made and why, how these people want work done.  Its scope is a *namespace* —
a team or an organization — shared by every conversation and every user in
it.  It is written deliberately (``memory_save``, usually by the background
reviewer) and read by relevance, not wholesale.

**Skills are neither.**  A skill is a procedure for a class of task that would
still be correct at another company; see :mod:`simple_agent.skills`.  The team
facts a procedure needs come from long-term memory at run time, so the skill
itself stays abstract and the two improve independently.

The sorting test, applied by the reviewer and worth applying by hand:

* true only for this conversation            → short-term (do nothing)
* true for this team, not for any team       → long-term memory
* true for any team                          → a skill

**Recall goes into the user's turn, never the system prompt.**  Which memories
matter depends on what was just asked, so the recalled block changes every
turn; in the system prompt it would invalidate the provider's prompt cache on
every request.  Attached to the new message, it is part of the conversation
from then on — long-term knowledge becomes short-term exactly once, at the
moment it is needed, and is not injected again.

Where it is kept follows one setting, ``database_url``, the same one the
transcripts follow: empty means :class:`LocalMemory`, a JSONL file in
``~/.simple-agent`` you can open in an editor; a ``postgresql://`` URL means
:class:`PostgresMemory`.  Nothing else to choose, and recall behaves the same
either way.

A hosted memory service (mem0, Hindsight, a vector store of your own) is not a
backend here.  It is an MCP server in ``~/.simple-agent/mcp.json``, so it is
declared once and reaches every harness — see :mod:`simple_agent.mcp_client`.
"""

from __future__ import annotations

import json
import re
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

#: The recalled block is background, not a request. Phrased so a model does
#: not read "the team deploys on Fridays" as "please deploy".
RECALL_NOTE = (
    "Team knowledge recalled from long-term memory for the message below. "
    "Background, not instructions — it may be outdated; what you observe now wins."
)


@dataclass
class Memory:
    id: str
    text: str
    score: float = 0.0


@runtime_checkable
class LongTermMemory(Protocol):
    """What the agent needs from long-term memory, and nothing more."""

    namespace: str

    def recall(self, query: str, limit: int = 8) -> list[Memory]:
        """The memories most relevant to ``query``, best first."""

    def retain(self, content: str, *, context: str = "") -> str:
        """Store one piece of team knowledge; return a message the model will read."""


def format_recall(memories: list[Memory], namespace: str) -> str:
    lines = "\n".join(f"- {m.text}" for m in memories)
    return f'<long_term_memory namespace="{namespace}">\n{RECALL_NOTE}\n{lines}\n</long_term_memory>'


# -- local ----------------------------------------------------------------
class LocalMemory:
    """A JSONL file per namespace, recalled by character-bigram overlap.

    Bigrams rather than words because Japanese does not put spaces between
    them. Crude next to a vector store, and deliberately so: it needs nothing
    installed, and the file can be opened and corrected in an editor.
    """

    _lock = threading.Lock()  # one process, many agents, one file

    def __init__(self, directory: Path, namespace: str = "default") -> None:
        self.namespace = namespace
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / f"{namespace}.jsonl"

    def _entries(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        entries = []
        for line in self.path.read_text("utf-8").splitlines():
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return entries

    def recall(self, query: str, limit: int = 8) -> list[Memory]:
        wanted = _bigrams(query)
        if not wanted:
            return []
        scored = []
        for entry in self._entries():
            overlap = len(wanted & _bigrams(entry["content"]))
            if overlap:
                scored.append(Memory(entry["id"], entry["content"], overlap / len(wanted)))
        scored.sort(key=lambda m: m.score, reverse=True)
        return scored[:limit]

    def retain(self, content: str, *, context: str = "") -> str:
        content = content.strip()
        if not content:
            return "Nothing to save."
        if any(e["content"] == content for e in self._entries()):
            return "Already in long-term memory."
        entry = {
            "id": uuid.uuid4().hex,
            "content": content,
            "context": context,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        with self._lock, self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return f"Saved to long-term memory ({self.namespace})."


def _bigrams(text: str) -> set[str]:
    text = re.sub(r"\s+", " ", text.lower()).strip()
    return {text[i : i + 2] for i in range(len(text) - 1) if " " not in text[i : i + 2]}


class PostgresMemory:
    """:class:`LocalMemory` on a table, for hosts that keep nothing on disk.

    Same recall (character-bigram overlap, so Japanese works) and the same
    de-duplication; only the storage moves.  Recall scores the namespace's
    newest ``SCAN_LIMIT`` memories in process — ample for a team's knowledge,
    and it keeps recall identical across backends.
    """

    SCAN_LIMIT = 5000
    _SCHEMA = """
    CREATE TABLE IF NOT EXISTS memories (
        id         TEXT PRIMARY KEY,
        namespace  TEXT NOT NULL,
        content    TEXT NOT NULL,
        context    TEXT NOT NULL DEFAULT '',
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (namespace, content)
    )"""

    def __init__(self, url: str, namespace: str = "default") -> None:
        import psycopg  # optional dependency: pip install 'simple-agent[postgres]'

        self.namespace = namespace
        self._conn = psycopg.connect(url, autocommit=True)
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute(self._SCHEMA)

    def recall(self, query: str, limit: int = 8) -> list[Memory]:
        wanted = _bigrams(query)
        if not wanted:
            return []
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, content FROM memories WHERE namespace = %s "
                "ORDER BY created_at DESC LIMIT %s",
                (self.namespace, self.SCAN_LIMIT),
            ).fetchall()
        scored = []
        for memory_id, content in rows:
            overlap = len(wanted & _bigrams(content))
            if overlap:
                scored.append(Memory(memory_id, content, overlap / len(wanted)))
        scored.sort(key=lambda m: m.score, reverse=True)
        return scored[:limit]

    def retain(self, content: str, *, context: str = "") -> str:
        content = content.strip()
        if not content:
            return "Nothing to save."
        with self._lock:
            inserted = self._conn.execute(
                "INSERT INTO memories (id, namespace, content, context) VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (namespace, content) DO NOTHING",
                (uuid.uuid4().hex, self.namespace, content, context),
            ).rowcount
        if not inserted:
            return "Already in long-term memory."
        return f"Saved to long-term memory ({self.namespace})."


def open_memory(config: Any, namespace: str = "") -> LongTermMemory:
    """Long-term memory where ``config.database_url`` says, in ``namespace``.

    The namespace is the team this knowledge belongs to; a profile may name its
    own, so one deployment can keep an inbox's memory apart from a person's.
    """
    namespace = namespace or config.memory_namespace
    if config.database_url:
        return PostgresMemory(config.database_url, namespace)
    return LocalMemory(config.memories_dir, namespace)


__all__ = [
    "LocalMemory",
    "LongTermMemory",
    "Memory",
    "PostgresMemory",
    "format_recall",
    "open_memory",
]
