"""Slack in, through the router, out again — no workspace, no network, no model."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import re
import struct

import pytest

from simple_agent.config import Config
from simple_agent.providers.base import Provider, Response
from simple_agent.slack import SlackRouter, SlackSink, SlackSource, parse_event, to_mrkdwn
from simple_agent.slack import ws
from simple_agent.slack.inbox import SqliteInbox
from simple_agent.slack.sink import chunks

BOT = "U_BOT"


# -- helpers --------------------------------------------------------------


def server_frame(opcode: int, payload: bytes = b"", fin: bool = True) -> bytes:
    """A frame as Slack sends them: never masked."""
    header = bytearray([(0x80 if fin else 0) | opcode])
    if len(payload) < 126:
        header.append(len(payload))
    elif len(payload) < 1 << 16:
        header.append(126)
        header += struct.pack("!H", len(payload))
    else:
        header.append(127)
        header += struct.pack("!Q", len(payload))
    return bytes(header) + payload


def client_frames(data: bytes) -> list[tuple[int, bytes]]:
    """Every frame the client sent, unmasked again."""
    frames, position = [], 0
    while position < len(data):
        first, second = data[position], data[position + 1]
        position += 2
        opcode, length = first & 0x0F, second & 0x7F
        if length == 126:
            (length,) = struct.unpack("!H", data[position : position + 2])
            position += 2
        elif length == 127:
            (length,) = struct.unpack("!Q", data[position : position + 8])
            position += 8
        assert second & 0x80, "a client frame must be masked"
        mask = data[position : position + 4]
        position += 4
        body = bytes(b ^ mask[i % 4] for i, b in enumerate(data[position : position + length]))
        position += length
        frames.append((opcode, body))
    return frames


class FakeSocket:
    """Enough socket for the handshake and a scripted stream of frames."""

    def __init__(self, frames: bytes = b"", *, accept: str | None = None, status: str = "101") -> None:
        self.frames = frames
        self.accept = accept  # None = compute the correct one
        self.status = status
        self.inbound = io.BytesIO(b"")
        self.sent = bytearray()
        self.closed = False

    def sendall(self, data: bytes) -> None:
        self.sent += data
        if data.startswith(b"GET "):
            match = re.search(rb"Sec-WebSocket-Key: (.+)\r\n", data)
            assert match
            key = match.group(1).decode()
            accept = self.accept or base64.b64encode(
                hashlib.sha1((key + ws.GUID).encode()).digest()
            ).decode()
            response = (
                f"HTTP/1.1 {self.status} Switching Protocols\r\n"
                "Upgrade: websocket\r\n"
                f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
            ).encode()
            self.inbound = io.BytesIO(response + self.frames)

    def makefile(self, mode: str = "rb"):
        return self.inbound

    def settimeout(self, value) -> None:
        self.timeout = value

    def close(self) -> None:
        self.closed = True


def open_socket(frames: bytes = b"", **kwargs) -> tuple[ws.WebSocket, FakeSocket]:
    sock = FakeSocket(frames, **kwargs)
    connection = ws.connect(
        "wss://slack.example/link?ticket=1",
        create_connection=lambda *a, **k: sock,
        ssl_context=_PassThroughTLS(),
    )
    return connection, sock


class _PassThroughTLS:
    def wrap_socket(self, sock, server_hostname=""):
        return sock


# -- the websocket --------------------------------------------------------


def test_a_text_message_arrives_and_a_reply_goes_back_masked():
    connection, sock = open_socket(server_frame(ws.TEXT, b'{"type":"hello"}'))

    assert connection.recv() == '{"type":"hello"}'
    connection.send('{"envelope_id":"e1"}')

    sent = client_frames(bytes(sock.sent[sock.sent.index(b"\r\n\r\n") + 4 :]))
    assert sent == [(ws.TEXT, b'{"envelope_id":"e1"}')]


def test_a_ping_is_answered_without_surfacing_as_a_message():
    frames = server_frame(ws.PING, b"beat") + server_frame(ws.TEXT, b"after")
    connection, sock = open_socket(frames)

    assert connection.recv() == "after"
    assert (ws.PONG, b"beat") in client_frames(bytes(sock.sent[sock.sent.index(b"\r\n\r\n") + 4 :]))


def test_a_fragmented_message_is_joined():
    frames = server_frame(ws.TEXT, b"one ", fin=False) + server_frame(ws.CONTINUATION, b"piece")
    connection, _ = open_socket(frames)

    assert connection.recv() == "one piece"


def test_a_close_frame_ends_the_stream():
    connection, _ = open_socket(server_frame(ws.CLOSE, struct.pack("!H", 1000)))

    assert connection.recv() is None


def test_a_frame_bigger_than_the_limit_is_refused():
    connection, _ = open_socket(server_frame(ws.TEXT, b"x" * 300))

    assert connection.recv() == "x" * 300  # a 16-bit length, still fine


def test_a_handshake_that_does_not_prove_itself_is_refused():
    """Something in the middle answering for Slack must not get frames."""
    with pytest.raises(ws.WebSocketError):
        open_socket(accept="not-the-right-key")

    with pytest.raises(ws.WebSocketError):
        open_socket(status="200")


def test_plain_ws_is_refused():
    with pytest.raises(ValueError):
        ws.connect("ws://slack.example/link")


# -- what counts as a request ---------------------------------------------


def envelope(event: dict, event_id: str = "Ev1") -> dict:
    return {
        "type": "events_api",
        "envelope_id": f"env-{event_id}",
        "payload": {"event_id": event_id, "event": event},
    }


def message_event(**overrides) -> dict:
    event = {
        "type": "message",
        "user": "U_ALICE",
        "channel": "C_OPS",
        "channel_type": "channel",
        "text": f"<@{BOT}> deploy status?",
        "ts": "1712345678.000100",
    }
    event.update(overrides)
    return event


def test_a_mention_in_a_channel_is_a_request():
    message = parse_event(envelope(message_event()), bot_user_id=BOT)

    assert message is not None
    assert message.platform == "slack"
    assert message.chat_id == "C_OPS"
    assert message.chat_type == "channel"
    assert message.user_id == "U_ALICE"
    assert message.text == "deploy status?"  # the mention is addressing, not content
    assert message.message_id == "1712345678.000100"


def test_channel_chatter_without_a_mention_is_not():
    assert parse_event(envelope(message_event(text="deploying now")), bot_user_id=BOT) is None


def test_a_direct_message_never_needs_a_mention():
    event = message_event(channel="D_ALICE", channel_type="im", text="morning")

    message = parse_event(envelope(event), bot_user_id=BOT)

    assert message is not None and message.chat_type == "dm" and message.text == "morning"


def test_the_agent_does_not_answer_itself():
    """Two agents in one channel is how a token budget disappears overnight."""
    assert parse_event(envelope(message_event(user=BOT)), bot_user_id=BOT) is None
    assert parse_event(envelope(message_event(bot_id="B1")), bot_user_id=BOT) is None


def test_app_mention_is_dropped_because_message_already_carries_it():
    event = message_event(type="app_mention")

    assert parse_event(envelope(event), bot_user_id=BOT) is None


def test_edits_and_joins_are_not_requests():
    for subtype in ("message_changed", "message_deleted", "channel_join"):
        event = message_event(subtype=subtype)
        assert parse_event(envelope(event), bot_user_id=BOT) is None


def test_a_thread_reply_keeps_the_thread_and_shares_one_session():
    """The question, its answer and every follow-up are one conversation."""
    first = parse_event(envelope(message_event()), bot_user_id=BOT)
    assert first is not None
    assert first.thread_id == first.message_id  # it is the root of its own thread
    reply = parse_event(
        envelope(
            message_event(
                user="U_BOB",
                text=f"<@{BOT}> and the rollback?",
                ts="1712345999.000200",
                thread_ts=first.message_id,
            ),
            event_id="Ev2",
        ),
        bot_user_id=BOT,
    )

    assert reply is not None
    assert reply.thread_id == first.message_id
    # Everyone in a thread is talking to one agent, even a second speaker.
    assert reply.session_key() == first.session_key()


def test_two_questions_in_a_channel_are_two_conversations():
    """Each thread is its own task; nobody's follow-up lands in someone else's."""
    one = parse_event(envelope(message_event()), bot_user_id=BOT)
    two = parse_event(
        envelope(message_event(user="U_BOB", ts="1712349999.000300"), event_id="Ev3"),
        bot_user_id=BOT,
    )

    assert one is not None and two is not None
    assert one.session_key() != two.session_key()


def test_one_dm_is_one_running_conversation():
    event = message_event(channel="D_ALICE", channel_type="im", text="morning")
    first = parse_event(envelope(event), bot_user_id=BOT)
    later = parse_event(
        envelope(message_event(channel="D_ALICE", channel_type="im", text="and now?",
                               ts="1712349999.000300"), event_id="Ev3"),
        bot_user_id=BOT,
    )

    assert first is not None and later is not None
    assert first.thread_id == ""  # the channel is already the conversation
    assert first.session_key() == later.session_key()


def test_slack_markup_becomes_prose_and_files_are_named():
    event = message_event(
        text=f"<@{BOT}> see <https://example.com|the report> in <#C_DEV|dev> &amp; tell <!here>",
        files=[{"name": "q3.pdf"}],
    )

    message = parse_event(envelope(event), bot_user_id=BOT)

    assert message is not None
    assert message.text == "see the report (https://example.com) in #dev & tell @here"
    assert message.attachments == ("q3.pdf",)


# -- the inbox ------------------------------------------------------------


def test_an_event_is_claimed_once_and_replayed_until_it_is_marked(tmp_path):
    inbox = SqliteInbox(tmp_path / "state.db")

    assert inbox.claim("T1/U_BOT", "Ev1", '{"raw":1}') is True
    assert inbox.claim("T1/U_BOT", "Ev1", '{"raw":1}') is False  # a re-delivery
    assert inbox.pending("T1/U_BOT") == [("Ev1", '{"raw":1}')]

    inbox.mark("T1/U_BOT", "Ev1")

    assert inbox.pending("T1/U_BOT") == []
    assert inbox.claim("T1/U_BOT", "Ev1", "{}") is False  # still remembered
    assert inbox.pending("T2/U_BOT") == []  # another workspace, another inbox


def test_pruning_forgets_finished_events_but_not_live_ones(tmp_path):
    inbox = SqliteInbox(tmp_path / "state.db")
    inbox.claim("app", "done", "{}")
    inbox.claim("app", "live", '{"raw":1}')
    inbox.mark("app", "done")

    inbox.prune("app", keep_seconds=-1)

    assert inbox.pending("app") == [("live", '{"raw":1}')]
    assert inbox.claim("app", "done", "{}") is True  # forgotten, claimable again


# -- the source -----------------------------------------------------------


class FakeApi:
    def __init__(self, **answers) -> None:
        self.calls: list[tuple] = []
        self.answers = answers

    def call(self, method, payload=None, *, form=False, tolerate=()):
        self.calls.append((method, payload))
        return self.answers.get(method, {"ok": True})


class FakeConnection:
    def __init__(self, frames: list[str]) -> None:
        self.frames = list(frames)
        self.sent: list[str] = []
        self.closed = False

    def recv(self):
        return self.frames.pop(0) if self.frames else None

    def send(self, text: str) -> None:
        self.sent.append(text)

    def close(self) -> None:
        self.closed = True


def make_source(tmp_path, frames, **kwargs):
    connection = FakeConnection(frames)
    source = SlackSource(
        app_token="xapp",
        bot_token="xoxb",
        inbox=SqliteInbox(tmp_path / "state.db"),
        api=FakeApi(**{"auth.test": {"ok": True, "user_id": BOT, "team_id": "T1"}}),
        app_api=FakeApi(**{"apps.connections.open": {"ok": True, "url": "wss://x/link"}}),
        connect=lambda url, **kw: connection,
        reconnect_seconds=0.01,
        **kwargs,
    )
    return source, connection


def collect(source, count: int, timeout: float = 5.0):
    async def run():
        found = []

        async def drain():
            async for message in source.messages():
                found.append(message)
                if len(found) >= count:
                    return

        await asyncio.wait_for(drain(), timeout)
        return found

    return asyncio.run(run())


def test_every_envelope_is_acknowledged_and_the_request_comes_through(tmp_path):
    frames = [
        json.dumps({"type": "hello"}),
        json.dumps(envelope(message_event())),
    ]
    source, connection = make_source(tmp_path, frames)

    found = collect(source, 1)

    assert [m.text for m in found] == ["deploy status?"]
    assert connection.sent == [json.dumps({"envelope_id": "env-Ev1"})]


def test_a_redelivered_event_is_acknowledged_but_not_answered_twice(tmp_path):
    first = json.dumps(envelope(message_event()))
    again = json.dumps(envelope(message_event()))  # same event_id after a reconnect
    second = json.dumps(envelope(message_event(text=f"<@{BOT}> and now?"), event_id="Ev2"))
    source, connection = make_source(tmp_path, [first, again, second])

    found = collect(source, 2)

    assert [m.text for m in found] == ["deploy status?", "and now?"]
    assert len(connection.sent) == 3  # all three acknowledged; only two answered


def test_an_event_that_is_not_a_request_is_finished_without_a_turn(tmp_path):
    chatter = json.dumps(envelope(message_event(text="no mention here")))
    wanted = json.dumps(envelope(message_event(), event_id="Ev2"))
    source, _ = make_source(tmp_path, [chatter, wanted])

    found = collect(source, 1)

    assert [m.message_id for m in found] == ["1712345678.000100"]
    # The chatter is finished with; only the request is still owed an answer.
    assert [event_id for event_id, _ in source.inbox.pending("T1/U_BOT")] == ["Ev2"]


def test_a_turn_interrupted_by_a_crash_is_replayed_next_start(tmp_path):
    frames = [json.dumps(envelope(message_event()))]
    source, _ = make_source(tmp_path, frames)
    [message] = collect(source, 1)  # received, never acked: the process "died"

    assert source.inbox.pending("T1/U_BOT")  # still owed an answer

    restarted, _ = make_source(tmp_path, [])  # nothing new on the socket
    [replayed] = collect(restarted, 1)

    assert replayed.text == message.text
    asyncio.run(restarted.ack(replayed))
    assert restarted.inbox.pending("T1/U_BOT") == []


def test_a_disconnect_request_is_not_an_error(tmp_path):
    frames = [
        json.dumps({"type": "disconnect", "reason": "refresh_requested"}),
        json.dumps(envelope(message_event())),  # never read: the connection ended
    ]
    source, connection = make_source(tmp_path, frames)

    # The next connection is the same scripted one, which now yields the event.
    found = collect(source, 1)

    assert found[0].text == "deploy status?"
    assert connection.closed


# -- the sink -------------------------------------------------------------


def test_markdown_becomes_mrkdwn_and_code_is_left_alone():
    text = (
        "## Status\n"
        "**two** deploys failed, see [the log](https://example.com/log)\n"
        "- first\n"
        "- second\n"
        "```\nif a < b: pass\n```\n"
        "`a & b` stays"
    )

    assert to_mrkdwn(text) == (
        "*Status*\n"
        "*two* deploys failed, see <https://example.com/log|the log>\n"
        "• first\n"
        "• second\n"
        "```\nif a &lt; b: pass\n```\n"
        "`a &amp; b` stays"
    )


def test_an_answer_too_long_for_one_message_is_split_without_breaking_code():
    text = "a" * 300 + "\n\n" + "```\n" + "b" * 300 + "\n```"

    parts = chunks(text, limit=400)

    assert len(parts) == 2
    assert parts[0].count("```") % 2 == 0 and parts[1].count("```") % 2 == 0
    assert "b" * 300 in parts[1]


def test_the_answer_goes_into_the_thread_it_was_asked_in():
    api = FakeApi()
    sink = SlackSink(api=api)
    from simple_agent.seams import Destination

    asyncio.run(sink.send(Destination("slack", "C_OPS", "1712345678.000100"), "done"))

    method, payload = api.calls[0]
    assert method == "chat.postMessage"
    assert payload["channel"] == "C_OPS"
    assert payload["thread_ts"] == "1712345678.000100"
    assert payload["text"] == "done"
    assert payload["unfurl_links"] is False


def test_progress_is_a_reaction_on_the_question_and_a_warning_if_it_failed():
    api = FakeApi()
    sink = SlackSink(api=api)
    message = parse_event(envelope(message_event()), bot_user_id=BOT)
    assert message is not None
    to = message.reply_to()

    asyncio.run(sink.on_turn_start(message, to))
    asyncio.run(sink.on_turn_end(message, to, ok=False))

    assert [(method, payload["name"]) for method, payload in api.calls] == [
        ("reactions.add", "eyes"),
        ("reactions.remove", "eyes"),
        ("reactions.add", "warning"),
    ]


def test_a_sink_ignores_turns_from_another_platform():
    """Every sink is handed every turn's hooks, including other platforms'."""
    from simple_agent.mail.parse import parse_message
    from simple_agent.seams import Destination

    api = FakeApi()
    sink = SlackSink(api=api)
    mail = parse_message(b"From: a@b.c\nMessage-ID: <x@y>\n\nhi\n")

    asyncio.run(sink.on_turn_start(mail, Destination("slack", "C_OPS")))

    assert api.calls == []


# -- the router -----------------------------------------------------------


def test_the_router_answers_where_it_was_asked_in_a_thread():
    message = parse_event(envelope(message_event()), bot_user_id=BOT)
    assert message is not None

    route = SlackRouter(allow=("C_OPS",)).route(message)

    assert route is not None
    assert route.to[0].chat_id == "C_OPS"
    # No thread yet, so the answer starts one on the question itself.
    assert route.to[0].thread_id == message.message_id

    # Told not to start threads, it answers in the channel instead...
    loose = SlackRouter(allow=("C_OPS",), thread_replies=False).route(message)
    assert loose is not None and loose.to[0].thread_id == ""
    assert route.profile.name == "slack"
    assert route.profile.tools == ("skill_view",) and route.profile.learning is False


def test_a_question_inside_a_thread_is_always_answered_there():
    """"Do not start threads" is not "ignore the thread you are in"."""
    reply = parse_event(
        envelope(
            message_event(ts="1712345999.000200", thread_ts="1712345678.000100"),
            event_id="Ev2",
        ),
        bot_user_id=BOT,
    )
    assert reply is not None

    route = SlackRouter(allow=("C_OPS",), thread_replies=False).route(reply)

    assert route is not None and route.to[0].thread_id == "1712345678.000100"


def test_a_channel_nobody_allowed_never_reaches_the_model():
    message = parse_event(envelope(message_event(channel="C_RANDOM")), bot_user_id=BOT)
    assert message is not None

    assert SlackRouter(allow=("C_OPS", "#ops")).route(message) is None
    assert SlackRouter(allow=("*",)).route(message) is not None


def test_dm_entry_covers_direct_messages_only():
    dm = parse_event(
        envelope(message_event(channel="D_ALICE", channel_type="im", text="hi")),
        bot_user_id=BOT,
    )
    channel = parse_event(envelope(message_event()), bot_user_id=BOT)
    router = SlackRouter(allow=("dm",))

    assert dm is not None and channel is not None
    assert router.route(dm) is not None
    assert router.route(channel) is None


# -- end to end, without a model ------------------------------------------


class Scripted(Provider):
    name = "scripted"

    def __init__(self, text="done"):
        self.text = text
        self.tools_seen = []

    def complete(self, *, system, messages, tools, max_tokens, model):
        self.tools_seen.append(sorted(tool["name"] for tool in tools))
        return Response(text=self.text, raw_content=[{"type": "text", "text": self.text}])


@pytest.fixture
def config(tmp_path):
    cfg = Config(home=tmp_path)
    for directory in (cfg.memories_dir, cfg.skills_dir, cfg.shell_state_dir):
        directory.mkdir(parents=True, exist_ok=True)
    return cfg


def test_a_mention_becomes_an_answer_posted_back_into_the_thread(config):
    from simple_agent.agent import Agent
    from simple_agent.host import Host

    provider = Scripted("two deploys failed")
    api = FakeApi()
    sink = SlackSink(api=api)
    message = parse_event(envelope(message_event()), bot_user_id=BOT)
    assert message is not None

    def factory(cfg, *, session_key, source, profile):
        return Agent(
            cfg, source=source, session_key=session_key, profile=profile, provider=provider
        )

    host = Host(
        config,
        sources=[],
        router=SlackRouter(allow=("C_OPS",)),
        sinks=[sink],
        agent_factory=factory,
    )

    assert asyncio.run(host.handle(message)) == "two deploys failed"
    assert provider.tools_seen == [["skill_view"]]  # the slack profile's ceiling
    posted = [payload for method, payload in api.calls if method == "chat.postMessage"]
    assert posted[0]["text"] == "two deploys failed"
    assert posted[0]["thread_ts"] == message.message_id


def test_splitting_keeps_the_paragraph_breaks():
    text = "\n\n".join(["para " + "x" * 100] * 6)

    parts = chunks(text, limit=300)

    assert len(parts) > 1
    assert "\n\n".join(parts) == text  # nothing lost in the seams
