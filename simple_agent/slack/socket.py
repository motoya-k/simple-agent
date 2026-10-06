"""Slack Socket Mode source.

Socket Mode and not the Events API, for one reason: the connection is outbound.
No public URL, no load balancer, no request signatures to verify — the same
container that runs the mail host runs this one, in a private subnet, and the
only secrets are two tokens.

How it works: ``apps.connections.open`` mints a single-use ``wss://`` URL, the
app connects, and Slack pushes an *envelope* per event down it.  Each envelope
must be acknowledged within three seconds, which is nothing like long enough to
answer; so the acknowledgement and the answer are separate, and the event is
written to :mod:`simple_agent.slack.inbox` before it is acknowledged, so it can
be replayed if this process dies mid-turn.

Two things that look like over-engineering and are not:

* **The socket is read by its own task, not by the generator.**  The host
  suspends ``messages()`` whenever it is at its concurrency limit; a generator
  that read the socket directly would stop answering pings and acknowledging
  envelopes while it waited, and Slack would drop the connection.  Reading
  continues regardless, and backpressure lands on a bounded queue instead,
  which is safe because the events in it are already on disk.
* **A reconnect is normal, not an error.**  Slack recycles connections every
  few hours and says so with a ``disconnect`` envelope; ``refresh_requested``
  is routine.  So is the silence of a connection that died without saying so,
  which the read timeout turns into the same reconnect.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
from typing import Any, AsyncIterator

from ..seams import InboundMessage, Source
from . import ws
from .api import SlackApi
from .inbox import Inbox
from .parse import PLATFORM, parse_event

log = logging.getLogger(__name__)

MAX_RECONNECT_DELAY = 60.0


class SlackSource(Source):
    platform = PLATFORM

    def __init__(
        self,
        *,
        app_token: str,
        bot_token: str,
        inbox: Inbox,
        require_mention: bool = True,
        api: SlackApi | None = None,
        app_api: SlackApi | None = None,
        connect=ws.connect,
        queue_size: int = 64,
        reconnect_seconds: float = 3.0,
        read_timeout: float = 60.0,
    ) -> None:
        self._api = api or SlackApi(bot_token)
        self._app_api = app_api or SlackApi(app_token)
        self.inbox = inbox
        self.require_mention = require_mention
        self._connect = connect
        self._queue_size = max(1, queue_size)
        self._reconnect_seconds = reconnect_seconds
        self._read_timeout = read_timeout
        self._bot_user_id = ""
        #: Identifies this app in this workspace, so two deployments sharing one
        #: database do not read each other's inbox.
        self._key = ""
        self._connection: ws.WebSocket | None = None
        #: (channel, ts) -> the event ids that delivered it, so ``ack`` can
        #: finish every copy. A second delivery of one message is not a second
        #: question, but it is a second row to close.
        self._pending: dict[tuple[str, str], list[str]] = {}

    # -- receiving ------------------------------------------------------
    async def messages(self) -> AsyncIterator[InboundMessage]:
        await asyncio.to_thread(self._identify)
        queue: asyncio.Queue[InboundMessage | None] = asyncio.Queue(self._queue_size)
        pump = asyncio.create_task(self._pump(queue))
        try:
            while True:
                message = await queue.get()
                if message is None:
                    break
                yield message
        finally:
            # Close the socket before waiting for the reader: it is blocked in
            # recv on a worker thread, and cancelling a thread does not
            # interrupt it. Closing does.
            pump.cancel()
            await self.close()
            with contextlib.suppress(asyncio.CancelledError):
                await pump

    def _identify(self) -> None:
        """Who the agent is in this workspace — and how it recognises itself.

        Without ``bot_user_id`` the agent's own replies come back as events it
        would answer; without a mention to strip, every channel message reads
        as addressed to it.  Worth one API call at startup.
        """
        who = self._api.call("auth.test")
        self._bot_user_id = str(who.get("user_id", ""))
        self._key = f"{who.get('team_id', '')}/{self._bot_user_id}"
        log.info("slack: %s in %s", who.get("user", "?"), who.get("team", "?"))

    async def _pump(self, queue: asyncio.Queue) -> None:
        """Keep one connection alive, forever, and feed what arrives into ``queue``."""
        try:
            await self._replay(queue)
            delay = self._reconnect_seconds
            while True:
                try:
                    connection = await asyncio.to_thread(self._open)
                except Exception as exc:  # token, network, Slack itself
                    log.warning("slack connect failed (%s), retrying in %.0fs", exc, delay)
                    await asyncio.sleep(delay * random.uniform(0.5, 1.0))
                    delay = min(MAX_RECONNECT_DELAY, delay * 2)
                    continue
                delay = self._reconnect_seconds
                self._connection = connection
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(self.inbox.prune, self._key)
                try:
                    await self._read(connection, queue)
                except OSError as exc:  # timeout, reset, half-open connection
                    log.info("slack connection ended (%s); reconnecting", exc)
                finally:
                    self._connection = None
                    with contextlib.suppress(Exception):
                        await asyncio.to_thread(connection.close)
                await asyncio.sleep(self._reconnect_seconds)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("slack source stopped")
            queue.put_nowait(None)

    def _open(self) -> ws.WebSocket:
        url = str(self._app_api.call("apps.connections.open", form=True).get("url", ""))
        if not url:
            raise ws.WebSocketError("apps.connections.open returned no url")
        return self._connect(url, read_timeout=self._read_timeout)

    async def _read(self, connection: ws.WebSocket, queue: asyncio.Queue) -> None:
        while True:
            frame = await asyncio.to_thread(connection.recv)
            if frame is None:
                return  # the peer closed; the caller reconnects
            try:
                envelope = json.loads(frame)
            except json.JSONDecodeError:
                log.warning("slack sent something that is not JSON; ignoring")
                continue
            if not await self._handle(envelope, frame, connection, queue):
                return

    async def _handle(
        self,
        envelope: dict[str, Any],
        raw: str,
        connection: ws.WebSocket,
        queue: asyncio.Queue,
    ) -> bool:
        """Deal with one envelope. ``False`` means this connection is finished."""
        kind = envelope.get("type")
        if kind == "hello":
            log.info("slack socket open")
            return True
        if kind == "disconnect":
            log.info("slack asked for a reconnect (%s)", envelope.get("reason", ""))
            return False

        event_id = str((envelope.get("payload") or {}).get("event_id", ""))
        envelope_id = envelope.get("envelope_id")
        first_sight = True
        if kind == "events_api" and event_id:
            # Written down *before* the acknowledgement, because acknowledging
            # is what stops Slack from re-sending it. A local insert costs a
            # millisecond or two of the three seconds available.
            first_sight = await asyncio.to_thread(self.inbox.claim, self._key, event_id, raw)
        if envelope_id:
            with contextlib.suppress(OSError):
                await asyncio.to_thread(connection.send, json.dumps({"envelope_id": envelope_id}))
        if kind != "events_api":
            return True  # slash commands and interactivity are not this host's
        if not first_sight:
            log.debug("slack event %s already handled", event_id)
            return True

        message = parse_event(
            envelope, bot_user_id=self._bot_user_id, require_mention=self.require_mention
        )
        if message is None:
            await asyncio.to_thread(self.inbox.mark, self._key, event_id)
            return True
        self._queue(message, event_id)
        await queue.put(message)
        return True

    async def _replay(self, queue: asyncio.Queue) -> None:
        """Events claimed by a process that died before finishing them."""
        rows = await asyncio.to_thread(self.inbox.pending, self._key)
        for event_id, payload in rows:
            try:
                envelope = json.loads(payload)
            except json.JSONDecodeError:
                await asyncio.to_thread(self.inbox.mark, self._key, event_id)
                continue
            message = parse_event(
                envelope, bot_user_id=self._bot_user_id, require_mention=self.require_mention
            )
            if message is None:
                await asyncio.to_thread(self.inbox.mark, self._key, event_id)
                continue
            log.info("replaying slack event %s", event_id)
            self._queue(message, event_id)
            await queue.put(message)

    def _queue(self, message: InboundMessage, event_id: str) -> None:
        self._pending.setdefault((message.chat_id, message.message_id), []).append(event_id)

    async def ack(self, message: InboundMessage) -> None:
        for event_id in self._pending.pop((message.chat_id, message.message_id), []):
            await asyncio.to_thread(self.inbox.mark, self._key, event_id)

    async def close(self) -> None:
        connection, self._connection = self._connection, None
        if connection is not None:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(connection.close)
