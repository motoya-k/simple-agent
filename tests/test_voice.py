"""The layer that talks: a small model words the answer, the agent does the work.

What these hold is mostly what the voice may *not* do. It may not cost an
answer (a failed rewrite sends the draft), it may not change a fact (a rewrite
that lost a number sends the draft), and a message nobody works on must reach
no agent at all — the voice answers it alone, or nobody does.
"""

from __future__ import annotations

import asyncio

import pytest

from simple_agent.config import Config
from simple_agent.host import FirstMatch, Host
from simple_agent.loop import Turn
from simple_agent.profile import Profile
from simple_agent.providers.base import Provider, Response
from simple_agent.seams import Destination, InboundMessage, Sink, Voice
from simple_agent.slack.router import SlackRouter
from simple_agent.voice import ProviderVoice, VoiceError, facts

PLATFORM = "slack"
TONE = Profile("voice", "Short and polite.")


def message(text="hello", *, user="U1", channel="C1", thread="T1"):
    return InboundMessage(
        platform=PLATFORM, chat_id=channel, user_id=user, text=text,
        chat_type="channel", thread_id=thread, message_id="1",
    )


class Recorder(Sink):
    platform = PLATFORM

    def __init__(self):
        self.sent: list[str] = []
        self.turns: list[bool] = []

    async def send(self, to: Destination, text: str) -> None:
        self.sent.append(text)

    async def on_turn_end(self, message, to, ok) -> None:
        self.turns.append(ok)


class FakeAgent:
    def __init__(self, answer="the draft"):
        self.answer = answer
        self.calls: list[str] = []

    def run(self, text):
        self.calls.append(text)
        return Turn(text=self.answer)

    def interrupt(self):
        return None


class FakeVoice(Voice):
    def __init__(self, fail=False):
        self.fail = fail
        self.heard: list[tuple[str, str, str | None]] = []

    def speak(self, message, profile, draft):
        self.heard.append((message.text, profile.name, draft))
        if self.fail:
            raise RuntimeError("the small model is down")
        return "sorry, not for you" if draft is None else f"voiced: {draft}"


class FakeProvider(Provider):
    name = "fake"

    def __init__(self, text, stop_reason="end_turn"):
        self.text, self.stop_reason = text, stop_reason
        self.requests: list[dict] = []

    def complete(self, **request):
        self.requests.append(request)
        return Response(text=self.text, stop_reason=self.stop_reason)


@pytest.fixture
def config(tmp_path):
    return Config(home=tmp_path)


def make_host(config, router, agent=None, voice=None):
    sink, agent = Recorder(), agent or FakeAgent()
    host = Host(
        config, sources=[], router=router, sinks=[sink],
        agent_factory=lambda cfg, **kw: agent, voice=voice,
    )
    return host, sink, agent


# -- the host's half ------------------------------------------------------
def test_the_voice_words_what_the_agent_wrote(config):
    voice = FakeVoice()
    host, sink, agent = make_host(config, SlackRouter(allow=("*",), voice=TONE), voice=voice)

    returned = asyncio.run(host.handle(message("how much?")))

    assert sink.sent == ["voiced: the draft"]
    assert voice.heard == [("how much?", "voice", "the draft")]
    # The transcript's side of it is the agent's own words.
    assert returned == "the draft"


def test_a_voice_that_fails_sends_the_draft(config):
    host, sink, _ = make_host(
        config, SlackRouter(allow=("*",), voice=TONE), voice=FakeVoice(fail=True)
    )

    asyncio.run(host.handle(message()))

    assert sink.sent == ["the draft"]
    assert not list(config.home.glob("dead_letters*"))  # delivered, not lost


def test_a_route_without_a_voice_is_untouched(config):
    voice = FakeVoice()
    host, sink, _ = make_host(config, SlackRouter(allow=("*",)), voice=voice)

    asyncio.run(host.handle(message()))

    assert sink.sent == ["the draft"] and voice.heard == []


def test_a_voice_profile_on_a_host_with_no_voice_changes_nothing(config):
    host, sink, _ = make_host(config, SlackRouter(allow=("*",), voice=TONE))

    asyncio.run(host.handle(message()))

    assert sink.sent == ["the draft"]


def test_a_message_nobody_works_on_is_answered_by_the_voice_alone(config):
    """The agent does not run for the person the first router turned away."""
    voice = FakeVoice()
    router = FirstMatch((
        SlackRouter(allow=("U-member",), voice=TONE),
        SlackRouter(allow=("C1",), voice=TONE, work=False),
    ))
    host, sink, agent = make_host(config, router, voice=voice)

    asyncio.run(host.handle(message("hi", user="U-stranger")))

    assert agent.calls == []
    assert sink.sent == ["sorry, not for you"]
    assert voice.heard == [("hi", "voice", None)]
    assert sink.turns == [True]

    asyncio.run(host.handle(message("the books", user="U-member")))
    assert agent.calls == ["the books"]
    assert sink.sent[-1] == "voiced: the draft"


def test_unworked_without_a_voice_is_dropped_like_no_route(config):
    host, sink, agent = make_host(config, SlackRouter(allow=("*",), voice=TONE, work=False))
    assert asyncio.run(host.handle(message())) is None

    host2, sink2, agent2 = make_host(
        config, SlackRouter(allow=("*",), work=False), voice=FakeVoice()
    )
    assert asyncio.run(host2.handle(message())) is None

    assert sink.sent == sink2.sent == [] and agent.calls == agent2.calls == []


def test_an_unworked_message_the_voice_cannot_answer_gets_nothing(config):
    host, sink, _ = make_host(
        config, SlackRouter(allow=("*",), voice=TONE, work=False), voice=FakeVoice(fail=True)
    )

    assert asyncio.run(host.handle(message())) is None
    assert sink.sent == [] and sink.turns == [False]


def test_a_stop_from_someone_nobody_works_for_stops_nothing():
    assert SlackRouter(allow=("*",)).stops(message("stop"))
    assert not SlackRouter(allow=("*",), voice=TONE, work=False).stops(message("stop"))


# -- the included voice ---------------------------------------------------
def test_the_profile_is_the_system_prompt_and_names_the_model(monkeypatch):
    monkeypatch.delenv("SIMPLE_AGENT_VOICE_MODEL", raising=False)
    provider = FakeProvider("Not here, sorry.")
    voice = ProviderVoice(provider, model="small")

    assert voice.speak(message("pay this"), TONE, None) == "Not here, sorry."
    request = provider.requests[0]
    assert request["system"] == "Short and polite."
    assert request["model"] == "small" and request["tools"] == []
    assert "pay this" in request["messages"][0]["content"]

    named = Profile("voice", "x", settings={"voice_model": "smaller"})
    voice.speak(message(), named, None)
    assert provider.requests[1]["model"] == "smaller"


def test_a_rewrite_that_loses_a_fact_is_refused():
    draft = "The total is 1,280,000 yen, due 2026-10-31. See https://example.com/inv/42 <@U123>"

    kept = ProviderVoice(
        FakeProvider("Due 2026-10-31: 1,280,000 yen <@U123>. https://example.com/inv/42"),
        model="m",
    )
    assert "1,280,000" in kept.speak(message(), TONE, draft)

    for damaged in (
        "About 1.3 million yen, due 2026-10-31. https://example.com/inv/42 <@U123>",
        "The total is 1,280,000 yen, due 2026-10-31. <@U123>",
        "The total is 1,280,000 yen, due 2026-10-31. See https://example.com/inv/42",
    ):
        with pytest.raises(VoiceError):
            ProviderVoice(FakeProvider(damaged), model="m").speak(message(), TONE, draft)


def test_a_number_inside_a_link_is_the_link():
    assert facts("see https://example.com/inv/42.") == {"https://example.com/inv/42"}
    assert facts("3 items, 1,200 yen each") == {"3", "1,200"}


def test_a_cut_off_or_empty_answer_is_refused():
    with pytest.raises(VoiceError):
        ProviderVoice(FakeProvider("half an ans", "max_tokens"), model="m").speak(
            message(), TONE, "a draft"
        )
    with pytest.raises(VoiceError):
        ProviderVoice(FakeProvider("  "), model="m").speak(message(), TONE, None)


def test_a_long_draft_is_never_sent_to_the_model():
    provider = FakeProvider("short")
    voice = ProviderVoice(provider, model="m", max_draft_chars=50)

    with pytest.raises(VoiceError):
        voice.speak(message(), TONE, "x" * 51)
    assert provider.requests == []
