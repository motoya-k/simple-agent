"""Smoke tests — no network. A fake provider stands in for the LLM."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from simple_agent.config import Config  # noqa: E402
from simple_agent.loop import Budget, run_conversation  # noqa: E402
from simple_agent.memory import MemoryStore  # noqa: E402
from simple_agent.providers.base import Provider, Response, ToolCall  # noqa: E402
from simple_agent.skills import SkillLibrary  # noqa: E402
from simple_agent.state import Store  # noqa: E402
from simple_agent.tools import ToolRegistry, build_registry  # noqa: E402


class ScriptedProvider(Provider):
    """Replays a list of prepared responses, one per model call."""

    name = "scripted"

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def complete(self, *, system, messages, tools, max_tokens, model):
        self.calls.append({"system": system, "messages": list(messages)})
        return self.responses.pop(0)


@pytest.fixture
def config(tmp_path):
    cfg = Config(home=tmp_path)
    for directory in (cfg.memories_dir, cfg.skills_dir, cfg.shell_state_dir):
        directory.mkdir(parents=True, exist_ok=True)
    return cfg


@pytest.fixture
def registry(config):
    return build_registry(
        config,
        MemoryStore(config.memories_dir),
        SkillLibrary(config.skills_dir),
        Store(config.state_db),
    )


def test_loop_returns_answer_when_no_tools_requested(registry):
    provider = ScriptedProvider([Response(text="done", raw_content=[{"type": "text", "text": "done"}])])
    turn = run_conversation(
        provider=provider, model="m", system="s", messages=[{"role": "user", "content": "hi"}],
        registry=registry,
    )
    assert turn.text == "done"
    assert turn.tool_calls == 0


def test_loop_runs_tools_then_answers(registry, tmp_path):
    target = tmp_path / "hello.txt"
    provider = ScriptedProvider(
        [
            Response(
                tool_calls=[ToolCall("t1", "write_file", {"path": str(target), "content": "hi"})],
                raw_content=[{"type": "tool_use", "id": "t1", "name": "write_file", "input": {}}],
            ),
            Response(text="wrote it", raw_content=[{"type": "text", "text": "wrote it"}]),
        ]
    )
    messages = [{"role": "user", "content": "write a file"}]
    turn = run_conversation(
        provider=provider, model="m", system="s", messages=messages, registry=registry
    )
    assert turn.text == "wrote it"
    assert turn.tool_calls == 1
    assert target.read_text() == "hi"
    # user, assistant(tool_use), user(tool_result), assistant(answer)
    assert [m["role"] for m in messages] == ["user", "assistant", "user", "assistant"]
    assert messages[2]["content"][0]["type"] == "tool_result"


def test_loop_stops_at_iteration_ceiling(registry):
    looping = Response(
        tool_calls=[ToolCall("t", "read_file", {"path": "/nonexistent"})],
        raw_content=[{"type": "tool_use", "id": "t", "name": "read_file", "input": {}}],
    )
    provider = ScriptedProvider([looping] * 10)
    turn = run_conversation(
        provider=provider, model="m", system="s", messages=[], registry=registry,
        budget=Budget(max_iterations=3),
    )
    assert turn.stopped_by == "budget"
    assert len(provider.calls) == 3


def test_tool_errors_come_back_as_results(registry):
    output, is_error = registry.call("edit_file", {"path": "/nope", "old_string": "a", "new_string": "b"})
    assert is_error is False and "No such file" in output
    output, is_error = registry.call("does_not_exist", {})
    assert is_error is True


def test_memory_snapshot_is_frozen_for_the_session(config):
    store = MemoryStore(config.memories_dir)
    before = store.snapshot()
    store.write("memory", "the sky is green")
    assert store.snapshot() == before               # prompt prefix unchanged
    assert "green" in MemoryStore(config.memories_dir).snapshot()  # next session sees it


def test_memory_refuses_to_exceed_its_budget(config):
    store = MemoryStore(config.memories_dir)
    result = store.write("user", "x" * 5000)
    assert "Refused" in result


def test_skills_round_trip_and_never_delete(config):
    library = SkillLibrary(config.skills_dir)
    library.create("deploy-staging", "How to ship to staging", "## Steps\n1. run tests")
    assert "deploy-staging" in library.catalog()

    library.patch("deploy-staging", "1. run tests", "1. run tests\n2. push")
    assert "2. push" in library.get("deploy-staging").body

    skill = library.get("deploy-staging")
    skill.meta["updated"] = "2000-01-01"
    skill.path.write_text(
        "---\n" + "\n".join(f"{k}: {v}" for k, v in skill.meta.items()) + "\n---\n\n" + skill.body,
        "utf-8",
    )
    assert library.curate()                              # aged out
    assert library.get("deploy-staging").status == "archived"
    assert library.get("deploy-staging") is not None     # but still on disk


def test_session_search_finds_japanese_text(config):
    store = Store(config.state_db)
    session = store.new_session("/tmp")
    store.add_message(session, "user", "デプロイ手順を教えて")
    store.add_message(session, "assistant", "まずテストを実行します")
    if not store.trigram:
        pytest.skip("SQLite build has no trigram tokenizer")
    hits = store.search("デプロイ")
    assert hits and hits[0]["session_id"] == session


def test_registry_subset_narrows_powers(registry):
    narrow = registry.subset(["memory", "skill_manage"])
    assert "terminal" in registry and "terminal" not in narrow
    assert "memory" in narrow


def test_terminal_keeps_shell_state_across_calls(config):
    small = ToolRegistry()
    from simple_agent.tools import terminal

    terminal.register(small, config)
    small.call("terminal", {"command": "export MARKER=42; cd /tmp"})
    output, _ = small.call("terminal", {"command": "echo $MARKER; pwd"})
    assert "42" in output
    assert "tmp" in output


def test_agent_runs_a_turn_and_records_it(config, monkeypatch):
    from simple_agent import agent as agent_module

    provider = ScriptedProvider(
        [Response(text="hello", raw_content=[{"type": "text", "text": "hello"}])]
    )
    monkeypatch.setattr(agent_module, "get_provider", lambda *a, **k: provider)
    config.learning = False  # no background thread in tests

    agent = agent_module.Agent(config)
    turn = agent.run("who are you?")

    assert turn.text == "hello"
    assert "<memory>" in agent.system and "<skills>" in agent.system
    assert agent.store.search("who are you")           # persisted and indexed
    assert agent.system == agent._build_system_prompt()  # prefix stayed put
