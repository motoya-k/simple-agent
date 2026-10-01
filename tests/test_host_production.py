"""The host under production conditions: concurrency, shutdown, health."""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from simple_agent.config import Config
from simple_agent.host import AllowlistRouter, Host
from simple_agent.loop import Turn
from simple_agent.seams import InboundMessage


def message(sender, thread, n):
    return InboundMessage(platform="email", chat_id=sender, user_id=sender, text=f"m{n}",
                          thread_id=thread, message_id=f"<{n}>")


class ListSource:
    platform = "email"

    def __init__(self, messages, hold=False):
        self.items, self.acked, self.hold = messages, [], hold

    async def messages(self):
        for item in self.items:
            yield item
        if self.hold:  # keep listening, like a real inbox
            await asyncio.Event().wait()

    async def ack(self, message):
        self.acked.append(message.message_id)


class SlowAgent:
    """Records when each turn ran; sleeps so overlap is observable."""

    def __init__(self, log, delay):
        self.log, self.delay, self.interrupted = log, delay, threading.Event()

    def run(self, text):
        start = time.monotonic()
        while time.monotonic() - start < self.delay and not self.interrupted.is_set():
            time.sleep(0.01)
        self.log.append((text, start, time.monotonic()))
        return Turn(text="ok")

    def interrupt(self):
        self.interrupted.set()


@pytest.fixture
def config(tmp_path):
    return Config(home=tmp_path, max_concurrent_turns=4)


def make_host(config, source, delay=0.2):
    log = []
    host = Host(config, sources=[source], router=AllowlistRouter(allow=("@x",)),
                agent_factory=lambda cfg, **kw: SlowAgent(log, delay))
    return host, log


def test_conversations_run_in_parallel_but_each_one_in_order(config):
    source = ListSource([
        message("a@x", "<t1>", 1), message("b@x", "<t2>", 2), message("a@x", "<t1>", 3),
    ])
    host, log = make_host(config, source)

    started = time.monotonic()
    asyncio.run(host.serve())
    elapsed = time.monotonic() - started

    runs = {text: (start, end) for text, start, end in log}
    assert runs["m2"][0] < runs["m1"][1]   # other conversation overlapped
    assert runs["m3"][0] >= runs["m1"][1]  # same conversation waited its turn
    assert elapsed < 0.6                   # not three turns back to back
    assert sorted(source.acked) == ["<1>", "<2>", "<3>"]


def test_sigterm_finishes_turns_in_flight_and_takes_no_more(config):
    source = ListSource([message("a@x", "<t1>", 1)], hold=True)
    host, log = make_host(config, source, delay=0.3)

    async def main():
        serving = asyncio.create_task(host.serve())
        await asyncio.sleep(0.1)  # the turn is running
        host.stop()
        await serving

    asyncio.run(main())
    assert [text for text, *_ in log] == ["m1"]  # finished, not cut off
    assert source.acked == ["<1>"]


def test_turns_past_the_grace_period_are_interrupted_and_not_acked(config, monkeypatch):
    import simple_agent.host as host_module

    monkeypatch.setattr(host_module, "SHUTDOWN_GRACE", 0.1)
    source = ListSource([message("a@x", "<t1>", 1)], hold=True)
    host, log = make_host(config, source, delay=30)

    async def main():
        serving = asyncio.create_task(host.serve())
        await asyncio.sleep(0.1)
        host.stop()
        await serving

    started = time.monotonic()
    asyncio.run(main())
    assert time.monotonic() - started < 5  # interrupted, did not wait 30s
    assert log and log[0][0] == "m1"


def test_heartbeat_is_written_while_serving(config):
    host, _ = make_host(config, ListSource([]))
    asyncio.run(host.serve())
    assert float(host.heartbeat.read_text()) == pytest.approx(time.time(), abs=5)


def test_disabled_tools_are_gone_everywhere(tmp_path):
    from simple_agent.mcp import build_tool_registry

    registry = build_tool_registry(Config(home=tmp_path, disabled_tools="terminal, *_file"), None)
    assert "terminal" not in registry and "read_file" not in registry
    assert "memory_search" in registry


def test_imap_does_not_refetch_mail_still_in_flight(tmp_path):
    from simple_agent.mail import ImapSource, SqliteLedger

    raw = b"From: a@x\r\nMessage-ID: <dup@x>\r\n\r\nhi\r\n"

    class Client:
        uids = b"1"

        def __init__(self, *a): ...
        def login(self, *a): ...
        def select(self, *a, **k): ...
        def logout(self): ...
        def response(self, name): return ("OK", [b"7"])
        def uid(self, command, *args):
            if command == "SEARCH":
                return "OK", [Client.uids]
            return "OK", [(b"1 (BODY[] {10}", raw)]

    source = ImapSource(host="h", user="u", password="p", ledger=SqliteLedger(tmp_path / "l.db"),
                        connect=Client)
    assert source.poll() == []          # first sight: baseline, backlog skipped
    Client.uids = b"1 2 3"              # two new copies of the same message
    first = source.poll()
    assert len(first) == 1              # one request, not two
    assert source.poll() == []          # still in flight: not fetched again
    asyncio.run(source.ack(first[0]))
    assert source.poll() == []          # both copies recorded as handled


def test_health_follows_the_heartbeat(config, capsys):
    from simple_agent.cli import health

    assert health(config) == 1  # no heartbeat yet
    (config.home / "heartbeat").write_text(str(time.time()))
    assert health(config) == 0
    (config.home / "heartbeat").write_text(str(time.time() - 3600))
    assert health(config) == 1


def test_json_logs_are_one_object_per_line(monkeypatch, capsys):
    import json
    import logging

    from simple_agent.logs import configure

    monkeypatch.setenv("SIMPLE_AGENT_LOG_FORMAT", "json")
    configure()
    try:
        raise ValueError("boom")
    except ValueError:
        logging.getLogger("simple_agent.test").exception("turn failed for %s", "<1>")
    entry = json.loads(capsys.readouterr().err.strip().splitlines()[-1])
    assert entry["level"] == "ERROR" and entry["message"] == "turn failed for <1>"
    assert "ValueError: boom" in entry["exception"]
