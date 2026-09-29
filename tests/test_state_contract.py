"""The Store contract, run against every backend.

SQLite always runs.  Postgres runs when ``SIMPLE_AGENT_TEST_DATABASE_URL``
points at a database this test may drop tables in, e.g.::

    docker run --rm -d -p 55432:5432 -e POSTGRES_PASSWORD=test postgres:16-alpine
    SIMPLE_AGENT_TEST_DATABASE_URL=postgresql://postgres:test@localhost:55432/postgres pytest
"""

from __future__ import annotations

import os

import pytest

from simple_agent.state import SqliteStore

PG_URL = os.environ.get("SIMPLE_AGENT_TEST_DATABASE_URL", "")


@pytest.fixture(params=["sqlite", "postgres"])
def store(request, tmp_path):
    if request.param == "sqlite":
        yield SqliteStore(tmp_path / "state.db")
        return
    if not PG_URL:
        pytest.skip("SIMPLE_AGENT_TEST_DATABASE_URL not set")
    psycopg = pytest.importorskip("psycopg")
    with psycopg.connect(PG_URL, autocommit=True) as conn:
        conn.execute("DROP TABLE IF EXISTS messages, sessions")
    from simple_agent.state_postgres import PostgresStore

    yield PostgresStore(PG_URL)


def test_replays_blocks_and_hides_rewound_messages(store):
    session = store.new_session("/tmp", "main:mail:dm:a@example.com")
    store.add_message(session, "user", "read the file")
    tool_turn = [{"type": "tool_use", "id": "t1", "name": "read_file", "input": {"path": "a"}}]
    store.add_message(session, "assistant", tool_turn)
    rewind_from = store.add_message(session, "user", "never mind")
    store.add_message(session, "assistant", "ok")

    assert store.conversation(session)[1]["content"] == tool_turn
    assert store.deactivate_from(session, rewind_from) == 2
    assert store.message_count(session) == 2
    assert store.latest_session_for_key("main:mail:dm:a@example.com") == session


def test_replace_supersedes_the_row(store):
    session = store.new_session("/tmp")
    row = store.add_message(session, "user", "first draft")
    store.replace_message(session, row, "second draft")
    assert [m["content"] for m in store.conversation(session)] == ["second draft"]


def test_search_finds_words_and_japanese(store):
    session = store.new_session("/tmp")
    store.add_message(session, "user", "デプロイ手順を教えて")
    store.add_message(session, "assistant", "Run the deployment script first")

    english = store.search("deployment")
    assert english and "deployment" in english[0]["window"][-1]["text"]
    if store.trigram:
        japanese = store.search("デプロイ")
        assert japanese and japanese[0]["opened_with"] == "デプロイ手順を教えて"


def test_search_survives_hostile_queries(store):
    session = store.new_session("/tmp")
    store.add_message(session, "user", 'a "quoted" 100% thing_here')
    for query in ['"', "(", "100%", "thing_", "AND OR NOT"]:
        store.search(query)  # must not raise


def test_recent_sessions_are_mappings(store):
    session = store.new_session("/tmp")
    store.add_message(session, "user", "title me")
    row = store.recent_sessions()[0]
    assert row["id"] == session and row["title"] == "title me"
