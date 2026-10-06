"""A message host: Sources in, Router in the middle, Sinks out.

The terminal REPL is one host; this is the other kind — a process that listens
to one or more platforms and runs one agent per conversation.  It owns no
platform knowledge: that lives in the Sources and Sinks it is handed.

Three rules it enforces, because each is easy to get wrong in a platform
adapter and expensive when it is:

* **A route runs under a profile, and the profile says how far it reaches.**
  Long-term memory and skills are shared by every conversation, so a route
  that had to take the terminal away (an inbox anyone can write to) must not
  be able to *teach* the trusted ones either.  The ``email`` profile says both
  at once — read-only tools, ``learning: false`` — and the host just obeys it;
  see :mod:`simple_agent.profile`.  Without ``memory_search`` such a route is
  not handed the team's long-term memory either.
* **A message is acknowledged once, after the turn** — successfully or not.
  A crash mid-turn leaves it unacknowledged, so it is replayed on restart;
  a message that makes the agent fail every time is not retried forever.
* **An answer that cannot be delivered is kept.**  It cost a model call and
  cannot be regenerated identically, so a failed send goes to a dead-letter
  file rather than the log.

And three that make it safe to run unattended in a container:

* **Turns run concurrently, conversations do not.**  Up to
  ``max_concurrent_turns`` messages are worked on at once, but two messages in
  the same conversation run in the order they arrived.
* **SIGTERM drains.**  A deploy stops taking new messages, gives turns in
  flight ``SHUTDOWN_GRACE`` seconds to finish, then interrupts the rest.  An
  interrupted message was never acknowledged, so it is replayed on restart.
* **A heartbeat file** is touched while the host is healthy, for a container
  health check (``simple-agent --health``).
"""

from __future__ import annotations

import asyncio
import json
import logging
import signal
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .profile import BUILT_IN, Profile, load_profile
from .registry import AgentRegistry
from .seams import Destination, InboundMessage, Route, Router, Sink, Source

log = logging.getLogger(__name__)

# ECS gives a task 30s by default and at most 120s between SIGTERM and SIGKILL;
# set the task's stopTimeout above this.
SHUTDOWN_GRACE = 90.0
HEARTBEAT_SECONDS = 30.0


@dataclass(frozen=True)
class AllowlistRouter(Router):
    """Handle mail from known senders only; drop everything else unseen.

    ``allow`` entries are full addresses (``alice@example.com``) or whole
    domains (``@example.com``).  A sender address is trivially forged, so this
    is a cost filter — strangers never cost a model call — not a security
    boundary.  The boundary is ``tools``.
    """

    allow: tuple[str, ...]
    profile: Profile = BUILT_IN["email"]
    to: tuple[Destination, ...] = ()

    def route(self, message: InboundMessage) -> Route | None:
        sender = message.user_id.lower()
        for entry in self.allow:
            entry = entry.lower().strip()
            if entry and (sender == entry or (entry.startswith("@") and sender.endswith(entry))):
                return Route(to=self.to, profile=self.profile)
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
        # The profile each live conversation was built under, so the agent
        # the registry asks for is built with the one its route resolved to.
        self._profiles: dict[str, Profile] = {}
        self.agents = AgentRegistry(
            self._build, max_agents=config.max_agents, idle_seconds=config.agent_idle_seconds
        )
        self.heartbeat = config.home / "heartbeat"
        self._slots = asyncio.Semaphore(max(1, int(config.max_concurrent_turns)))
        self._conversation_locks: dict[str, asyncio.Lock] = {}
        self._in_flight: set[asyncio.Task] = set()
        self._live: dict[str, object] = {}  # session key -> agent mid-turn
        self._stopping = asyncio.Event()

    # -- agents ---------------------------------------------------------
    def _build(self, session_key: str, source):
        profile = self._profiles[session_key]
        return self._factory(
            self.config, session_key=session_key, source=source, profile=profile
        )

    def _default_factory(self, config, *, session_key, source, profile):
        from .agent import Agent
        from .state import open_store

        if not hasattr(self, "_store"):
            self._store = open_store(config)  # one connection for all agents
        return Agent(
            config, source=source, session_key=session_key, profile=profile, store=self._store
        )

    # -- running --------------------------------------------------------
    async def serve(self) -> None:
        """Run until every source is exhausted or SIGTERM/SIGINT arrives."""
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, self.stop)
            except (NotImplementedError, RuntimeError, ValueError):
                pass  # not the main thread, or not a platform with signals
        drains = [asyncio.create_task(self._drain(source)) for source in self.sources]
        beat = asyncio.create_task(self._beat())
        waiter = asyncio.create_task(self._stopping.wait())
        try:
            await asyncio.wait([*drains, waiter], return_when=asyncio.FIRST_COMPLETED)
            if not self._stopping.is_set():  # every source ran dry
                await asyncio.gather(*drains, return_exceptions=True)
        finally:
            for task in (*drains, waiter):
                task.cancel()
            await self._finish_in_flight()
            beat.cancel()
            for source in self.sources:
                close = getattr(source, "close", None)
                if close is not None:
                    await close()

    def stop(self) -> None:
        """Stop taking messages; turns in flight get SHUTDOWN_GRACE to finish."""
        if not self._stopping.is_set():
            log.info("stopping: draining %d turn(s) in flight", len(self._in_flight))
            self._stopping.set()

    async def _drain(self, source: Source) -> None:
        async for message in source.messages():
            await self._slots.acquire()
            if self._stopping.is_set():
                self._slots.release()
                return  # not acknowledged, so it is read again after restart
            task = asyncio.create_task(self._process(source, message))
            self._in_flight.add(task)
            task.add_done_callback(self._in_flight.discard)

    async def _process(self, source: Source, message: InboundMessage) -> None:
        lock = self._conversation_locks.setdefault(message.session_key(), asyncio.Lock())
        try:
            async with lock:
                await self.handle(message)
        except asyncio.CancelledError:
            raise  # interrupted by shutdown: leave it unacknowledged
        except Exception:
            log.exception("handling %s failed", message.message_id)
            await source.ack(message)
        else:
            await source.ack(message)
        finally:
            self._slots.release()

    async def _finish_in_flight(self) -> None:
        if not self._in_flight:
            return
        done, pending = await asyncio.wait(set(self._in_flight), timeout=SHUTDOWN_GRACE)
        if pending:
            log.warning("interrupting %d turn(s) still running after %.0fs", len(pending), SHUTDOWN_GRACE)
            for agent in list(self._live.values()):
                agent.interrupt()
            await asyncio.wait(pending, timeout=10)

    async def _beat(self) -> None:
        self.heartbeat.parent.mkdir(parents=True, exist_ok=True)
        while True:
            self.heartbeat.write_text(str(time.time()))
            await asyncio.sleep(HEARTBEAT_SECONDS)

    async def handle(self, message: InboundMessage) -> str | None:
        """Run one message through the router, the agent, and the sinks."""
        route = self.router.route(message)
        if route is None:
            log.info("dropped %s from %s (no route)", message.message_id, message.user_id)
            return None

        profile = route.profile or load_profile(self.config)
        key = message.session_key(profile=profile.namespace or self.config.memory_namespace)
        self._profiles[key] = profile
        # A conversation whose profile changed must not keep the agent built
        # under the old one: that is how a widened toolset would leak backwards.
        # The name is not enough — an edited profile keeps its name.
        signature = f"{profile.name}:{profile.tools}:{profile.learning}:{profile.namespace}"
        agent = self.agents.get(key, message.source(), signature=signature)

        for to in route.to:
            sink = self.sinks.get(to.platform)
            if sink is not None:
                await sink.on_turn_start(message, to)
        ok = False
        text = ""
        try:
            with self.agents.lease(key):
                self._live[key] = agent
                try:
                    turn = await asyncio.to_thread(agent.run, message.text)
                finally:
                    self._live.pop(key, None)
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
