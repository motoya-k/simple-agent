"""Extension seams — declared here, implemented nowhere.

Hermes carries four capabilities this repo deliberately does not implement:
message gateways, subagent delegation, a rich TUI, and a multi-provider adapter
matrix.  Each is a large surface, and none of them belongs in the core.

What is worth keeping at this size is the *shape* — the exact boundary each one
would attach to.  These abstract classes are that shape.  Nothing in the running
agent imports them; they are here so that adding a capability is a new file
implementing an interface, never a change to ``agent.py`` or ``loop.py``.

Read DESIGN.md alongside this file: it explains what each capability does in
Hermes and which decisions are the load-bearing ones.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Iterable


# --------------------------------------------------------------------------
# 1. Gateway — one process, many chat platforms
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class InboundMessage:
    platform: str
    chat_id: str
    user_id: str
    text: str
    thread_id: str | None = None
    attachments: tuple[str, ...] = ()

    def session_key(self) -> str:
        """The identity of a conversation.

        The single most important line in a gateway. Every wrong answer of the
        form "the bot replied to the wrong person" or "it lost the thread" is a
        bug in this key. Group chats key on the thread; DMs key on the user;
        every platform is namespaced so ids from different platforms can never
        collide.
        """
        parts = [self.platform, self.chat_id, self.user_id, self.thread_id or "-"]
        return ":".join(parts)


class PlatformAdapter(ABC):
    """One class per chat platform. The runner never learns their differences."""

    platform: str

    @abstractmethod
    async def connect(self) -> None: ...

    @abstractmethod
    async def receive(self) -> Iterable[InboundMessage]: ...

    @abstractmethod
    async def send(self, chat_id: str, text: str, *, thread_id: str | None = None) -> None: ...

    @abstractmethod
    async def disconnect(self) -> None: ...


# --------------------------------------------------------------------------
# 2. Delegation — subagents that do not pollute the parent's context
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class DelegationResult:
    summary: str
    ok: bool
    tokens: int = 0


class Delegator(ABC):
    """Runs a task in a *fresh* agent and returns only a summary.

    The point is not parallelism, it is context hygiene: the child burns its own
    context window on searching, reading and failing, and the parent pays only
    for the conclusion.

    Two roles keep it safe. A ``leaf`` child gets a reduced toolset and cannot
    delegate further, write memory, or message the user. An ``orchestrator``
    child may spawn its own children, bounded by a maximum depth — otherwise a
    confused agent discovers unbounded recursion with a billing account.
    """

    max_depth: int = 2
    max_concurrent_children: int = 3

    @abstractmethod
    def delegate(
        self, task: str, *, role: str = "leaf", context: dict[str, Any] | None = None
    ) -> DelegationResult: ...


# --------------------------------------------------------------------------
# 3. UI — the loop emits events, the host decides how to draw them
# --------------------------------------------------------------------------
class Renderer(ABC):
    """``cli.py`` is the plain-text implementation of this idea.

    A TUI, a desktop app and a web client differ only in this class. The loop
    emits ``(kind, text)`` events and never formats anything, which is what
    keeps a second frontend from becoming a second agent.
    """

    @abstractmethod
    def on_event(self, kind: str, text: str) -> None: ...

    @abstractmethod
    def on_answer(self, text: str) -> None: ...
