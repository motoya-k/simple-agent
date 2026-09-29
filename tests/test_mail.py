"""Email in, through the router, into an agent — no network, no model."""

from __future__ import annotations

import asyncio
import json

import pytest

from simple_agent.config import Config
from simple_agent.host import AllowlistRouter, DeadLetters, Host
from simple_agent.mail.parse import parse_message, strip_quoted
from simple_agent.providers.base import Provider, Response
from simple_agent.seams import Destination, InboundMessage, Sink

FIRST = b"""From: Alice Example <Alice@Example.com>
To: agent@example.com
Subject: =?utf-8?b?6KuL5rGC5pu4?=
Message-ID: <root@example.com>
Content-Type: text/plain; charset=utf-8

Please file this invoice.
"""

REPLY = b"""From: alice@example.com
To: agent@example.com
Subject: Re: invoice
Message-ID: <second@example.com>
In-Reply-To: <first-reply@example.com>
References: <root@example.com> <first-reply@example.com>
Content-Type: text/plain; charset=utf-8

Also the receipt.

On Mon, 29 Sep 2026 at 10:00, Agent <agent@example.com> wrote:
> Filed.
"""

HTML_WITH_ATTACHMENT = b"""From: bob@corp.example
Subject: report
Message-ID: <html@corp.example>
MIME-Version: 1.0
Content-Type: multipart/mixed; boundary="b"

--b
Content-Type: text/html; charset=utf-8

<p>Numbers<br>attached</p>
--b
Content-Type: application/pdf
Content-Disposition: attachment; filename="q3.pdf"

JVBERi0=
--b--
"""


# -- parsing --------------------------------------------------------------


def test_first_message_starts_its_own_thread():
    message = parse_message(FIRST)

    assert message.platform == "email"
    assert message.user_id == "alice@example.com"  # lower-cased
    assert message.user_name == "Alice Example"
    assert message.thread_id == message.message_id == "<root@example.com>"
    assert "Subject: 請求書" in message.text  # RFC 2047 decoded
    assert message.text.endswith("Please file this invoice.")


def test_a_reply_joins_the_thread_of_its_first_message():
    first, reply = parse_message(FIRST), parse_message(REPLY)

    assert reply.thread_id == "<root@example.com>"
    assert reply.session_key() == first.session_key()
    assert "Filed." not in reply.text  # the quote is already in the transcript
    assert reply.text.endswith("Also the receipt.")


def test_the_same_thread_from_another_sender_is_another_conversation():
    other = parse_message(REPLY.replace(b"From: alice@", b"From: mallory@"))
    assert other.session_key() != parse_message(REPLY).session_key()


def test_html_body_and_attachment_names():
    message = parse_message(HTML_WITH_ATTACHMENT)

    assert "Numbers\nattached" in message.text
    assert message.attachments == ("q3.pdf",)
    assert "Attachments (not downloaded): q3.pdf" in message.text


def test_strip_quoted_keeps_text_above_the_attribution():
    assert strip_quoted("new\n\nOn Tue, Bob wrote:\nold\n> older") == "new"


# -- routing --------------------------------------------------------------


def inbound(sender: str, text: str = "hi", message_id: str = "<m@x>") -> InboundMessage:
    return InboundMessage(
        platform="email", chat_id=sender, user_id=sender, text=text,
        thread_id=message_id, message_id=message_id,
    )


def test_allowlist_matches_addresses_and_domains_only():
    router = AllowlistRouter(allow=("alice@example.com", "@corp.example"))

    assert router.route(inbound("Alice@Example.com")) is not None
    assert router.route(inbound("bob@corp.example")) is not None
    assert router.route(inbound("eve@evilcorp.example")) is None
    assert router.route(inbound("alice@example.com.evil")) is None


# -- the host -------------------------------------------------------------


class Scripted(Provider):
    name = "scripted"

    def __init__(self, text="done", fail=False):
        self.text, self.fail, self.tools_seen = text, fail, []

    def complete(self, *, system, messages, tools, max_tokens, model):
        self.tools_seen.append(sorted(t["name"] for t in tools))
        if self.fail:
            raise RuntimeError("model down")
        return Response(text=self.text, raw_content=[{"type": "text", "text": self.text}])


class BrokenSink(Sink):
    platform = "slack"

    async def send(self, to, text):
        raise ConnectionError("slack is down")


@pytest.fixture
def config(tmp_path):
    cfg = Config(home=tmp_path)
    for directory in (cfg.memories_dir, cfg.skills_dir, cfg.shell_state_dir):
        directory.mkdir(parents=True, exist_ok=True)
    return cfg


def make_host(config, provider, router, **kwargs):
    from simple_agent.agent import Agent

    built = []

    def factory(cfg, *, session_key, source, tools):
        agent = Agent(cfg, source=source, session_key=session_key, tools=tools, provider=provider)
        built.append(agent)
        return agent

    host = Host(config, sources=[], router=router, agent_factory=factory, **kwargs)
    return host, built


def test_untrusted_route_gets_narrow_tools_and_does_not_learn(config):
    provider = Scripted()
    host, built = make_host(config, provider, AllowlistRouter(allow=("@example.com",)))

    text = asyncio.run(host.handle(parse_message(FIRST)))

    assert text == "done"
    assert provider.tools_seen == [["skill_view"]]  # no terminal, files, or memory
    assert built[0].config.learning is False
    assert config.learning is True  # the host's own config is untouched


def test_unknown_sender_never_reaches_the_model(config):
    provider = Scripted()
    host, built = make_host(config, provider, AllowlistRouter(allow=("@corp.example",)))

    assert asyncio.run(host.handle(parse_message(FIRST))) is None
    assert provider.tools_seen == [] and built == []


def test_undeliverable_answer_goes_to_dead_letters(config):
    to = Destination("slack", "C1")
    router = AllowlistRouter(allow=("@example.com",), to=(to,))
    dead = DeadLetters(config.home / "dead.jsonl")
    host, _ = make_host(config, Scripted("the answer"), router, sinks=[BrokenSink()], dead_letters=dead)

    asyncio.run(host.handle(parse_message(FIRST)))

    record = json.loads(dead.path.read_text().splitlines()[0])
    assert record["text"] == "the answer"
    assert record["to"]["chat_id"] == "C1"
    assert "slack is down" in record["error"]


def test_a_failed_turn_is_still_acknowledged(config):
    class OneShot:
        platform = "email"

        def __init__(self):
            self.acked = []

        async def messages(self):
            yield parse_message(FIRST)

        async def ack(self, message):
            self.acked.append(message.message_id)

    source = OneShot()
    host, _ = make_host(config, Scripted(fail=True), AllowlistRouter(allow=("@example.com",)))
    host.sources = [source]

    asyncio.run(host.serve())

    assert source.acked == ["<root@example.com>"]
