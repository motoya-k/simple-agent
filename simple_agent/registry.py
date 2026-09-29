"""One agent per conversation, kept warm — the layer a chat host needs.

A terminal host builds one :class:`~simple_agent.agent.Agent` and keeps it.  A
chat host cannot: it has as many conversations as people care to start, they
arrive interleaved, and they never formally end.

Rebuilding the agent for every message looks harmless and is not.  The system
prompt is assembled once per agent (see :mod:`simple_agent.memory`), and a
rebuilt prompt is a *different* prompt, so the provider's prompt cache misses
on every turn and the same conversation costs several times more.  Keeping the
agent is what makes the frozen prefix pay off.

So this is a cache with three questions to answer:

* **When is a cached agent still the right one?**  When its configuration
  signature matches, and when nobody else has written to its transcript.
* **When must one be let go?**  Least-recently-used past a cap, or idle past a
  deadline — but never while it is mid-turn.
* **What survives eviction?**  The transcript, because it is on disk.  The
  agent object is rebuilt from it.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator

DEFAULT_MAX_AGENTS = 128
DEFAULT_IDLE_SECONDS = 3600.0


@dataclass
class _Entry:
    agent: Any
    signature: str
    message_count: int
    last_used: float = field(default_factory=time.monotonic)
    busy: int = 0

    def touch(self) -> None:
        self.last_used = time.monotonic()


class AgentRegistry:
    """Maps a session key to a live agent, and decides when to let one go.

    ``factory(session_key, source)`` builds a fresh agent.  It is called with
    the registry lock released, so building one may take as long as it needs.

    Give the factory a **shared** :class:`~simple_agent.state.Store` rather
    than letting each agent open its own::

        store = open_store(config)
        registry = AgentRegistry(
            lambda key, src: Agent(config, source=src, store=store)
        )

    A hundred conversations otherwise mean a hundred connections to one file,
    each serializing writes against a lock the others cannot see.
    """

    def __init__(
        self,
        factory: Callable[..., Any],
        *,
        max_agents: int = DEFAULT_MAX_AGENTS,
        idle_seconds: float = DEFAULT_IDLE_SECONDS,
        on_evict: Callable[[Any], None] | None = None,
    ) -> None:
        self.factory = factory
        self.max_agents = max_agents
        self.idle_seconds = idle_seconds
        self.on_evict = on_evict
        self._entries: "OrderedDict[str, _Entry]" = OrderedDict()
        self._lock = threading.Lock()

    # -- lookup ---------------------------------------------------------
    def get(self, session_key: str, source: Any = None, *, signature: str = "") -> Any:
        """Return the agent for this conversation, building one if needed."""
        stale = None
        with self._lock:
            entry = self._entries.get(session_key)
            if entry is not None:
                if self._still_valid(entry, signature):
                    entry.touch()
                    self._entries.move_to_end(session_key)
                    return entry.agent
                stale = self._entries.pop(session_key, None)

        # Released the lock before doing anything slow. Tearing an agent down
        # can block on file handles and subprocesses; holding the lock through
        # that stalls every other conversation in the process.
        if stale is not None:
            self._release(stale.agent)

        agent = self.factory(session_key, source)
        with self._lock:
            self._entries[session_key] = _Entry(
                agent=agent,
                signature=signature,
                message_count=_message_count(agent),
            )
            self._entries.move_to_end(session_key)
            victims = [self._entries.pop(key) for key in self._select_for_eviction()]
        for victim in victims:
            self._release(victim.agent)
        return agent

    def _still_valid(self, entry: _Entry, signature: str) -> bool:
        if signature and entry.signature != signature:
            return False
        # Someone else wrote to this conversation's transcript (another
        # process, a repair script, a second host). The in-memory copy is now
        # out of date, so rebuild from disk rather than answer from a stale
        # one. Our own writes are accounted for when the turn's lease ends.
        current = _message_count(entry.agent)
        if current is None or entry.message_count is None:
            return True
        return current == entry.message_count

    # -- lifetime -------------------------------------------------------
    @contextmanager
    def lease(self, session_key: str) -> Iterator[None]:
        """Mark a conversation as mid-turn so eviction leaves it alone."""
        with self._lock:
            entry = self._entries.get(session_key)
            if entry is not None:
                entry.busy += 1
        try:
            yield
        finally:
            with self._lock:
                entry = self._entries.get(session_key)
                if entry is not None:
                    entry.busy = max(0, entry.busy - 1)
                    entry.touch()
                    entry.message_count = _message_count(entry.agent)

    def evict(self, session_key: str) -> bool:
        with self._lock:
            entry = self._entries.pop(session_key, None)
        if entry is None:
            return False
        self._release(entry.agent)
        return True

    def sweep_idle(self) -> int:
        """Drop agents nobody has spoken to in a while. Safe to call anytime."""
        deadline = time.monotonic() - self.idle_seconds
        with self._lock:
            expired = [
                key
                for key, entry in self._entries.items()
                if entry.busy == 0 and entry.last_used < deadline
            ]
            victims = [self._entries.pop(key) for key in expired]
        for entry in victims:
            self._release(entry.agent)
        return len(victims)

    def _select_for_eviction(self) -> list[str]:
        """Least-recently-used keys over the cap, skipping anything mid-turn.

        A conversation that is over the cap but busy stays. The cap is
        re-checked on the next insert, by which time its turn has finished.
        """
        excess = len(self._entries) - self.max_agents
        if excess <= 0:
            return []
        chosen = []
        for key, entry in list(self._entries.items()):
            if len(chosen) >= excess:
                break
            if entry.busy == 0:
                chosen.append(key)
        return chosen

    def _release(self, agent: Any) -> None:
        if self.on_evict is None:
            return
        try:
            self.on_evict(agent)
        except Exception:
            pass  # letting go of an agent must never break the caller

    # -- introspection --------------------------------------------------
    def keys(self) -> list[str]:
        with self._lock:
            return list(self._entries)

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def __contains__(self, session_key: object) -> bool:
        with self._lock:
            return session_key in self._entries


def _message_count(agent: Any) -> int | None:
    """How many messages the store believes this conversation has."""
    store = getattr(agent, "store", None)
    session_id = getattr(agent, "session_id", None)
    if store is None or not session_id:
        return None
    counter = getattr(store, "message_count", None)
    if not callable(counter):
        return None
    try:
        return counter(session_id)
    except Exception:
        return None
