"""A Socket Mode envelope -> :class:`~simple_agent.seams.InboundMessage`, or ``None``.

A pure function, kept apart from the connection so the part most likely to be
wrong — *was this message for us at all* — is tested against recorded
envelopes without a workspace.

``None`` means "not a request", and most envelopes are not one.  Slack delivers
every message in every channel the app is in, including its own.

Three traps live here, each one its own ``None``:

* **A mention arrives twice.**  Slack sends ``app_mention`` *and*
  ``message`` for the same words, so handling both answers everything twice.
  ``message`` is the one with every field this agent needs, so ``app_mention``
  is dropped on the floor — see :data:`HANDLED_EVENTS`.
* **The agent hears itself.**  Its own replies come back as events.  Without
  ``bot_id`` and bot-user checks, two agents in one channel talk until the
  budget runs out.
* **Edits and joins are not messages.**  ``message_changed``,
  ``channel_join`` and friends arrive as ``message`` with a subtype.

Conversation identity: a DM is one session (``chat_id`` is the DM channel), a
thread is one session shared by everyone in it, and a channel message outside
a thread is one session per speaker.  That is
:func:`simple_agent.session.build_session_key`'s doing, not this module's; here
it is only a matter of filling in ``chat_type`` and ``thread_id`` honestly.
"""

from __future__ import annotations

import re
from typing import Any

from ..seams import InboundMessage

PLATFORM = "slack"

#: ``app_mention`` is deliberately absent; see the module docstring.
HANDLED_EVENTS = frozenset({"message"})

#: A ``message`` with any other subtype is an edit, a deletion, a join, or
#: another piece of channel furniture — not somebody asking for something.
TEXT_SUBTYPES = frozenset({"", "file_share", "thread_broadcast"})

#: Slack's ``channel_type``, in this repo's vocabulary.  ``mpim`` (a group DM)
#: is a group: more than one person can speak into it, which is what the
#: session rules and the memory review care about.
CHAT_TYPES = {"im": "dm", "mpim": "group", "group": "group", "channel": "channel"}

#: ``app_mention`` and a few message shapes omit ``channel_type``; the channel
#: id's first letter is the fallback.  D is a DM, G a private channel or group
#: DM, C a channel.
ID_CHAT_TYPES = {"D": "dm", "G": "group", "C": "channel"}

_USER_MENTION = re.compile(r"<@([^|>\s]+)(?:\|[^>]*)?>")
_CHANNEL_LINK = re.compile(r"<#([^|>\s]+)(?:\|([^>]*))?>")
_LABELLED_LINK = re.compile(r"<(https?://[^|>]+)\|([^>]+)>")
_BARE_LINK = re.compile(r"<(https?://[^|>]+)>")
_SPECIAL = re.compile(r"<!(here|channel|everyone)(?:\|[^>]*)?>")


def parse_event(
    envelope: dict[str, Any],
    *,
    bot_user_id: str = "",
    require_mention: bool = True,
) -> InboundMessage | None:
    """One ``events_api`` envelope as a message for the agent, or ``None``.

    ``require_mention`` applies to channels and group DMs only: a bot in a
    busy channel that answers every line is a bot somebody removes the same
    afternoon.  A direct message is always for the agent — there is nobody
    else in it.
    """
    if envelope.get("type") != "events_api":
        return None
    payload = envelope.get("payload") or {}
    event = payload.get("event") or {}
    if event.get("type") not in HANDLED_EVENTS:
        return None
    if str(event.get("subtype", "")) not in TEXT_SUBTYPES:
        return None
    if event.get("bot_id") or event.get("bot_profile"):
        return None  # another app, or this one hearing its own reply

    user_id = str(event.get("user", ""))
    if not user_id or (bot_user_id and user_id == bot_user_id):
        return None

    channel = str(event.get("channel", ""))
    chat_type = CHAT_TYPES.get(
        str(event.get("channel_type", "")), ID_CHAT_TYPES.get(channel[:1], "channel")
    )

    text = str(event.get("text", ""))
    mentioned = bool(bot_user_id) and f"<@{bot_user_id}>" in text
    if chat_type != "dm" and require_mention and not mentioned:
        return None

    files = event.get("files") or []
    attachments = tuple(str(f.get("name") or f.get("title") or "unnamed") for f in files)

    return InboundMessage(
        platform=PLATFORM,
        chat_id=channel,
        user_id=user_id,
        text=clean_text(text, bot_user_id=bot_user_id),
        chat_type=chat_type,
        chat_name=str(event.get("channel_name", "")),
        # ``ts`` rather than the envelope's ``event_id``: it is the id of the
        # message *in the channel*, which is what a Sink needs to answer in its
        # thread or put a reaction on it.  Replay and de-duplication key on the
        # event id instead, in the inbox.
        message_id=str(event.get("ts", "")),
        thread_id=_thread_id(event, chat_type),
        attachments=attachments,
    )


def _thread_id(event: dict[str, Any], chat_type: str) -> str:
    """Which conversation this message belongs to.

    Slack gives ``thread_ts`` only to replies, but a channel question that gets
    answered *in a thread* is the first message of that thread — and the answer
    is what creates it.  So outside a DM the root's own ``ts`` is its thread id,
    and the question, the answer and every follow-up land in one session.

    Without this, the second message in a thread opens a transcript that has
    never seen the question it is a follow-up to.  (A DM needs none of it: the
    channel is already the conversation.)
    """
    thread = str(event.get("thread_ts", ""))
    if thread:
        return thread
    return "" if chat_type == "dm" else str(event.get("ts", ""))


def clean_text(text: str, *, bot_user_id: str = "") -> str:
    """Slack's wire format as something a model reads as prose.

    The bot's own mention goes (it is addressing, not content), links keep both
    label and URL, and the three HTML entities Slack escapes come back.  Other
    people's mentions stay as raw ids: an id is at least unambiguous, and
    resolving each one costs an API call per message.
    """
    if bot_user_id:
        text = re.sub(rf"<@{re.escape(bot_user_id)}(?:\|[^>]*)?>", "", text)
    text = _LABELLED_LINK.sub(lambda m: f"{m.group(2)} ({m.group(1)})", text)
    text = _BARE_LINK.sub(lambda m: m.group(1), text)
    text = _CHANNEL_LINK.sub(lambda m: f"#{m.group(2) or m.group(1)}", text)
    text = _SPECIAL.sub(lambda m: f"@{m.group(1)}", text)
    text = _USER_MENTION.sub(lambda m: f"@{m.group(1)}", text)
    text = text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
    return text.strip()
