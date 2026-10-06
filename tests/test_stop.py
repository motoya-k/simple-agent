"""Saying stop mid-turn: the answer is dropped, the work is not interrupted.

One conversation is answered in order, so "stop" would normally be read only
*after* the answer it meant to stop had already been posted. These tests hold
the one exception to that order — the question is asked as the message
arrives, outside the turn lock — and hold its limits: a stop that arrives when
nothing is running must not swallow the next answer, and it must not leak into
the conversation's following turn.
"""

from __future__ import annotations

import asyncio
import threading

import pytest

from simple_agent.config import Config
from simple_agent.host import DeadLetters, Host
from simple_agent.loop import Turn
from simple_agent.profile import Profile
from simple_agent.seams import Destination, InboundMessage, Sink
from simple_agent.slack.router import NO_STOP_WORDS, STOP_WORDS, SlackRouter

PLATFORM = "slack"


def message(text, *, thread="T1", channel="C1", user="U1", mid="1"):
    return InboundMessage(
        platform=PLATFORM, chat_id=channel, user_id=user, text=text,
        chat_type="channel", thread_id=thread, message_id=mid,
    )


class Recorder(Sink):
    platform = PLATFORM

    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    async def send(self, to: Destination, text: str) -> None:
        self.sent.append((to.thread_id, text))


class FakeAgent:
    """Answers `text`, and blocks on the first turn until released."""

    def __init__(self, release: threading.Event | None = None):
        self.release = release
        self.started = threading.Event()
        self.calls: list[str] = []

    def run(self, text):
        self.calls.append(text)
        self.started.set()
        if self.release is not None and len(self.calls) == 1:
            self.release.wait(5)
        return Turn(text=f"answer to {text!r}")

    def interrupt(self):
        return None


@pytest.fixture
def config(tmp_path):
    return Config(home=tmp_path)


def make_host(config, agent, router=None, **kwargs):
    sink = Recorder()
    host = Host(
        config,
        sources=[],
        router=router or SlackRouter(allow=("*",)),
        sinks=[sink],
        agent_factory=lambda cfg, **kw: agent,
        **kwargs,
    )
    return host, sink


# -- the router's half ----------------------------------------------------
def test_only_a_whole_message_is_a_stop_word():
    router = SlackRouter(allow=("*",))

    assert router.stops(message("やめて"))
    assert router.stops(message("Stop"))
    assert router.stops(message("STOP!"))
    assert router.stops(message("  cancel  "))
    assert router.stops(message("ストップ。"))
    # A question that merely contains one is a question.
    assert not router.stops(message("stop the deploy and tell me what broke"))
    assert not router.stops(message("まとめて"))


def test_a_stop_word_from_a_conversation_we_do_not_answer_is_not_one():
    router = SlackRouter(allow=("C9",))
    assert not router.stops(message("stop", channel="C1"))
    assert not router.stops(
        InboundMessage(platform="email", chat_id="a@b", user_id="a@b", text="stop")
    )


def test_the_profile_can_replace_or_disable_the_words():
    custom = SlackRouter(
        allow=("*",),
        profile=Profile("slack", "x", settings={"stop_words": "ちょっと待って, halt"}),
    )
    assert custom.stops(message("halt")) and custom.stops(message("ちょっと待って"))
    assert not custom.stops(message("stop"))  # replaced, not added to

    off = SlackRouter(
        allow=("*",), profile=Profile("slack", "x", settings={"stop_words": "none"})
    )
    assert off.stop_words() == frozenset()
    assert not off.stops(message("stop"))
    assert NO_STOP_WORDS and STOP_WORDS  # the defaults are still there for everyone else


def test_a_router_that_says_nothing_about_stopping_stops_nothing(config):
    """The email host is unchanged: it answers nobody, so there is nothing to drop."""
    from simple_agent.host import AllowlistRouter

    assert AllowlistRouter(allow=("@x",)).stops(
        InboundMessage(platform="email", chat_id="a@x", user_id="a@x", text="stop")
    ) is False


def test_bundling_routers_keeps_the_stop_words(config):
    """`--slack --cron` wraps the routers; the wrapper must not swallow this."""
    from simple_agent.host import FirstMatch

    class Deaf(SlackRouter):
        pass  # stands in for a router about another platform

    bundle = FirstMatch(routers=(Deaf(allow=()), SlackRouter(allow=("*",))))

    assert bundle.stops(message("やめて"))
    assert not bundle.stops(message("summarize the doc"))


# -- the host's half ------------------------------------------------------
def test_the_answer_is_dropped_but_the_turn_still_ran(config):
    agent = FakeAgent()
    host, sink = make_host(config, agent)
    msg = message("summarize the doc")
    host._running.add(msg.session_key())
    host._note_stop(message("やめて", mid="2"))

    asyncio.run(host._process(_NoSource(), msg))

    assert sink.sent == []  # nothing reached the channel
    assert agent.calls == ["summarize the doc"]  # but the work was done
    assert host._stops == set()  # and the signal was spent, not left behind


def test_a_stop_does_not_leak_into_the_next_turn(config):
    agent = FakeAgent()
    host, sink = make_host(config, agent)
    first, second = message("question one", mid="1"), message("question two", mid="2")
    host._running.add(first.session_key())
    host._note_stop(message("stop", mid="s"))

    asyncio.run(host._process(_NoSource(), first))
    asyncio.run(host._process(_NoSource(), second))

    assert sink.sent == [("T1", "answer to 'question two'")]


def test_a_stop_with_nothing_running_is_remembered_by_nobody(config):
    """Otherwise it would sit there and swallow whatever is asked next."""
    agent = FakeAgent()
    host, sink = make_host(config, agent)
    host._note_stop(message("stop", mid="s"))  # no turn in flight

    assert host._stops == set()
    asyncio.run(host._process(_NoSource(), message("question", mid="1")))
    assert sink.sent == [("T1", "answer to 'question'")]


def test_a_stop_in_another_thread_leaves_this_one_alone(config):
    agent = FakeAgent()
    host, sink = make_host(config, agent)
    mine = message("my question", thread="T1")
    theirs = message("stop", thread="T2", mid="s")
    host._running.add(mine.session_key())
    host._running.add(theirs.session_key())
    host._note_stop(theirs)

    asyncio.run(host._process(_NoSource(), mine))

    assert sink.sent == [("T1", "answer to 'my question'")]


def test_a_dropped_answer_is_not_a_dead_letter(config):
    """Dead letters are answers that could not be sent. This one was not wanted."""
    dead = DeadLetters(config.home / "dead.jsonl")
    agent = FakeAgent()
    host, sink = make_host(config, agent, dead_letters=dead)
    msg = message("question")
    host._running.add(msg.session_key())
    host._note_stop(message("stop", mid="s"))

    asyncio.run(host._process(_NoSource(), msg))

    assert sink.sent == []
    assert not dead.path.exists()


# -- end to end, with the stop genuinely arriving mid-turn ----------------
def test_a_stop_arriving_while_the_turn_runs_beats_the_answer_to_the_channel(config):
    """The real ordering: the stop is seen in _drain, not after the lock."""
    release = threading.Event()
    agent = FakeAgent(release)

    class Controlled:
        platform = PLATFORM

        def __init__(self):
            self.acked: list[str] = []

        async def messages(self):
            yield message("summarize the doc", mid="1")
            while not agent.started.is_set():  # the turn is now in flight
                await asyncio.sleep(0.01)
            yield message("やめて", mid="2")
            release.set()

        async def ack(self, msg):
            self.acked.append(msg.message_id)

    source = Controlled()
    host, sink = make_host(config, agent)
    host.sources = [source]

    asyncio.run(host.serve())

    # The first answer never reached the channel; the stop's own turn answered.
    assert sink.sent == [("T1", "answer to 'やめて'")]
    assert agent.calls == ["summarize the doc", "やめて"]  # both turns ran in full
    assert source.acked == ["1", "2"]  # and both messages are done with


class _NoSource:
    platform = PLATFORM

    async def ack(self, message):
        return None
