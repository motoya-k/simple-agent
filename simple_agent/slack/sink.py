"""Slack as a Sink: post the answer, and show on the triggering message that work is happening.

Two things a Slack adapter has to get right that a terminal never has to:

* **Slack's markup is not Markdown.**  One asterisk is bold, headings do not
  exist, and links are ``<url|label>``.  A model writing ordinary Markdown
  therefore reads as line noise in a channel, so the text is converted on the
  way out — by :func:`to_mrkdwn`, which also escapes the three characters Slack
  treats as syntax.
* **A message has a length limit.**  Past roughly 4000 characters Slack
  truncates, and a truncated answer is a lie, so a long one is split across
  messages on paragraph boundaries — reopening a code fence if the split landed
  inside one.

Progress is a reaction on the message that asked, not a running commentary:
in a channel every line is permanent and everybody sees it.  See
:class:`simple_agent.seams.Sink`.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Iterator

from ..seams import Destination, InboundMessage, Sink
from .api import SlackApi
from .parse import PLATFORM

log = logging.getLogger(__name__)

#: Slack's hard limit is 4000 characters; the margin leaves room for a reopened
#: code fence and for whatever Slack counts differently than Python does.
MAX_CHARS = 3800

#: Errors that mean "the thing you asked for is already true". Racing with a
#: human who removed the reaction first is not worth an exception.
_REACTION_NOISE = ("already_reacted", "no_reaction", "message_not_found")

_CODE = re.compile(r"```.*?```|`[^`\n]+`", re.DOTALL)


class SlackSink(Sink):
    platform = PLATFORM

    def __init__(
        self,
        *,
        bot_token: str = "",
        api: SlackApi | None = None,
        working_emoji: str = "eyes",
        failed_emoji: str = "warning",
        max_chars: int = MAX_CHARS,
        unfurl_links: bool = False,
    ) -> None:
        self._api = api or SlackApi(bot_token)
        self.working_emoji = working_emoji
        self.failed_emoji = failed_emoji
        self.max_chars = max_chars
        # An answer that quotes five URLs would otherwise bury the channel in
        # previews of pages nobody asked to see.
        self.unfurl_links = unfurl_links

    async def send(self, to: Destination, text: str) -> None:
        for part in chunks(to_mrkdwn(text), self.max_chars):
            payload = {
                "channel": to.chat_id,
                "text": part,
                "unfurl_links": self.unfurl_links,
                "unfurl_media": self.unfurl_links,
            }
            if to.thread_id:
                payload["thread_ts"] = to.thread_id
            await asyncio.to_thread(self._api.call, "chat.postMessage", payload)

    # -- progress -------------------------------------------------------
    async def on_turn_start(self, message: InboundMessage, to: Destination) -> None:
        await self._react("reactions.add", message, self.working_emoji)

    async def on_turn_end(self, message: InboundMessage, to: Destination, ok: bool) -> None:
        await self._react("reactions.remove", message, self.working_emoji)
        if not ok:
            # The host keeps the text of an answer it could not deliver, but a
            # turn that produced none leaves nothing at all in the channel. The
            # reaction is what stops the question looking ignored.
            await self._react("reactions.add", message, self.failed_emoji)

    async def _react(self, method: str, message: InboundMessage, emoji: str) -> None:
        # A sink is handed every turn's hooks, including turns whose message
        # arrived somewhere else entirely.
        if message.platform != self.platform or not message.message_id or not emoji:
            return
        payload = {"channel": message.chat_id, "timestamp": message.message_id, "name": emoji}
        try:
            await asyncio.to_thread(self._api.call, method, payload, tolerate=_REACTION_NOISE)
        except Exception as exc:
            # Progress decoration is never worth failing a turn over.
            log.debug("slack %s failed: %s", method, exc)


# -- text -----------------------------------------------------------------


def to_mrkdwn(text: str) -> str:
    """Markdown as Slack's mrkdwn, leaving code exactly as it was written."""
    out = []
    for is_code, chunk in _segments(text):
        escaped = _escape(chunk)
        out.append(escaped if is_code else _convert(escaped))
    return "".join(out)


def _segments(text: str) -> Iterator[tuple[bool, str]]:
    """Walk the text as alternating prose and code, fenced or inline."""
    position = 0
    for match in _CODE.finditer(text):
        if match.start() > position:
            yield False, text[position : match.start()]
        yield True, match.group(0)
        position = match.end()
    if position < len(text):
        yield False, text[position:]


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _convert(text: str) -> str:
    text = re.sub(r"(?m)^\s{0,3}#{1,6}\s+(.+?)\s*$", r"*\1*", text)  # no headings in Slack
    text = re.sub(r"\*\*(.+?)\*\*", r"*\1*", text, flags=re.DOTALL)
    text = re.sub(r"__(.+?)__", r"*\1*", text, flags=re.DOTALL)
    text = re.sub(r"~~(.+?)~~", r"~\1~", text, flags=re.DOTALL)
    text = re.sub(r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)", r"<\2|\1>", text)
    text = re.sub(r"(?m)^(\s*)[-*+]\s+", r"\1• ", text)
    # A blockquote means the same thing in both dialects, so give back the ">"
    # that escaping took away.
    text = re.sub(r"(?m)^(\s*)&gt;\s", r"\1> ", text)
    return text


def chunks(text: str, limit: int = MAX_CHARS) -> list[str]:
    """Split on blank lines, then lines, then characters — never mid-code-fence.

    A chunk that ends with a code fence still open has one added, and the next
    chunk starts with one, so neither message renders as half a block.
    """
    if len(text) <= limit:
        return [text] if text.strip() else []

    parts: list[str] = []
    current = ""
    for piece in _pieces(text, limit):
        if current and len(current) + len(piece) > limit:
            parts.append(current.rstrip())
            current = ""
        current += piece
    if current.strip():
        parts.append(current.rstrip())
    return _balance_fences(parts)


def _pieces(text: str, limit: int) -> Iterator[str]:
    # The separators are captured and yielded too, so reassembling the pieces
    # gives back the text exactly — a paragraph break that went missing in the
    # split would show up as a wall of lines in the channel.
    for paragraph in re.split(r"(\n{2,})", text):
        if not paragraph:
            continue
        if len(paragraph) <= limit:
            yield paragraph
            continue
        for line in paragraph.splitlines(keepends=True):
            if len(line) <= limit:
                yield line
                continue
            for start in range(0, len(line), limit):  # one unbroken wall of text
                yield line[start : start + limit]


def _balance_fences(parts: list[str]) -> list[str]:
    balanced = []
    carry = ""
    for part in parts:
        part = carry + part
        if part.count("```") % 2:
            part += "\n```"
            carry = "```\n"
        else:
            carry = ""
        balanced.append(part)
    return balanced
