"""A message host: Sources in, Router in the middle, Sinks out.

The terminal REPL is one host; this is the other kind — a process that listens
to one or more platforms and runs one agent per conversation.  It owns no
platform knowledge: that lives in the Sources and Sinks it is handed.

Three rules it enforces, because each is easy to get wrong in a platform
adapter and expensive when it is:

* **A narrowed route is an untrusted route.**  Long-term memory and skills are
  shared by every conversation.  A route that had to take the terminal away
  (an inbox anyone can write to) must also not be able to *teach* the trusted
  ones, so its conversations run without the background review, and it should
  only be given read-only tools.  Without ``memory_search`` it is not handed
  the team's long-term memory either.
* **A message is acknowledged once, after the turn** — successfully or not.
  A crash mid-turn leaves it unacknowledged, so it is replayed on restart;
  a message that makes the agent fail every time is not retried forever.
* **An answer that cannot be delivered is kept.**  It cost a model call and
  cannot be regenerated identically, so a failed send goes to a dead-letter
  file rather than the log.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Iterable

from .registry import AgentRegistry
from .seams import Destination, InboundMessage, Route, Router, Sink, Source

log = logging.getLogger(__name__)

# What an untrusted route may use: look things up, never write anything that
# another conversation will read. Skills qualify because they are abstract;
# long-term memory does not, because it is the team's own knowledge.
READ_ONLY_TOOLS = ("skill_view",)


@dataclass(frozen=True)
class AllowlistRouter(Router):
    """Handle mail from known senders only; drop everything else unseen.

    ``allow`` entries are full addresses (``alice@example.com``) or whole
    domains (``@example.com``).  A sender address is trivially forged, so this
    is a cost filter — strangers never cost a model call — not a security
    boundary.  The boundary is ``tools``.
    """

    allow: tuple[str, ...]
    tools: tuple[str, ...] | None = READ_ONLY_TOOLS
    to: tuple[Destination, ...] = ()

    def route(self, message: InboundMessage) -> Route | None:
        sender = message.user_id.lower()
        for entry in self.allow:
            entry = entry.lower().strip()
            if entry and (sender == entry or (entry.startswith("@") and sender.endswith(entry))):
                return Route(to=self.to, tools=self.tools)
        return None


class DeadLetters:
    """Answers that could not be delivered, one JSON object per line."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def keep(self, message: InboundMessage, to: Destination, text: str, error: str) -> None:
        record = {
            "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "message_id": message.message_id,
            "from": message.user_id,
            "to": {"platform": to.platform, "chat_id": to.chat_id, "thread_id": to.thread_id},
            "text": text,
            "error": error,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


class Host:
    def __init__(
        self,
        config,
        *,
        sources: Iterable[Source],
        router: Router,
        sinks: Iterable[Sink] = (),
        agent_factory=None,
        dead_letters: DeadLetters | None = None,
    ) -> None:
        self.config = config
        self.sources = list(sources)
        self.router = router
        self.sinks = {sink.platform: sink for sink in sinks}
        self.dead_letters = dead_letters or DeadLetters(config.home / "dead_letters.jsonl")
        self._factory = agent_factory or self._default_factory
        self._routes: dict[str, Route] = {}
        self.agents = AgentRegistry(
            self._build, max_agents=config.max_agents, idle_seconds=config.agent_idle_seconds
        )

    # -- agents ---------------------------------------------------------
    def _build(self, session_key: str, source):
        route = self._routes[session_key]
        config = self.config
        if route.tools is not None:
            config = replace(config, learning=False)  # untrusted: see module doc
        return self._factory(config, session_key=session_key, source=source, tools=route.tools)

    def _default_factory(self, config, *, session_key, source, tools):
        from .agent import Agent
        from .state import open_store

        if not hasattr(self, "_store"):
            self._store = open_store(config)  # one connection for all agents
        return Agent(config, source=source, session_key=session_key, tools=tools, store=self._store)

    # -- running --------------------------------------------------------
    async def serve(self) -> None:
        await asyncio.gather(*(self._drain(source) for source in self.sources))

    async def _drain(self, source: Source) -> None:
        async for message in source.messages():
            try:
                await self.handle(message)
            finally:
                await source.ack(message)

    async def handle(self, message: InboundMessage) -> str | None:
        """Run one message through the router, the agent, and the sinks."""
        route = self.router.route(message)
        if route is None:
            log.info("dropped %s from %s (no route)", message.message_id, message.user_id)
            return None

        key = message.session_key(
            group_sessions_per_user=self.config.group_sessions_per_user,
            thread_sessions_per_user=self.config.thread_sessions_per_user,
        )
        self._routes[key] = route
        signature = "tools=" + (",".join(route.tools) if route.tools is not None else "*")
        agent = self.agents.get(key, message.source(), signature=signature)

        for to in route.to:
            sink = self.sinks.get(to.platform)
            if sink is not None:
                await sink.on_turn_start(message, to)
        ok = False
        text = ""
        try:
            with self.agents.lease(key):
                turn = await asyncio.to_thread(agent.run, message.text)
            text, ok = turn.text, True
        except Exception:
            log.exception("turn failed for %s", message.message_id)
        finally:
            for to in route.to:
                sink = self.sinks.get(to.platform)
                if sink is not None:
                    await sink.on_turn_end(message, to, ok)

        if ok and text:
            await self._deliver(message, route, text)
        return text if ok else None

    async def _deliver(self, message: InboundMessage, route: Route, text: str) -> None:
        for to in route.to:
            sink = self.sinks.get(to.platform)
            try:
                if sink is None:
                    raise RuntimeError(f"no sink for platform {to.platform!r}")
                await sink.send(to, text)
            except Exception as exc:
                log.warning("delivery to %s failed: %s", to.platform, exc)
                self.dead_letters.keep(message, to, text, repr(exc))
