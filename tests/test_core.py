"""Smoke tests — no network. A fake provider stands in for the LLM."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from simple_agent.compaction import TailCompactor  # noqa: E402
from simple_agent.config import Config  # noqa: E402
from simple_agent.context import current_session_key, session_scope  # noqa: E402
from simple_agent.loop import Budget, run_conversation  # noqa: E402
from simple_agent.memory import HindsightMemory, LocalMemory, Mem0Memory, Memory  # noqa: E402
from simple_agent.providers.base import Provider, Response, ToolCall  # noqa: E402
from simple_agent.registry import AgentRegistry  # noqa: E402
from simple_agent.session import (  # noqa: E402
    SessionSource,
    build_session_key,
    is_shared_multi_user_session,
)
from simple_agent.skills import SkillLibrary, find_specifics  # noqa: E402
from simple_agent.state import SqliteStore  # noqa: E402
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


def text_response(text):
    return Response(text=text, raw_content=[{"type": "text", "text": text}])


def tool_response(*calls):
    return Response(
        tool_calls=[ToolCall(*c) for c in calls],
        raw_content=[
            {"type": "tool_use", "id": c[0], "name": c[1], "input": c[2]} for c in calls
        ],
    )


@pytest.fixture
def config(tmp_path):
    cfg = Config(home=tmp_path, learning=False)
    for directory in (cfg.memories_dir, cfg.skills_dir, cfg.shell_state_dir):
        directory.mkdir(parents=True, exist_ok=True)
    return cfg


@pytest.fixture
def registry(config):
    return build_registry(
        config,
        LocalMemory(config.memories_dir),
        SkillLibrary(config.skills_dir),
        SqliteStore(config.state_db),
    )


def build_agent(config, provider, **kwargs):
    from simple_agent.agent import Agent

    return Agent(config, provider=provider, **kwargs)


# -- loop -----------------------------------------------------------------
def test_loop_returns_answer_when_no_tools_requested(registry):
    provider = ScriptedProvider([text_response("done")])
    turn = run_conversation(
        provider=provider, model="m", system="s",
        messages=[{"role": "user", "content": "hi"}], registry=registry,
    )
    assert turn.text == "done"
    assert turn.tool_calls == 0
    assert turn.complete


def test_loop_runs_tools_then_answers(registry, tmp_path):
    target = tmp_path / "hello.txt"
    provider = ScriptedProvider(
        [
            tool_response(("t1", "write_file", {"path": str(target), "content": "hi"})),
            text_response("wrote it"),
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


def test_loop_stops_at_turn_ceiling(registry):
    looping = tool_response(("t", "read_file", {"path": "/nonexistent"}))
    provider = ScriptedProvider([looping] * 10)
    turn = run_conversation(
        provider=provider, model="m", system="s", messages=[], registry=registry,
        max_turn_iterations=3,
    )
    assert turn.stopped_by == "turn_limit"
    assert len(provider.calls) == 3


def test_loop_stops_at_session_ceiling(registry):
    looping = tool_response(("t", "read_file", {"path": "/nonexistent"}))
    provider = ScriptedProvider([looping] * 10)
    turn = run_conversation(
        provider=provider, model="m", system="s", messages=[], registry=registry,
        budget=Budget(max_iterations=2), max_turn_iterations=99,
    )
    assert turn.stopped_by == "budget"
    assert len(provider.calls) == 2


def test_loop_keeps_the_order_the_model_asked_for(registry, tmp_path):
    """A read that follows a write must not overtake it.

    read_file is parallel-safe and write_file is not; batching all reads first
    would return the file's old contents while the model saw them in order.
    """
    target = tmp_path / "ordered.txt"
    target.write_text("old", "utf-8")
    provider = ScriptedProvider(
        [
            tool_response(
                ("a", "write_file", {"path": str(target), "content": "new"}),
                ("b", "read_file", {"path": str(target)}),
            ),
            text_response("ok"),
        ]
    )
    messages = [{"role": "user", "content": "go"}]
    run_conversation(
        provider=provider, model="m", system="s", messages=messages, registry=registry
    )
    results = messages[2]["content"]
    assert [r["tool_use_id"] for r in results] == ["a", "b"]
    assert "new" in results[1]["content"]


def test_loop_interrupt_answers_every_outstanding_tool_call(registry, tmp_path):
    """Stopping mid-turn must still leave a transcript the provider accepts."""
    target = tmp_path / "a.txt"
    target.write_text("x", "utf-8")
    stop = []

    def interrupt():
        # False on the loop's entry check, True once the first tool has run.
        return len(stop) > 1

    original_call = registry.call

    def counting_call(name, arguments):
        stop.append(name)
        return original_call(name, arguments)

    registry.call = counting_call
    provider = ScriptedProvider(
        [
            tool_response(
                ("x", "write_file", {"path": str(target), "content": "1"}),
                ("y", "write_file", {"path": str(target), "content": "2"}),
                ("z", "write_file", {"path": str(target), "content": "3"}),
            )
        ]
    )
    messages = [{"role": "user", "content": "go"}]
    turn = run_conversation(
        provider=provider, model="m", system="s", messages=messages, registry=registry,
        interrupt=interrupt,
    )

    assert turn.stopped_by == "interrupt"
    assert len(stop) < 3  # really did stop early
    results = messages[1]["content"] if isinstance(messages[1]["content"], list) else []
    answered = [b["tool_use_id"] for b in messages[2]["content"]]
    asked = [b["id"] for b in results if b["type"] == "tool_use"]
    assert answered == asked  # every call got an answer, run or not
    assert messages[-1]["role"] == "assistant"  # and the halt is in the record


def test_tool_errors_come_back_as_results(registry):
    output, is_error = registry.call(
        "edit_file", {"path": "/nope", "old_string": "a", "new_string": "b"}
    )
    assert is_error is False and "No such file" in output
    output, is_error = registry.call("does_not_exist", {})
    assert is_error is True


# -- session identity -----------------------------------------------------
def test_dms_never_share_a_session():
    a = SessionSource(platform="slack", chat_id="D1", chat_type="dm", user_id="U1")
    b = SessionSource(platform="slack", chat_id="D2", chat_type="dm", user_id="U2")
    assert build_session_key(a) != build_session_key(b)


def test_a_thread_is_one_conversation_for_everyone():
    kwargs = dict(platform="slack", chat_id="C1", chat_type="group", thread_id="T1")
    alice = SessionSource(user_id="U1", **kwargs)
    bob = SessionSource(user_id="U2", **kwargs)
    assert build_session_key(alice) == build_session_key(bob)
    assert is_shared_multi_user_session(alice)


def test_channel_chatter_is_isolated_per_speaker():
    kwargs = dict(platform="slack", chat_id="C1", chat_type="group")
    alice = SessionSource(user_id="U1", **kwargs)
    bob = SessionSource(user_id="U2", **kwargs)
    assert build_session_key(alice) != build_session_key(bob)
    assert not is_shared_multi_user_session(alice)


def test_platforms_are_namespaced_so_ids_cannot_collide():
    slack = SessionSource(platform="slack", chat_id="X", chat_type="dm")
    discord = SessionSource(platform="discord", chat_id="X", chat_type="dm")
    assert build_session_key(slack) != build_session_key(discord)


def test_a_dm_without_a_chat_id_falls_back_to_the_sender():
    """Otherwise every such DM collapses into one shared session."""
    a = SessionSource(platform="sms", chat_type="dm", user_id="U1")
    b = SessionSource(platform="sms", chat_type="dm", user_id="U2")
    assert build_session_key(a) != build_session_key(b)


# -- memory ---------------------------------------------------------------
def test_long_term_memory_recalls_japanese_by_relevance(config):
    memory = LocalMemory(config.memories_dir, "team-a")
    memory.retain("リリースは毎週金曜日に行う", context="slack")
    memory.retain("請求書の担当は経理チーム")
    assert "Already" in memory.retain("リリースは毎週金曜日に行う")

    hits = memory.recall("次のリリースはいつ？")
    assert hits and hits[0].text == "リリースは毎週金曜日に行う"
    assert LocalMemory(config.memories_dir, "team-b").recall("リリース") == []  # namespaced


class FakeHTTP:
    """Stands in for urlopen: records each request, answers with ``payload``."""

    def __init__(self, payload):
        self.payload = payload
        self.requests = []

    def __call__(self, request, timeout=None):
        import io
        import json

        self.requests.append(
            (request.full_url, dict(request.header_items()), json.loads(request.data))
        )
        body = io.BytesIO(json.dumps(self.payload).encode())
        body.__enter__ = lambda: body
        body.__exit__ = lambda *a: None
        return body


def test_mem0_scopes_team_memory_by_app_id(monkeypatch):
    http = FakeHTTP({"results": [{"id": "m1", "memory": "Deploys on Fridays", "score": 0.9}]})
    monkeypatch.setattr("urllib.request.urlopen", http)
    memory = Mem0Memory("key", "acme", base_url="https://mem0.test")

    assert memory.recall("deploy") == [Memory("m1", "Deploys on Fridays", 0.9)]
    memory.retain("Ops owns deploys", context="slack")

    (search_url, headers, search), (add_url, _, add) = http.requests
    assert search_url == "https://mem0.test/v3/memories/search/"
    assert headers["Authorization"] == "Token key"
    assert search["filters"] == {"app_id": "acme"}
    assert add_url == "https://mem0.test/v3/memories/add/"
    assert add["app_id"] == "acme" and add["messages"][0]["content"] == "Ops owns deploys"


def test_hindsight_uses_the_namespace_as_its_bank(monkeypatch):
    http = FakeHTTP({"results": [{"id": "h1", "text": "Ops owns deploys"}]})
    monkeypatch.setattr("urllib.request.urlopen", http)
    memory = HindsightMemory("acme", base_url="http://hs.test")

    assert [m.text for m in memory.recall("who deploys")] == ["Ops owns deploys"]
    memory.retain("Deploys on Fridays", context="slack")
    (recall_url, _, recall), (retain_url, _, retain) = http.requests
    assert recall_url == "http://hs.test/v1/default/banks/acme/memories/recall"
    assert recall["query"] == "who deploys"
    assert retain_url == "http://hs.test/v1/default/banks/acme/memories"
    assert retain["items"] == [{"content": "Deploys on Fridays", "context": "slack"}]


# -- skills ---------------------------------------------------------------
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
    assert library.curate()  # aged out
    assert library.get("deploy-staging").status == "archived"
    assert library.get("deploy-staging") is not None  # but still on disk


def test_skills_flag_team_specifics_that_belong_in_memory(config):
    library = SkillLibrary(config.skills_dir)
    result = library.create(
        "check-staging", "Verify a staging deploy", "Open https://stg.acme.internal and ask ops@acme.jp"
    )
    assert "Warning" in result and "memory_save" in result
    assert find_specifics("1. open the staging URL\n2. ask the deploy owner") == []


# -- store ----------------------------------------------------------------
def test_session_search_finds_japanese_text(config):
    store = SqliteStore(config.state_db)
    session = store.new_session("/tmp")
    store.add_message(session, "user", "デプロイ手順を教えて")
    store.add_message(session, "assistant", "まずテストを実行します")
    if not store.trigram:
        pytest.skip("SQLite build has no trigram tokenizer")
    hits = store.search("デプロイ")
    assert hits and hits[0]["session_id"] == session


def test_store_replays_tool_calls_not_just_text(config):
    """The whole point of the schema change: a resumed conversation is valid."""
    store = SqliteStore(config.state_db)
    session = store.new_session("/tmp", "slack:C1:T1")
    store.add_message(session, "user", "read the file")
    store.add_message(
        session, "assistant",
        [{"type": "tool_use", "id": "t1", "name": "read_file", "input": {"path": "/x"}}],
    )
    store.add_message(
        session, "user",
        [{"type": "tool_result", "tool_use_id": "t1", "content": "contents"}],
    )
    store.add_message(session, "assistant", "it says 'contents'")

    replay = store.conversation(session)
    assert [m["role"] for m in replay] == ["user", "assistant", "user", "assistant"]
    assert replay[1]["content"][0]["type"] == "tool_use"
    assert replay[2]["content"][0]["tool_use_id"] == "t1"


def test_store_finds_the_session_a_thread_should_resume(config):
    store = SqliteStore(config.state_db)
    first = store.new_session("/tmp", "main:slack:group:C1:T1")
    store.add_message(first, "user", "hello")
    other = store.new_session("/tmp", "main:slack:group:C1:T2")
    assert store.latest_session_for_key("main:slack:group:C1:T1") == first
    assert store.latest_session_for_key("main:slack:group:C1:T2") == other
    assert store.latest_session_for_key("main:slack:group:C1:T9") is None


def test_search_indexes_readable_text_not_json(config):
    store = SqliteStore(config.state_db)
    session = store.new_session("/tmp")
    store.add_message(
        session, "assistant",
        [{"type": "text", "text": "the deployment finished"},
         {"type": "tool_use", "id": "t1", "name": "terminal", "input": {"command": "x"}}],
    )
    hits = store.search("deployment")
    assert hits
    assert "tool_use" not in hits[0]["window"][0]["text"]


# -- compaction -----------------------------------------------------------
def test_compaction_never_orphans_a_tool_call():
    messages = [{"role": "user", "content": "start"}, {"role": "assistant", "content": "ok"}]
    for i in range(10):
        messages += [
            {"role": "user", "content": f"do {i}"},
            {"role": "assistant", "content": [{"type": "tool_use", "id": f"t{i}", "name": "x", "input": {}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"t{i}", "content": "r"}]},
            {"role": "assistant", "content": f"done {i}"},
        ]

    compacted = TailCompactor(keep_head=2, keep_tail=6).compact(messages)
    assert len(compacted) < len(messages)

    open_calls = set()
    for message in compacted:
        content = message.get("content")
        blocks = content if isinstance(content, list) else []
        for block in blocks:
            if block.get("type") == "tool_use":
                open_calls.add(block["id"])
            elif block.get("type") == "tool_result":
                # every result answers a call that is still in the transcript
                assert block["tool_use_id"] in open_calls
    # and roles still alternate, which the provider requires
    roles = [m["role"] for m in compacted]
    assert all(a != b for a, b in zip(roles, roles[1:]))


def test_compaction_only_fires_when_the_context_is_actually_large():
    compactor = TailCompactor(context_limit=1000, threshold=0.5)
    many = [{"role": "user", "content": "x"}] * 100
    assert not compactor.should_compact(many, prompt_tokens=100)
    assert compactor.should_compact(many, prompt_tokens=600)


# -- tools ----------------------------------------------------------------
def test_registry_subset_narrows_powers(registry):
    narrow = registry.subset(["memory_search", "skill_manage"])
    assert "terminal" in registry and "terminal" not in narrow
    assert "memory_search" in narrow and "memory_save" not in narrow


def test_terminal_keeps_shell_state_across_calls(config):
    small = ToolRegistry()
    from simple_agent.tools import terminal

    terminal.register(small, config)
    with session_scope("main:local:dm:/tmp"):
        small.call("terminal", {"command": "export MARKER=42; cd /tmp"})
        output, _ = small.call("terminal", {"command": "echo $MARKER; pwd"})
    assert "42" in output
    assert "tmp" in output


def test_terminal_state_does_not_leak_between_conversations(config):
    small = ToolRegistry()
    from simple_agent.tools import terminal

    terminal.register(small, config)
    with session_scope("main:slack:dm:alice"):
        small.call("terminal", {"command": "export SECRET=alice"})
    with session_scope("main:slack:dm:bob"):
        output, _ = small.call("terminal", {"command": "echo [$SECRET]"})
    assert "alice" not in output


# -- agent ----------------------------------------------------------------
def test_agent_runs_a_turn_and_records_it(config):
    provider = ScriptedProvider([text_response("hello")])
    agent = build_agent(config, provider)
    turn = agent.run("who are you?")

    assert turn.text == "hello"
    assert "<skills>" in agent.system
    assert agent.store.search("who are you")  # persisted and indexed
    assert agent.system == agent._build_system_prompt()  # prefix stayed put


def test_long_term_memory_enters_the_conversation_not_the_system_prompt(config):
    memory = LocalMemory(config.memories_dir)
    memory.retain("Releases go out on Fridays")
    provider = ScriptedProvider([text_response("Friday"), text_response("still Friday")])
    agent = build_agent(config, provider, memory=memory)
    system = agent.system

    agent.run("when do releases go out?")
    first = provider.calls[0]["messages"][0]["content"]
    assert "<long_term_memory" in first and "Fridays" in first
    assert "Fridays" not in system and agent.system == system  # prefix untouched

    agent.run("and releases next month?")  # already in short-term memory
    assert "<long_term_memory" not in provider.calls[1]["messages"][-1]["content"]


def test_untrusted_routes_are_not_handed_team_memory(config):
    memory = LocalMemory(config.memories_dir)
    memory.retain("Releases go out on Fridays")
    provider = ScriptedProvider([text_response("no idea")])
    agent = build_agent(config, provider, memory=memory, tools=["skill_view"])
    agent.run("when do releases go out?")
    assert provider.calls[0]["messages"][0]["content"] == "when do releases go out?"


def test_a_memory_backend_outage_costs_the_recall_not_the_turn(config):
    class Down(LocalMemory):
        def recall(self, query, limit=8):
            raise OSError("mem0 unreachable")

    provider = ScriptedProvider([text_response("hello")])
    agent = build_agent(config, provider, memory=Down(config.memories_dir))
    assert agent.run("hi").text == "hello"


def test_agent_resumes_a_conversation_by_session_key(config):
    source = SessionSource(platform="slack", chat_id="C1", chat_type="group", thread_id="T1")

    first = build_agent(config, ScriptedProvider([text_response("hi")]), source=source)
    first.run("hello there")
    assert len(first.messages) == 2

    second = build_agent(config, ScriptedProvider([text_response("again")]), source=source)
    assert second.session_id == first.session_id
    assert [m["role"] for m in second.messages] == ["user", "assistant"]
    assert second.messages[0]["content"] == "hello there"


def test_agent_resumes_a_conversation_that_used_tools(config):
    """A restart mid-tool-use must not produce a transcript the API rejects."""
    source = SessionSource(platform="slack", chat_id="C1", chat_type="dm", user_id="U1")
    provider = ScriptedProvider(
        [tool_response(("t1", "read_file", {"path": "/nope"})), text_response("no file")]
    )
    first = build_agent(config, provider, source=source)
    first.run("read it")

    second = build_agent(config, ScriptedProvider([text_response("ok")]), source=source)
    assert [m["role"] for m in second.messages] == ["user", "assistant", "user", "assistant"]
    assert second.messages[1]["content"][0]["type"] == "tool_use"
    assert second.messages[2]["content"][0]["type"] == "tool_result"


def test_agent_repairs_a_transcript_left_mid_tool_call(config):
    """A killed process leaves a tool call unanswered; the next turn settles it."""
    source = SessionSource(platform="slack", chat_id="C9", chat_type="dm", user_id="U9")
    agent = build_agent(config, ScriptedProvider([text_response("fine")]), source=source)
    agent.messages.append(
        {"role": "assistant", "content": [{"type": "tool_use", "id": "orphan", "name": "x", "input": {}}]}
    )
    agent.run("continue")

    # The answer and the new message are one user turn, so roles alternate.
    assert [m["role"] for m in agent.messages] == ["assistant", "user", "assistant"]
    blocks = agent.messages[1]["content"]
    assert blocks[0]["tool_use_id"] == "orphan" and blocks[0]["is_error"]
    assert blocks[-1] == {"type": "text", "text": "continue"}


def test_agent_does_not_double_write_a_merged_user_turn(config):
    source = SessionSource(platform="slack", chat_id="CX", chat_type="dm", user_id="UX")
    agent = build_agent(config, ScriptedProvider([text_response("ok")]), source=source)
    agent.run("first")

    # Simulate a process killed after the tool results were written but before
    # the reply: the transcript ends on a user turn.
    agent.messages.append({"role": "user", "content": "half-sent"})
    agent._persist_new_messages()
    before = agent.store.message_count(agent.session_id)

    agent.provider.responses.append(text_response("second"))
    agent.run("rest of it")

    replay = agent.store.conversation(agent.session_id)
    assert [m["role"] for m in replay] == ["user", "assistant", "user", "assistant"]
    assert "half-sent" in replay[2]["content"] and "rest of it" in replay[2]["content"]
    assert agent.store.message_count(agent.session_id) == before + 1


def test_agent_starts_a_separate_conversation_per_thread(config):
    base = dict(platform="slack", chat_id="C1", chat_type="group")
    one = build_agent(
        config, ScriptedProvider([text_response("a")]),
        source=SessionSource(thread_id="T1", **base),
    )
    two = build_agent(
        config, ScriptedProvider([text_response("b")]),
        source=SessionSource(thread_id="T2", **base),
    )
    assert one.session_key != two.session_key
    assert one.session_id != two.session_id


def test_agent_sets_the_session_context_while_running(config):
    seen = {}

    class Peeking(ScriptedProvider):
        def complete(self, **kwargs):
            seen["key"] = current_session_key()
            return super().complete(**kwargs)

    agent = build_agent(config, Peeking([text_response("ok")]))
    agent.run("hi")
    assert seen["key"] == agent.session_key
    assert current_session_key() == ""  # and cleaned up afterwards


# -- registry -------------------------------------------------------------
def test_registry_reuses_one_agent_per_conversation(config):
    built = []

    def factory(session_key, source):
        built.append(session_key)
        return build_agent(config, ScriptedProvider([text_response("x")]), source=source)

    registry = AgentRegistry(factory, max_agents=2)
    source = SessionSource(platform="slack", chat_id="C1", chat_type="dm", user_id="U1")

    first = registry.get("k1", source)
    assert registry.get("k1", source) is first
    assert built == ["k1"]


def test_registry_evicts_least_recently_used_over_the_cap(config):
    def factory(session_key, source):
        return build_agent(
            config, ScriptedProvider([text_response("x")]),
            source=SessionSource(platform="p", chat_id=session_key, chat_type="dm"),
        )

    registry = AgentRegistry(factory, max_agents=2)
    registry.get("k1")
    registry.get("k2")
    registry.get("k1")  # k1 becomes most recent
    registry.get("k3")  # pushes over the cap
    assert "k2" not in registry
    assert "k1" in registry and "k3" in registry


def test_registry_never_evicts_a_conversation_mid_turn(config):
    def factory(session_key, source):
        return build_agent(
            config, ScriptedProvider([text_response("x")]),
            source=SessionSource(platform="p", chat_id=session_key, chat_type="dm"),
        )

    registry = AgentRegistry(factory, max_agents=1)
    registry.get("busy")
    with registry.lease("busy"):
        registry.get("other")
        assert "busy" in registry  # over cap, but protected


# -- migration and async bridge -------------------------------------------
def test_store_upgrades_a_database_written_by_an_older_build(tmp_path):
    """An existing ~/.simple-agent/state.db must survive the schema change."""
    import sqlite3

    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript(
        """
        CREATE TABLE sessions (id TEXT PRIMARY KEY, started_at REAL NOT NULL,
                               cwd TEXT, title TEXT);
        CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT,
                               session_id TEXT NOT NULL, role TEXT NOT NULL,
                               content TEXT NOT NULL, created_at REAL NOT NULL);
        CREATE VIRTUAL TABLE messages_fts USING fts5(content);
        CREATE TRIGGER messages_fts_insert AFTER INSERT ON messages BEGIN
            INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
        END;
        INSERT INTO sessions VALUES ('s1', 1.0, '/tmp', 'old chat');
        INSERT INTO messages (session_id, role, content, created_at)
            VALUES ('s1', 'user', 'the old deployment question', 2.0);
        """
    )
    old.commit()
    old.close()

    store = SqliteStore(path)
    assert store.search("deployment")  # old rows stayed searchable
    assert [m["role"] for m in store.conversation("s1")] == ["user"]

    # and the upgraded database accepts the new shape
    store.add_message(
        "s1", "assistant",
        [{"type": "tool_use", "id": "t1", "name": "terminal", "input": {}}],
    )
    assert store.conversation("s1")[1]["content"][0]["type"] == "tool_use"


def test_host_builds_from_a_default_config(config):
    from simple_agent.host import AllowlistRouter, Host
    from simple_agent.registry import DEFAULT_IDLE_SECONDS, DEFAULT_MAX_AGENTS

    host = Host(config, sources=[], router=AllowlistRouter(allow=("@example.com",)))
    assert host.agents.max_agents == DEFAULT_MAX_AGENTS
    assert host.agents.idle_seconds == DEFAULT_IDLE_SECONDS


def test_async_hosts_can_drive_the_sync_agent_without_losing_the_session(config):
    """The bridge a chat gateway needs: blocking work, context carried along."""
    import asyncio

    from simple_agent.context import WorkerPool, run_in_executor_with_context

    pool = WorkerPool(max_workers=2)
    agent = build_agent(config, ScriptedProvider([text_response("from a worker")]))

    async def main():
        with session_scope(agent.session_key, source=agent.source):
            return await run_in_executor_with_context(
                lambda: (current_session_key(), agent.run("hi").text), pool=pool
            )

    key, text = asyncio.run(main())
    pool.shutdown()
    assert key == agent.session_key  # the worker thread saw the conversation
    assert text == "from a worker"


def test_many_conversations_can_share_one_store_concurrently(config):
    """Reads need the store's lock as much as writes do.

    `check_same_thread=False` only silences Python's guard. Driving one sqlite
    connection from two threads at once crashes the interpreter rather than
    raising, so an unlocked read path fails as a segfault under load — not as
    a test failure. This exercises the shape that found it.
    """
    import threading

    from simple_agent.state import Store

    store = SqliteStore(config.state_db)
    errors: list[str] = []

    def talk(n: int) -> None:
        try:
            source = SessionSource(
                platform="slack", chat_id="C1", chat_type="group",
                thread_id=f"T{n}", user_id=f"U{n}",
            )
            for i in range(4):
                agent = build_agent(
                    config, ScriptedProvider([text_response("ok")]),
                    source=source, store=store,
                )
                agent.run(f"message {i}")
                store.search("message")
        except Exception as exc:  # pragma: no cover - only on regression
            errors.append(repr(exc))

    threads = [threading.Thread(target=talk, args=(n,)) for n in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert len(store.recent_sessions(limit=50)) == 8  # one per thread, resumed


# -- Bedrock Converse adapter ---------------------------------------------------


def test_bedrock_request_narrows_the_normalized_transcript():
    from simple_agent.providers.bedrock import build_request

    body = build_request(
        system="sys",
        messages=[
            {"role": "user", "content": "list files"},
            {"role": "assistant", "content": [
                {"type": "text", "text": ""},
                {"type": "tool_use", "id": "t1", "name": "terminal", "input": {"command": "ls"}},
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "", "is_error": True},
            ]},
        ],
        tools=[{"name": "terminal", "description": "run", "input_schema": {"type": "object"}}],
        max_tokens=100,
    )

    assert body["system"] == [{"text": "sys"}, {"cachePoint": {"type": "default"}}]
    assert body["inferenceConfig"] == {"maxTokens": 100}
    assert body["messages"][0] == {"role": "user", "content": [{"text": "list files"}]}
    # the empty text block is dropped; Converse rejects it
    assert body["messages"][1]["content"] == [
        {"toolUse": {"toolUseId": "t1", "name": "terminal", "input": {"command": "ls"}}}
    ]
    result = body["messages"][2]["content"][0]["toolResult"]
    assert result["status"] == "error"
    assert result["content"] == [{"text": "(no output)"}]
    spec = body["toolConfig"]["tools"][0]["toolSpec"]
    assert spec == {"name": "terminal", "description": "run", "inputSchema": {"json": {"type": "object"}}}


def test_bedrock_response_widens_back_to_the_normalized_shape():
    from simple_agent.providers.bedrock import parse_response

    response = parse_response({
        "output": {"message": {"role": "assistant", "content": [
            {"text": "checking"},
            {"toolUse": {"toolUseId": "t9", "name": "read_file", "input": {"path": "a"}}},
        ]}},
        "stopReason": "tool_use",
        "usage": {"inputTokens": 12, "outputTokens": 3},
    })

    assert response.text == "checking"
    assert response.tool_calls[0].id == "t9"
    assert response.tool_calls[0].arguments == {"path": "a"}
    assert response.raw_content == [
        {"type": "text", "text": "checking"},
        {"type": "tool_use", "id": "t9", "name": "read_file", "input": {"path": "a"}},
    ]
    assert (response.input_tokens, response.output_tokens) == (12, 3)
    assert response.stop_reason == "tool_use"


def test_bedrock_sends_a_bearer_token_to_the_regional_endpoint(monkeypatch):
    import io
    import json as _json

    from simple_agent.providers import bedrock

    sent = {}

    def fake_urlopen(request, timeout):
        sent["url"] = request.full_url
        sent["auth"] = request.get_header("Authorization")
        reply = {"output": {"message": {"content": [{"text": "hi"}]}}, "stopReason": "end_turn"}
        return io.BytesIO(_json.dumps(reply).encode())

    monkeypatch.setattr(bedrock.urllib.request, "urlopen", fake_urlopen)
    provider = bedrock.BedrockProvider(api_key="k", region="ap-northeast-1")
    response = provider.complete(
        system="s", messages=[{"role": "user", "content": "hi"}], tools=[],
        max_tokens=10, model="jp.anthropic.claude-haiku-4-5-20251001-v1:0",
    )

    assert sent["url"] == (
        "https://bedrock-runtime.ap-northeast-1.amazonaws.com/model/"
        "jp.anthropic.claude-haiku-4-5-20251001-v1%3A0/converse"
    )
    assert sent["auth"] == "Bearer k"
    assert response.text == "hi"


def test_switching_provider_switches_default_models(tmp_path, monkeypatch):
    monkeypatch.setenv("SIMPLE_AGENT_HOME", str(tmp_path))
    monkeypatch.setenv("SIMPLE_AGENT_PROVIDER", "bedrock")
    monkeypatch.delenv("SIMPLE_AGENT_MODEL", raising=False)
    monkeypatch.delenv("SIMPLE_AGENT_REVIEW_MODEL", raising=False)
    import simple_agent.config as config_module

    monkeypatch.setattr(config_module, "HOME", tmp_path)
    cfg = config_module.Config.load()

    assert cfg.model == "jp.anthropic.claude-sonnet-4-6"
    assert cfg.review_model == "jp.anthropic.claude-haiku-4-5-20251001-v1:0"


def test_config_yaml_sets_only_what_it_names_and_env_wins(tmp_path, monkeypatch):
    for key in ("PROVIDER", "MODEL", "REVIEW_MODEL", "LEARNING"):
        monkeypatch.delenv(f"SIMPLE_AGENT_{key}", raising=False)
    import simple_agent.config as config_module

    monkeypatch.setattr(config_module, "HOME", tmp_path)
    (tmp_path / "config.yaml").write_text(
        "# mine\nprovider: bedrock\nlearning: false  # quiet\n", "utf-8"
    )
    monkeypatch.setenv("SIMPLE_AGENT_MODEL", "custom-model")
    cfg = config_module.Config.load()

    assert cfg.provider == "bedrock"
    assert cfg.learning is False
    assert cfg.model == "custom-model"
    assert cfg.review_model == "jp.anthropic.claude-haiku-4-5-20251001-v1:0"


def test_config_yaml_rejects_unknown_keys(tmp_path, monkeypatch):
    import simple_agent.config as config_module

    monkeypatch.setattr(config_module, "HOME", tmp_path)
    (tmp_path / "config.yaml").write_text("max_tokens: 100\n", "utf-8")
    with pytest.raises(ValueError, match="max_tokens"):
        config_module.Config.load()
