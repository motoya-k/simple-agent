"""IMAP polling source.

Reads without touching the mailbox: the folder is selected read-only and
bodies are fetched with ``BODY.PEEK``, so a person reading the same inbox sees
nothing marked as read.  What has been handled is recorded in the agent's own
state database instead — see :mod:`simple_agent.mail.ledger`.

The first poll of a folder records its current newest UID as a baseline and
only handles mail that arrives after it.  Pointing the agent at an existing
inbox must not answer ten years of backlog.

``imaplib`` is blocking; each poll runs in a worker thread.
"""

from __future__ import annotations

import asyncio
import imaplib
import logging
from typing import AsyncIterator

from ..seams import InboundMessage, Source
from .ledger import Ledger
from .parse import PLATFORM, parse_message

log = logging.getLogger(__name__)


class ImapSource(Source):
    platform = PLATFORM

    def __init__(
        self,
        *,
        host: str,
        user: str,
        password: str,
        ledger: Ledger,
        mailbox: str = "INBOX",
        port: int = 993,
        poll_seconds: float = 60.0,
        connect=imaplib.IMAP4_SSL,
    ) -> None:
        self.host, self.port, self.user, self.password = host, port, user, password
        self.mailbox = mailbox
        self.ledger = ledger
        self.poll_seconds = poll_seconds
        self._connect = connect
        self._key = f"{user}@{host}/{mailbox}"
        # message_id -> every (uidvalidity, uid) carrying it, so ack() can
        # record them all. Also what keeps the next poll from fetching mail a
        # turn is still working on: the host runs turns concurrently.
        self._pending: dict[str, list[tuple[int, int]]] = {}

    async def messages(self) -> AsyncIterator[InboundMessage]:
        while True:
            try:
                batch = await asyncio.to_thread(self.poll)
            except (OSError, imaplib.IMAP4.error) as exc:
                # A dropped connection or a server hiccup is not a reason to
                # stop listening; the next poll reconnects.
                log.warning("IMAP poll failed: %s", exc)
                batch = []
            for message in batch:
                yield message
            await asyncio.sleep(self.poll_seconds)

    def poll(self) -> list[InboundMessage]:
        """One connection: fetch every unhandled message newer than the baseline."""
        client = self._connect(self.host, self.port)
        try:
            client.login(self.user, self.password)
            client.select(self.mailbox, readonly=True)
            uidvalidity = int(_untagged(client, "UIDVALIDITY") or 0)
            uids = _uids(client, "ALL")
            baseline = self.ledger.baseline(self._key, uidvalidity, max(uids, default=0))

            in_flight = {entry for entries in self._pending.values() for entry in entries}
            found: list[InboundMessage] = []
            for uid in uids:
                if uid <= baseline or (uidvalidity, uid) in in_flight:
                    continue
                if self.ledger.seen(self._key, uidvalidity, uid):
                    continue
                status, data = client.uid("FETCH", str(uid), "(BODY.PEEK[])")
                raw = _literal(data) if status == "OK" else None
                if raw is None:
                    continue
                message = parse_message(raw)
                if not message.message_id:
                    message = _with_message_id(message, f"uid:{uid}")
                copies = self._pending.setdefault(message.message_id, [])
                copies.append((uidvalidity, uid))
                if len(copies) == 1:  # a second copy of the same message is not a second request
                    found.append(message)
            return found
        finally:
            try:
                client.logout()
            except Exception:
                pass

    async def ack(self, message: InboundMessage) -> None:
        for entry in self._pending.pop(message.message_id, []):
            self.ledger.mark(self._key, *entry)


def _uids(client, criteria: str) -> list[int]:
    status, data = client.uid("SEARCH", None, criteria)
    if status != "OK" or not data or not data[0]:
        return []
    return sorted(int(uid) for uid in data[0].split())


def _untagged(client, name: str) -> str:
    _, data = client.response(name)
    value = data[0] if data else None
    return value.decode() if isinstance(value, bytes) else str(value or "")


def _literal(data) -> bytes | None:
    for item in data or []:
        if isinstance(item, tuple) and len(item) >= 2:
            return item[1]
    return None


def _with_message_id(message: InboundMessage, message_id: str) -> InboundMessage:
    from dataclasses import replace

    return replace(message, message_id=message_id, thread_id=message.thread_id or message_id)
