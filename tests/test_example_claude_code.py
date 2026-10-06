"""The ``examples/claude-code-host`` wiring: Claude Code as the engine.

An example that no longer runs is worse than no example, and the parts worth
pinning are the ones a reader would not notice breaking: the translation from a
profile to Claude Code's allowlist (where the security boundary ends up), the
transcript replay (the engine keeps nothing), and the composition itself.

The transports are the library's — see ``tests/test_slack.py``,
``tests/test_cron.py``, ``tests/test_mail.py``.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "claude-code-host"
sys.path.insert(0, str(EXAMPLE))

import claude_code  # noqa: E402
from claude_code import ClaudeCodeAgent, allowed_tools, write_mcp_config  # noqa: E402
from simple_agent.config import Config  # noqa: E402
from simple_agent.profile import Profile, load_profile  # noqa: E402
from simple_agent.session import SessionSource  # noqa: E402
from simple_agent.state import open_store  # noqa: E402

SERVERS = ("simple_agent", "hindsight", "notion", "slack", "google")


@pytest.fixture
def config(tmp_path):
    return Config(home=tmp_path)


# -- profile -> allowlist ----------------------------------------------
def test_a_pattern_becomes_a_server_and_a_name_becomes_a_tool():
    profile = Profile(
        name="slack",
        instructions="",
        tools=("notion__*", "hindsight__recall", "skill_view"),
    )
    assert allowed_tools(profile, SERVERS) == (
        "mcp__notion",
        "mcp__hindsight__recall",
        "mcp__simple_agent__skill_view",
    )


def test_every_tool_means_every_declared_server():
    # Not "no allowlist": with --permission-prompts none that denies everything.
    profile = Profile(name="slack", instructions="", tools=None)
    assert allowed_tools(profile, SERVERS) == tuple(f"mcp__{name}" for name in SERVERS)


def test_a_pattern_claude_code_cannot_express_is_refused():
    profile = Profile(name="slack", instructions="", tools=("notion__*_search",))
    with pytest.raises(ValueError, match="neither"):
        allowed_tools(profile, SERVERS)


@pytest.mark.parametrize("tool", ["hindsight__retain", "hindsight__*", "skill_manage"])
def test_learning_off_refuses_a_tool_that_teaches(tool):
    profile = Profile(name="email", instructions="", tools=(tool,), learning=False)
    with pytest.raises(ValueError, match="learning: false"):
        allowed_tools(profile, SERVERS)


def test_learning_off_keeps_the_read_tools():
    profile = Profile(
        name="email",
        instructions="",
        tools=("hindsight__recall", "skill_view", "session_search"),
        learning=False,
    )
    assert allowed_tools(profile, SERVERS) == (
        "mcp__hindsight__recall",
        "mcp__simple_agent__skill_view",
        "mcp__simple_agent__session_search",
    )


def test_the_shipped_profiles_are_consistent(config, tmp_path):
    """The two profile files in the example translate, and mail cannot teach."""
    for name in ("slack", "email"):
        (tmp_path / "profiles" / f"{name}.md").parent.mkdir(exist_ok=True)
        (tmp_path / "profiles" / f"{name}.md").write_text(
            (EXAMPLE / "profiles" / f"{name}.md").read_text("utf-8"), encoding="utf-8"
        )
    allowed_tools(load_profile(config, "slack"), SERVERS)  # raises if it drifted
    mail = allowed_tools(load_profile(config, "email"), SERVERS)
    assert not any(claude_code._teaches(name) for name in mail)


# -- the MCP config ----------------------------------------------------
def test_the_namespace_lands_in_the_hindsight_url(tmp_path):
    template = tmp_path / "mcp.json"
    template.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "hindsight": {"type": "http", "url": "${HINDSIGHT_URL}/mcp/${namespace}/"},
                    "notion": {"command": "x", "env": {"NOTION_TOKEN": "${NOTION_TOKEN}"}},
                }
            }
        )
    )
    path, servers = write_mcp_config(
        template,
        tmp_path / "out" / "mcp-support.json",
        namespace="support",
        env={"HINDSIGHT_URL": "https://h.example", "NOTION_TOKEN": "secret"},
    )
    rendered = json.loads(path.read_text())
    assert rendered["mcpServers"]["hindsight"]["url"] == "https://h.example/mcp/support/"
    assert rendered["mcpServers"]["notion"]["env"]["NOTION_TOKEN"] == "secret"
    assert servers == ("hindsight", "notion")
    # It holds every token this deployment has.
    assert oct(path.stat().st_mode)[-3:] == "600"


def test_a_missing_variable_is_named_at_startup(tmp_path):
    template = tmp_path / "mcp.json"
    template.write_text('{"mcpServers": {"notion": {"env": {"T": "${NOTION_TOKEN}"}}}}')
    with pytest.raises(ValueError, match="NOTION_TOKEN"):
        write_mcp_config(template, tmp_path / "out.json", namespace="ops", env={})


# -- one turn ----------------------------------------------------------
RESULT = {"type": "result", "subtype": "success", "is_error": False, "result": "the answer",
          "num_turns": 2, "usage": {"input_tokens": 10, "output_tokens": 5}}


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    """A ``claude`` that records how it was called and answers from FAKE_RESULT."""
    binary = tmp_path / "fake-claude"
    binary.write_text(
        '#!/bin/sh\n'
        'cat > "$FAKE_PROMPT"\n'
        'for arg in "$@"; do printf "%s\\037" "$arg" >> "$FAKE_ARGV"; done\n'
        'printf "%s" "$FAKE_RESULT"\n'
        'exit ${FAKE_EXIT:-0}\n'
    )
    binary.chmod(0o755)
    monkeypatch.setenv("FAKE_PROMPT", str(tmp_path / "prompt.txt"))
    monkeypatch.setenv("FAKE_ARGV", str(tmp_path / "argv.txt"))
    monkeypatch.setenv("FAKE_RESULT", json.dumps(RESULT))
    return binary


def build_agent(config, binary, profile=None, **kwargs):
    return ClaudeCodeAgent(
        config,
        session_key="slack:C1:t1",
        source=SessionSource(platform="slack", chat_id="C1", chat_type="channel"),
        profile=profile or Profile(name="slack", instructions="You are the team's assistant."),
        mcp_config=Path(config.home) / "mcp.json",
        servers=SERVERS,
        store=open_store(config),
        binary=str(binary),
        **kwargs,
    )


def test_a_turn_is_stored_and_replayed(config, fake_claude):
    agent = build_agent(config, fake_claude)
    turn = agent.run("what is the on-call rota?")
    assert turn.text == "the answer"
    assert turn.tokens == 15 and turn.iterations == 2
    assert Path(os.environ["FAKE_PROMPT"]).read_text() == "what is the on-call rota?"

    # Second turn: the engine keeps nothing, so the history comes back out of
    # the store and goes in with the new message.
    agent.run("and who is on it next week?")
    prompt = Path(os.environ["FAKE_PROMPT"]).read_text()
    assert "what is the on-call rota?" in prompt
    assert "assistant: the answer" in prompt
    assert prompt.rstrip().endswith("and who is on it next week?")

    # Four messages, in one conversation, resumable by key.
    stored = agent.store.conversation(agent.session_id)
    assert [row["role"] for row in stored] == ["user", "assistant", "user", "assistant"]
    assert agent.store.latest_session_for_key("slack:C1:t1") == agent.session_id


def test_the_conversation_is_picked_back_up_by_key(config, fake_claude):
    first = build_agent(config, fake_claude)
    first.run("hello")
    second = build_agent(config, fake_claude)  # a new process, same thread
    assert second.session_id == first.session_id


def test_the_command_carries_the_profile_and_keeps_no_session(config, fake_claude):
    agent = build_agent(
        config, fake_claude, profile=Profile(name="slack", instructions="Be brief.",
                                             tools=("hindsight__recall",))
    )
    agent.run("hi")
    argv = Path(os.environ["FAKE_ARGV"]).read_text().split("\x1f")  # a prompt has newlines in it
    assert "--no-session-persistence" in argv
    assert "--strict-mcp-config" in argv
    assert argv[argv.index("--allowedTools") + 1] == "mcp__hindsight__recall"
    assert argv[argv.index("--tools") + 1] == ""  # no shell, no file edits
    assert argv[argv.index("--permission-prompts") + 1] == "none"
    assert "Be brief." in argv[argv.index("--system-prompt") + 1]
    assert "slack" in argv[argv.index("--system-prompt") + 1]  # where it is


def test_a_failed_engine_raises_and_keeps_the_question(config, fake_claude, monkeypatch):
    monkeypatch.setenv("FAKE_RESULT", json.dumps({"is_error": True, "result": "overloaded"}))
    monkeypatch.setenv("FAKE_EXIT", "1")
    agent = build_agent(config, fake_claude)
    with pytest.raises(RuntimeError, match="overloaded"):
        agent.run("what broke?")
    assert [row["role"] for row in agent.store.conversation(agent.session_id)] == ["user"]


# -- the composition ---------------------------------------------------
def test_a_schedule_alone_is_a_host(config, tmp_path, monkeypatch):
    """The transports are the library's; only the engine is this example's."""
    import host as example_host  # the wiring; imports simple_agent, not the reverse

    for name in ("slack", "email"):
        (tmp_path / "profiles").mkdir(exist_ok=True)
        (tmp_path / "profiles" / f"{name}.md").write_text(
            (EXAMPLE / "profiles" / f"{name}.md").read_text("utf-8"), encoding="utf-8"
        )
    (tmp_path / "schedules").mkdir(exist_ok=True)
    (tmp_path / "schedules" / "digest.md").write_text(
        "---\nschedule: 0 9 * * 1-5\nto: slack:C0PS\nprofile: email\n---\nWhat broke yesterday?\n",
        encoding="utf-8",
    )
    # No Slack tokens and no mailbox password: nothing to listen to but the clock.
    for name in ("SIMPLE_AGENT_SLACK_APP_TOKEN", "SIMPLE_AGENT_SLACK_BOT_TOKEN",
                 "SIMPLE_AGENT_IMAP_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SIMPLE_AGENT_MCP_TEMPLATE", str(tmp_path / "mcp.json"))
    (tmp_path / "mcp.json").write_text(
        '{"mcpServers": {"hindsight": {"type": "http", "url": "http://h/mcp/${namespace}/"}}}',
        encoding="utf-8",
    )

    host = example_host.build_host(config)
    assert [type(source).__name__ for source in host.sources] == ["CronSource"]

    # And a firing is answered by claude -p, under the job's profile.
    from simple_agent.cron import CronRouter, load_jobs

    (job,) = load_jobs(config)
    message = CronSource_message(job)
    route = CronRouter((job,)).route(message)
    agent = host._factory(config, session_key=message.session_key(), source=message.source(),
                          profile=route.profile)
    assert isinstance(agent, ClaudeCodeAgent)
    assert "--no-session-persistence" in agent.argv
    # The job named the email profile: read-only, and it may not teach.
    assert agent.argv[agent.argv.index("--allowedTools") + 1] == (
        "mcp__hindsight__recall,mcp__hindsight__reflect,"
        "mcp__simple_agent__skill_view,mcp__simple_agent__session_search"
    )


def CronSource_message(job):
    from datetime import datetime, timezone

    from simple_agent.cron import CronSource, MemoryCursor

    return CronSource([job], cursor=MemoryCursor())._message(
        job, datetime(2026, 10, 5, 9, 0, tzinfo=timezone.utc)
    )


def test_mail_answers_in_slack_and_a_stranger_is_dropped(config, tmp_path):
    """Mail has no SMTP sink on purpose; its answer goes where a person reads."""
    from simple_agent.host import AllowlistRouter
    from simple_agent.mail.parse import PLATFORM as EMAIL
    from simple_agent.seams import Destination, InboundMessage

    router = AllowlistRouter(
        allow=("@example.com",),
        profile=Profile(name="email", instructions="", learning=False),
        to=(Destination("slack", "C0PS"),),
    )
    mail = InboundMessage(platform=EMAIL, chat_id="a@example.com", user_id="a@example.com",
                          text="please look at this")
    route = router.route(mail)
    assert route.profile.learning is False
    assert [(d.platform, d.chat_id) for d in route.to] == [("slack", "C0PS")]
    assert router.route(InboundMessage(platform=EMAIL, chat_id="x@other.test",
                                       user_id="x@other.test", text="hi")) is None
