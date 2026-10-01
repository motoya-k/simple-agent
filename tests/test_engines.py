"""Engine seam — the pi engine against a fake ``pi`` that prints JSON events."""

from __future__ import annotations

import json
import sys
import textwrap

import pytest

from simple_agent.agent import Agent
from simple_agent.config import Config
from simple_agent.engines import LoopEngine
from simple_agent.engines.pi import PiEngine
from simple_agent.providers.base import Provider, Response

FAKE_PI = textwrap.dedent(
    """
    import json, sys
    with open(sys.argv[1], "w") as f:
        json.dump(sys.argv[2:], f)
    events = [
        {"type": "session", "id": "s"},
        {"type": "tool_execution_start", "toolName": "bash", "toolCallId": "1", "args": {}},
        {"type": "turn_end"},
        {"type": "message_end", "message": {"role": "assistant",
            "content": [{"type": "text", "text": "listed"}], "usage": {"input": 7, "output": 3}}},
        {"type": "turn_end"},
    ]
    for event in events:
        print(json.dumps(event))
    """
)


class Unused(Provider):
    name = "unused"

    def complete(self, **kwargs):  # pragma: no cover - must never be called
        raise AssertionError("the pi engine must not call the provider")


@pytest.fixture
def config(tmp_path):
    cfg = Config(home=tmp_path, learning=False)
    for directory in (cfg.memories_dir, cfg.skills_dir, cfg.shell_state_dir):
        directory.mkdir(parents=True, exist_ok=True)
    return cfg


@pytest.fixture
def fake_pi(tmp_path):
    script = tmp_path / "fake_pi.py"
    script.write_text(FAKE_PI)
    argv_file = tmp_path / "argv.json"
    engine = PiEngine(command=f"{sys.executable} {script} {argv_file}", args="--provider x")
    return engine, argv_file


def test_pi_answers_and_the_transcript_stays_ours(config, fake_pi):
    engine, argv_file = fake_pi
    events = []
    agent = Agent(config, engine=engine, provider=Unused())

    turn = agent.run("list files", on_event=lambda kind, text: events.append((kind, text)))

    assert turn.text == "listed"
    assert (turn.tool_calls, turn.iterations, turn.tokens) == (1, 2, 10)
    assert events == [("tool", "bash")]
    assert agent.messages[-1] == {"role": "assistant", "content": [{"type": "text", "text": "listed"}]}
    # resumable from disk like any other conversation
    assert Agent(config, engine=engine, provider=Unused()).messages[-1]["content"][0]["text"] == "listed"

    argv = json.loads(argv_file.read_text())
    assert argv[:3] == ["-p", "--mode", "json"]
    tools = argv[argv.index("--tools") + 1].split(",")
    assert tools[:4] == ["bash", "edit", "read", "write"]  # pi's own
    assert {"memory_save", "memory_search", "skill_view", "session_search"} <= set(tools[4:])  # via the bridge
    assert argv[argv.index("-e") + 1].endswith("pi_bridge.ts")
    assert "--append-system-prompt" in argv
    # pi_args set --provider, so ours is not added; the model still is
    assert argv.count("--provider") == 1 and argv[argv.index("--model") + 1] == config.model
    assert argv[-1].endswith("list files")
    session = argv[argv.index("--session") + 1]
    assert session.startswith(str(config.home / "pi"))


def test_a_narrowed_toolset_is_never_widened_under_pi(config, fake_pi):
    engine, argv_file = fake_pi
    Agent(config, engine=engine, provider=Unused(), tools=[]).run("hi")
    argv = json.loads(argv_file.read_text())
    assert "--no-tools" in argv and "--tools" not in argv and "-e" not in argv

    agent = Agent(config, engine=engine, provider=Unused(), tools=["read_file", "skill_view"], resume=False)
    agent.run("hi")
    argv = json.loads(argv_file.read_text())
    assert argv[argv.index("--tools") + 1] == "read,skill_view"
    server = json.loads(engine.bridge_env(agent)["SIMPLE_AGENT_MCP_COMMAND"])
    assert server[server.index("--tools") + 1] == "skill_view"  # the bridge is narrowed too


def test_the_configured_llm_reaches_pi_without_repeating_it(tmp_path):
    config = Config(home=tmp_path, provider="bedrock", learning=False)
    agent = Agent(config, engine=PiEngine(), provider=Unused())

    argv = PiEngine().build_command(agent, "hi")

    assert argv[argv.index("--provider") + 1] == "amazon-bedrock"
    assert argv[argv.index("--model") + 1] == "jp.anthropic.claude-sonnet-4-6"


def test_an_impossible_combination_fails_at_start(tmp_path):
    config = Config(home=tmp_path, provider="ollama", model="m", review_model="m")
    with pytest.raises(RuntimeError, match="cannot run provider 'ollama'"):
        Agent(config, engine=PiEngine(), provider=Unused())


def test_pi_failure_raises_but_the_user_message_is_kept(config, tmp_path):
    script = tmp_path / "fail.py"
    script.write_text("import sys; print('boom', file=sys.stderr); sys.exit(3)")
    agent = Agent(config, engine=PiEngine(command=f"{sys.executable} {script}"), provider=Unused())

    with pytest.raises(RuntimeError, match="pi exited with 3: boom"):
        agent.run("hi")
    assert agent.messages[-1]["role"] == "user"


def test_loop_is_the_default_engine(config):
    class Echo(Provider):
        name = "echo"

        def complete(self, **kwargs):
            return Response(text="ok", raw_content=[{"type": "text", "text": "ok"}])

    agent = Agent(config, provider=Echo())
    assert isinstance(agent.engine, LoopEngine)
    assert agent.run("hi").text == "ok"


# -- Claude Code -------------------------------------------------------------

FAKE_CLAUDE = textwrap.dedent(
    """
    import json, sys
    with open(sys.argv[1], "w") as f:
        json.dump(sys.argv[2:], f)
    events = [
        {"type": "system", "subtype": "init"},
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "mcp__simple-agent__memory_save", "input": {}}]}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "saved"}]}},
        {"type": "result", "subtype": "success", "is_error": False, "result": "saved",
         "usage": {"input_tokens": 5, "output_tokens": 2}},
    ]
    for event in events:
        print(json.dumps(event))
    """
)


@pytest.fixture
def fake_claude(tmp_path):
    from simple_agent.engines.claude_code import ClaudeCodeEngine

    script = tmp_path / "fake_claude.py"
    script.write_text(FAKE_CLAUDE)
    argv_file = tmp_path / "claude_argv.json"
    return ClaudeCodeEngine(command=f"{sys.executable} {script} {argv_file}"), argv_file


def test_claude_code_gets_our_tools_over_mcp_and_resumes(tmp_path, fake_claude):
    engine, argv_file = fake_claude
    config = Config(home=tmp_path, provider="bedrock", learning=False)
    agent = Agent(config, engine=engine, provider=Unused(), tools=["read_file", "memory_save"])

    turn = agent.run("remember this")
    first = json.loads(argv_file.read_text())

    assert turn.text == "saved" and turn.tool_calls == 1 and turn.tokens == 7
    assert first[first.index("--tools") + 1] == "Glob,Grep,Read"
    allowed = first[first.index("--allowedTools") + 1].split(",")
    assert allowed == ["Glob", "Grep", "Read", "mcp__simple-agent__memory_save"]
    mcp = json.loads(first[first.index("--mcp-config") + 1])["mcpServers"]["simple-agent"]
    assert mcp["args"][mcp["args"].index("--tools") + 1] == "memory_save"  # narrowed too
    assert first[first.index("--permission-mode") + 1] == "dontAsk"
    assert first[first.index("--setting-sources") + 1] == ""
    assert first[first.index("--model") + 1] == "jp.anthropic.claude-sonnet-4-6"
    assert engine.environment(agent)["CLAUDE_CODE_USE_BEDROCK"] == "1"
    session = first[first.index("--session-id") + 1]

    agent.run("again")
    second = json.loads(argv_file.read_text())
    assert "--session-id" not in second and second[second.index("--resume") + 1] == session


def test_claude_code_with_no_tools_disables_every_builtin(tmp_path, fake_claude):
    engine, argv_file = fake_claude
    Agent(Config(home=tmp_path, learning=False), engine=engine, provider=Unused(), tools=[]).run("hi")
    argv = json.loads(argv_file.read_text())
    assert argv[argv.index("--tools") + 1] == ""
    assert "--allowedTools" not in argv and "--mcp-config" not in argv


def test_claude_code_rejects_providers_it_cannot_run(tmp_path):
    from simple_agent.engines.claude_code import ClaudeCodeEngine

    config = Config(home=tmp_path, provider="gemini", learning=False)
    with pytest.raises(RuntimeError, match="cannot run provider 'gemini'"):
        Agent(config, engine=ClaudeCodeEngine(), provider=Unused())


# -- Goose -------------------------------------------------------------------

FAKE_GOOSE = textwrap.dedent(
    """
    import json, sys
    with open(sys.argv[1], "w") as f:
        json.dump(sys.argv[2:], f)
    print("    __( O)>  banner lines are not JSON")
    def msg(role, *content):
        return {"type": "message", "message": {"role": role, "content": list(content)}}
    events = [
        msg("assistant", {"type": "text", "text": "let me check"}),
        msg("assistant", {"type": "toolRequest",
            "toolCall": {"status": "success", "value": {"name": "simple-agent__memory_search"}}}),
        msg("user", {"type": "toolResponse"}),
        msg("assistant", {"type": "text", "text": "Launch is "}),
        msg("assistant", {"type": "text", "text": "Nov 3."}),
        {"type": "complete", "total_tokens": 42},
    ]
    for event in events:
        print(json.dumps(event))
    """
)


@pytest.fixture
def fake_goose(tmp_path):
    from simple_agent.engines.goose import GooseEngine

    script = tmp_path / "fake_goose.py"
    script.write_text(FAKE_GOOSE)
    argv_file = tmp_path / "goose_argv.json"
    return GooseEngine(command=f"{sys.executable} {script} {argv_file}"), argv_file


def test_goose_joins_the_streamed_answer_and_resumes_without_re_adding(tmp_path, fake_goose):
    engine, argv_file = fake_goose
    agent = Agent(Config(home=tmp_path, provider="bedrock", learning=False), engine=engine, provider=Unused())

    turn = agent.run("when is launch?")
    first = json.loads(argv_file.read_text())

    assert turn.text == "Launch is Nov 3."  # only the text after the last tool result
    assert (turn.tool_calls, turn.tokens) == (1, 42)
    assert first[first.index("--provider") + 1] == "aws_bedrock"
    assert first[first.index("--with-builtin") + 1] == "developer"  # full toolset allowed
    extension = first[first.index("--with-extension") + 1]
    assert extension.startswith("simple-agent:") and "--session-key" not in extension
    assert "--resume" not in first

    agent.run("again")
    second = json.loads(argv_file.read_text())
    assert "--resume" in second and second[second.index("--name") + 1] == first[first.index("--name") + 1]
    assert "--with-extension" not in second and "--with-builtin" not in second  # restored by goose


def test_goose_without_the_full_toolset_bridges_our_file_tools_instead(tmp_path, fake_goose):
    engine, argv_file = fake_goose
    Agent(Config(home=tmp_path, provider="bedrock", learning=False), engine=engine,
          provider=Unused(), tools=["read_file", "skill_view"]).run("hi")
    argv = json.loads(argv_file.read_text())
    assert "--with-builtin" not in argv  # no shell sneaks in with the developer bundle
    extension = argv[argv.index("--with-extension") + 1]
    assert "--tools read_file,skill_view" in extension


# -- OpenCode ----------------------------------------------------------------

FAKE_OPENCODE = textwrap.dedent(
    """
    import json, os, sys
    with open(sys.argv[1], "w") as f:
        json.dump({"argv": sys.argv[2:], "config": json.loads(os.environ["OPENCODE_CONFIG_CONTENT"])}, f)
    def ev(kind, **part):
        return {"type": kind, "sessionID": "ses_abc", "part": part}
    for event in [
        ev("step_start"), ev("text", text="checking"), ev("tool_use", tool="simple-agent_memory_search"),
        ev("step_finish", tokens={"input": 3, "output": 1}),
        ev("step_start"), ev("text", text="PM standup "), ev("text", text="is 9:30."),
        ev("step_finish", tokens={"input": 4, "output": 2}),
    ]:
        print(json.dumps(event))
    """
)


@pytest.fixture
def fake_opencode(tmp_path):
    from simple_agent.engines.opencode import OpenCodeEngine

    script = tmp_path / "fake_opencode.py"
    script.write_text(FAKE_OPENCODE)
    record = tmp_path / "opencode_run.json"
    return OpenCodeEngine(command=f"{sys.executable} {script} {record}"), record


def test_opencode_denies_everything_then_allows_our_list(tmp_path, fake_opencode):
    engine, record = fake_opencode
    config = Config(home=tmp_path, provider="bedrock", learning=False)
    agent = Agent(config, engine=engine, provider=Unused(), tools=["read_file", "memory_search"])

    turn = agent.run("when is standup?")
    first = json.loads(record.read_text())

    assert turn.text == "PM standup is 9:30." and turn.tool_calls == 1 and turn.tokens == 10
    tools = first["config"]["tools"]
    assert tools == {"*": False, "glob": True, "grep": True, "list": True, "read": True,
                     "simple-agent_*": True}
    server = first["config"]["mcp"]["simple-agent"]["command"]
    assert server[server.index("--tools") + 1] == "memory_search"
    assert open(first["config"]["instructions"][0]).read() == agent.system
    argv = first["argv"]
    assert argv[argv.index("--model") + 1] == "amazon-bedrock/jp.anthropic.claude-sonnet-4-6"
    assert "--session" not in argv

    agent.run("again")
    second = json.loads(record.read_text())["argv"]
    assert second[second.index("--session") + 1] == "ses_abc"


def test_opencode_with_no_tools_gets_no_mcp_server(tmp_path, fake_opencode):
    engine, record = fake_opencode
    Agent(Config(home=tmp_path, learning=False), engine=engine, provider=Unused(), tools=[]).run("hi")
    config = json.loads(record.read_text())["config"]
    assert config["tools"] == {"*": False} and "mcp" not in config
