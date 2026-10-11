"""Transport seams — where messages come from, where answers go, and which is which.

:mod:`simple_agent.host` runs these; :mod:`simple_agent.mail` is the one
Source included.  Adding a platform is a new file implementing an interface
below, never a change to ``agent.py`` or ``loop.py``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, AsyncIterator

from .profile import Profile
from .session import SessionSource, build_session_key


# Three pieces rather than one adapter per platform.  An adapter that both
# receives and sends quietly fixes the rule "answer where the message came
# from"; splitting them lets a host receive by email and answer in Slack, or
# receive and not answer at all, by configuration alone.
#
# There is no fourth box for side effects.  Something the host does on every
# message is a Sink plus a Route; something the agent decides to do based on
# what it read is a tool.


@dataclass(frozen=True)
class InboundMessage:
    """One message as a platform handed it over.

    Deliberately thin: the platform-specific work — deciding whether a message
    was addressed to the bot, pulling text out of rich blocks, downloading
    attachments — belongs in the Source, not in a shared struct.
    """

    platform: str
    chat_id: str
    user_id: str
    text: str
    chat_type: str = "dm"
    user_name: str = ""
    chat_name: str = ""
    thread_id: str = ""
    message_id: str = ""
    attachments: tuple[str, ...] = ()

    def source(self) -> SessionSource:
        return SessionSource(
            platform=self.platform,
            chat_id=self.chat_id,
            chat_type=self.chat_type,
            user_id=self.user_id,
            user_name=self.user_name,
            chat_name=self.chat_name,
            thread_id=self.thread_id,
            message_id=self.message_id,
        )

    def session_key(self, **rules: Any) -> str:
        """The identity of this conversation.

        Delegates to :func:`simple_agent.session.build_session_key` rather than
        assembling a key here. Two places that both build keys eventually build
        two *different* keys, and the symptom is a thread that answers itself
        twice or loses its history halfway through.
        """
        return build_session_key(self.source(), **rules)

    def reply_to(self) -> "Destination":
        """The place this message came from, as somewhere to answer."""
        return Destination(self.platform, self.chat_id, self.thread_id)


@dataclass(frozen=True)
class Destination:
    """Somewhere an answer can be delivered: a chat, optionally a thread in it."""

    platform: str
    chat_id: str
    thread_id: str = ""


class Source(ABC):
    """Receives messages from one platform. It never sends anything.

    ``messages`` covers both kinds of transport: a polling source (IMAP) loops
    and sleeps inside it, a push source (a websocket) yields as events arrive.

    ``ack`` is called once the host has finished with a message, successfully
    or not.  A source that can be asked for the same message twice — an inbox
    polled after a restart — records it here, so a crash mid-turn replays the
    message instead of losing it.
    """

    platform: str

    @abstractmethod
    def messages(self) -> AsyncIterator[InboundMessage]: ...

    async def ack(self, message: InboundMessage) -> None:
        return None

    async def close(self) -> None:
        return None


class Sink(ABC):
    """Delivers answers to one platform. It never receives anything.

    A Sink implements everything its platform can do; whether a given message
    is answered, and where, is the Router's decision.  "Email does not reply"
    is a routing policy, not a missing capability.
    """

    platform: str

    @abstractmethod
    async def send(self, to: Destination, text: str) -> None: ...

    async def close(self) -> None:
        return None

    # -- progress, without filling the channel with it ------------------
    # A tool-by-tool commentary that reads fine in a terminal reads as spam in
    # a shared channel, where every line is permanent and everyone sees it.
    # These exist so a sink can show that work is happening on a surface that
    # is not the message stream: a reaction on the triggering message, a
    # status line beside the bot's name, a typing indicator. The defaults do
    # nothing, which is right for a platform with no such surface — and for a
    # message that arrived on a different platform than this sink's.

    async def on_turn_start(self, message: InboundMessage, to: Destination) -> None:
        return None

    async def on_turn_end(self, message: InboundMessage, to: Destination, ok: bool) -> None:
        return None


@dataclass(frozen=True)
class Route:
    """What the host does with one inbound message.

    ``to`` empty means receive and answer nobody — the agent still runs, and
    anything it should do in the world it does through tools.

    ``profile`` is who the agent is on this route: its instructions, its
    toolset, whether it may write long-term memory, and whose memory it reads.
    ``None`` takes the host config's profile.  See :mod:`simple_agent.profile`.

    ``voice`` is who words the answer: a profile whose instructions are about
    how to speak, not what to do.  With one, the agent's text is a draft and
    the host's :class:`Voice` rewrites it before it is delivered.  ``None``
    sends what the agent wrote, as written.

    ``work=False`` means nobody works on this message — no agent, no tools, no
    transcript — and the voice answers it alone.  It is how a host says "not
    here" or "not for you" instead of saying nothing.  It needs a ``voice``;
    without one the message is dropped, as if no route had taken it.

    ``ack=True`` has the voice say, the moment the turn starts, that the
    message was taken and what it is taken to be asking — while the agent is
    still starting.  It needs a ``voice`` and somewhere to say it (``to``).
    """

    to: tuple[Destination, ...] = ()
    profile: Profile | None = None
    voice: Profile | None = None
    work: bool = True
    ack: bool = False


class Router(ABC):
    """Decides, per message, whether to run the agent and where the answer goes.

    Returning ``None`` drops the message before the agent sees it — the place
    for a sender allowlist, so an unknown address never costs a model call.
    """

    @abstractmethod
    def route(self, message: InboundMessage) -> Route | None: ...

    def stops(self, message: InboundMessage) -> bool:
        """True when this message means "do not send the answer being written".

        Asked the moment the message arrives, before the conversation's turn
        lock — which is the whole point.  Messages in one conversation are
        answered in order, so by the time this one were routed normally, the
        answer it meant to stop would already be in the channel.

        It stops the *answer*, not the work: the turn runs to the end and its
        result is simply not delivered.  The default recognizes nothing, which
        is right for a route that does not answer anyone.
        """
        return False


class Voice(ABC):
    """Words what goes out. It never works on anything.

    The agent that does the work and the layer that talks are two jobs.  One
    needs the big model, the tools and the long instructions about what it may
    touch; the other needs to be quick, and to know how this place likes to be
    spoken to.  Keeping them in one prompt means the rules of tone compete with
    the rules of work for the same attention, and a message nobody will work on
    gets no answer at all, because the only thing that can speak is the thing
    that was not started.

    ``speak`` is called from a worker thread, like ``Agent.run``.  It has no
    tools and keeps no history: it sees one message and, when there is one, the
    draft written in answer to it.

    * ``draft`` is ``None`` — nobody worked on the message (``Route.work`` is
      false).  Say so, in the way ``profile`` describes.
    * ``draft`` is text — the agent's answer.  Return what should be posted
      instead.

    Raising is how a voice says "send the draft as it is": the host delivers
    the agent's own words rather than lose an answer that cost a turn.

    ``acknowledge`` is the other thing a quick model is for: the line that goes
    out the moment a turn starts (``Route.ack``), so the person knows they were
    heard and what the agent takes the request to be, minutes before the answer
    exists.  It is written without knowing what the agent will find, or even
    what it is able to do — so it says what was understood, never what will be
    delivered.  Returning ``""`` (the default) says nothing, and so does
    raising: an acknowledgement is a courtesy, and nothing waits for it.
    """

    @abstractmethod
    def speak(self, message: InboundMessage, profile: Profile, draft: str | None) -> str: ...

    def acknowledge(self, message: InboundMessage, profile: Profile) -> str:
        return ""
