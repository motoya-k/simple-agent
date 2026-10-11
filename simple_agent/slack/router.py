"""Which Slack conversations this host answers, and where the answer goes.

The counterpart of :class:`simple_agent.host.AllowlistRouter`, which reads
``user_id`` as an email address and so cannot speak about channels.

Allowing a channel is not a security decision — everyone in a workspace can
join a public channel and type.  It keeps a bot that was added to forty channels
from answering in all of them, and it keeps strangers from costing model calls.
What the agent may *do* is the profile's business, and the default here is the
``slack`` profile: read-only tools, no learning.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..profile import BUILT_IN, Profile
from ..seams import Destination, InboundMessage, Route, Router
from .parse import PLATFORM

#: Entries that mean "every direct message", whatever the channel id is.
DM_ENTRIES = frozenset({"dm", "dms", "im"})
EVERYTHING = "*"

#: A message that is only one of these means "do not post the answer you are
#: writing".  Whole messages, never substrings: "stop the deploy and tell me
#: what broke" is a question, not a cancellation.  Override with ``stop_words``
#: in the profile (``none`` turns it off).
STOP_WORDS = ("stop", "cancel", "abort", "やめて", "止めて", "中止", "キャンセル", "ストップ")

#: What ``stop_words`` is set to in order to answer regardless.
NO_STOP_WORDS = frozenset({"none", "off", "-"})

_TRAILING = " \t\r\n.!?。！？…、,"


@dataclass(frozen=True)
class SlackRouter(Router):
    """Answer in the conversations named in ``allow``.

    An entry is a channel id (``C0123``), a ``#channel-name``, a user id
    (``U0123``) to follow one person wherever they ask, ``dm`` for every direct
    message, or ``*`` for everything the app can see.

    ``thread_replies`` (default on) answers in a thread hanging off the question
    rather than in the channel.  It is also what keeps a conversation
    recoverable: a thread is one session, so a follow-up lands in the same
    transcript, while two loose channel messages are two turns of one
    per-speaker session with nothing tying them to what was answered.

    A message that is nothing but a stop word (see :data:`STOP_WORDS`) means
    "do not post what you are writing".  It is the one thing that cannot wait
    its turn — a thread is answered in order, so by the time it were routed
    normally the answer would already be in the channel — so the host asks
    about it as it arrives.  The work still finishes; only the answer is
    dropped.

    ``voice`` names the profile that words the answers (see
    :class:`~simple_agent.seams.Voice`).  ``work=False`` makes this the router
    for the conversations that get an answer but no agent: put it after the
    one that does the work, with a wider ``allow``, and whoever the first one
    turned away is told so by the voice instead of being met with silence.
    Such a router recognizes no stop words — a stop from somebody the agent
    does not work for must not drop an answer to somebody it does.

    ``ack=True`` has the voice post a line in the thread as the turn starts,
    saying what it takes the question to be, before the answer exists.
    """

    allow: tuple[str, ...] = ()
    profile: Profile = BUILT_IN["slack"]
    thread_replies: bool = True
    reply: bool = True
    voice: Profile | None = None
    work: bool = True
    ack: bool = False

    def route(self, message: InboundMessage) -> Route | None:
        if message.platform != PLATFORM or not self._allowed(message):
            return None
        if not self.reply:
            return Route(profile=self.profile, voice=self.voice, work=self.work, ack=self.ack)
        return Route(
            to=(Destination(PLATFORM, message.chat_id, self._thread(message)),),
            profile=self.profile,
            voice=self.voice,
            work=self.work,
            ack=self.ack,
        )

    def _thread(self, message: InboundMessage) -> str:
        """The thread to answer in, or "" for the channel itself.

        ``thread_replies=False`` means "do not start threads", not "ignore the
        one you are in": a question asked inside a thread is always answered
        there.  A root message is the one whose thread id is its own id — see
        :func:`simple_agent.slack.parse._thread_id`.
        """
        if self.thread_replies or message.thread_id != message.message_id:
            return message.thread_id
        return ""

    def stops(self, message: InboundMessage) -> bool:
        words = self.stop_words()
        if not self.work or not words or message.platform != PLATFORM or not self._allowed(message):
            return False
        return message.text.strip().strip(_TRAILING).casefold() in words

    def stop_words(self) -> frozenset[str]:
        """The profile's ``stop_words``, or the built-in list."""
        configured = self.profile.setting_list("stop_words")
        if configured and {w.casefold() for w in configured} & NO_STOP_WORDS:
            return frozenset()
        return frozenset(w.casefold() for w in (configured or STOP_WORDS))

    def _allowed(self, message: InboundMessage) -> bool:
        is_dm = message.chat_type in {"dm", "im"}
        for raw in self.allow:
            entry = raw.strip().lower()
            if not entry:
                continue
            if entry == EVERYTHING:
                return True
            if entry in DM_ENTRIES and is_dm:
                return True
            if entry.startswith("#"):
                if entry[1:] == message.chat_name.lower():
                    return True
                continue
            if entry in {message.chat_id.lower(), message.chat_name.lower(), message.user_id.lower()}:
                return True
        return False
