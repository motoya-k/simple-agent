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
    assert argv[argv.index("--tools") + 1] == "bash,edit,read,write"
    assert "--append-system-prompt" in argv and "--provider" in argv
    assert argv[-1].endswith("list files")
    session = argv[argv.index("--session") + 1]
    assert session.startswith(str(config.home / "pi"))


def test_a_narrowed_toolset_is_never_widened_under_pi(config, fake_pi):
    engine, argv_file = fake_pi
    Agent(config, engine=engine, provider=Unused(), tools=["skill_view"]).run("hi")
    argv = json.loads(argv_file.read_text())
    assert "--no-tools" in argv and "--tools" not in argv

    Agent(config, engine=engine, provider=Unused(), tools=["read_file", "memory"], resume=False).run("hi")
    argv = json.loads(argv_file.read_text())
    assert argv[argv.index("--tools") + 1] == "read"


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
