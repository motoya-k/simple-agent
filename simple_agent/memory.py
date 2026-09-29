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

Three backends implement :class:`LongTermMemory`: :class:`LocalMemory` (a
JSONL file, keyword recall, no dependencies — the default and what tests
use), :class:`Mem0Memory` and :class:`HindsightMemory`.  The last two call
their HTTP APIs with the standard library, so the repo stays dependency-free.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.request
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


# -- hosted ---------------------------------------------------------------
def _post_json(url: str, body: dict[str, Any], headers: dict[str, str], timeout: int) -> Any:
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read() or b"{}")


class Mem0Memory:
    """mem0's hosted API. The namespace is mem0's ``app_id``: shared by the
    team, not tied to one ``user_id``.  mem0 extracts and de-duplicates facts
    itself, so ``retain`` hands it the raw statement.

    https://docs.mem0.ai/api-reference/memory/add-memories
    """

    def __init__(
        self, api_key: str, namespace: str = "default", *, base_url: str = "", timeout: int = 10
    ) -> None:
        self.namespace = namespace
        self.api_key = api_key
        self.base_url = (base_url or os.environ.get("MEM0_API_URL") or "https://api.mem0.ai").rstrip("/")
        self.timeout = timeout

    def _post(self, path: str, body: dict[str, Any]) -> Any:
        headers = {"Authorization": f"Token {self.api_key}"}
        return _post_json(f"{self.base_url}{path}", body, headers, self.timeout)

    def recall(self, query: str, limit: int = 8) -> list[Memory]:
        data = self._post(
            "/v3/memories/search/",
            {"query": query, "filters": {"app_id": self.namespace}, "top_k": limit},
        )
        return [
            Memory(str(r.get("id", "")), r["memory"], float(r.get("score") or 0))
            for r in data.get("results", [])
            if r.get("memory")
        ]

    def retain(self, content: str, *, context: str = "") -> str:
        data = self._post(
            "/v3/memories/add/",
            {
                "messages": [{"role": "user", "content": content}],
                "app_id": self.namespace,
                "metadata": {"context": context} if context else {},
            },
        )
        return f"Sent to mem0 ({self.namespace}): {data.get('status', 'ok')}."


class HindsightMemory:
    """A Hindsight server. The namespace is the memory *bank*.

    https://hindsight.vectorize.io/api-reference
    """

    def __init__(
        self,
        namespace: str = "default",
        *,
        base_url: str = "",
        api_key: str = "",
        timeout: int = 30,
    ) -> None:
        self.namespace = namespace
        base = base_url or os.environ.get("HINDSIGHT_API_URL") or "http://localhost:8888"
        self.bank_url = f"{base.rstrip('/')}/v1/default/banks/{namespace}/memories"
        self.api_key = api_key or os.environ.get("HINDSIGHT_API_KEY", "")
        self.timeout = timeout

    def _post(self, url: str, body: dict[str, Any]) -> Any:
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        return _post_json(url, body, headers, self.timeout)

    def recall(self, query: str, limit: int = 8) -> list[Memory]:
        data = self._post(f"{self.bank_url}/recall", {"query": query, "budget": "low"})
        return [
            Memory(str(r.get("id", "")), r["text"])
            for r in data.get("results", [])[:limit]
            if r.get("text")
        ]

    def retain(self, content: str, *, context: str = "") -> str:
        item: dict[str, Any] = {"content": content}
        if context:
            item["context"] = context
        self._post(self.bank_url, {"items": [item]})
        return f"Retained in Hindsight bank {self.namespace!r}."


def open_memory(config: Any) -> LongTermMemory:
    """The long-term memory ``config.memory_backend`` names."""
    backend = config.memory_backend
    namespace = config.memory_namespace
    if backend == "local":
        return LocalMemory(config.memories_dir, namespace)
    if backend == "mem0":
        key = os.environ.get("MEM0_API_KEY", "")
        if not key:
            raise ValueError("memory_backend=mem0 needs MEM0_API_KEY")
        return Mem0Memory(key, namespace)
    if backend == "hindsight":
        return HindsightMemory(namespace)
    raise ValueError(f"Unknown memory_backend: {backend!r} (local | mem0 | hindsight)")


__all__ = [
    "HindsightMemory",
    "LocalMemory",
    "LongTermMemory",
    "Mem0Memory",
    "Memory",
    "format_recall",
    "open_memory",
]
