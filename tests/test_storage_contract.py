"""Memory and skills behave the same wherever they are kept.

One setting decides that, ``database_url``, and it decides it for all three
things the agent writes — transcripts, memory, skills — so there is no way to
half-move a deployment.

Postgres runs when ``SIMPLE_AGENT_TEST_DATABASE_URL`` is set; see
test_state_contract.py for how to start one.
"""

from __future__ import annotations

import os

import pytest

from simple_agent.config import Config
from simple_agent.memory import LocalMemory, PostgresMemory, open_memory
from simple_agent.skills import FileSkillStore, PostgresSkillStore, SkillLibrary, open_skills
from simple_agent.state import SqliteStore, open_store

PG_URL = os.environ.get("SIMPLE_AGENT_TEST_DATABASE_URL", "")


def _postgres(table):
    if not PG_URL:
        pytest.skip("SIMPLE_AGENT_TEST_DATABASE_URL not set")
    psycopg = pytest.importorskip("psycopg")
    with psycopg.connect(PG_URL, autocommit=True) as conn:
        conn.execute(f"DROP TABLE IF EXISTS {table}")


@pytest.fixture(params=["local", "postgres"])
def memory(request, tmp_path):
    if request.param == "local":
        return LocalMemory(tmp_path, "team")
    _postgres("memories")
    return PostgresMemory(PG_URL, "team")


@pytest.fixture(params=["files", "postgres"])
def library(request, tmp_path):
    if request.param == "files":
        return SkillLibrary(FileSkillStore(tmp_path / "skills"))
    _postgres("skills")
    return SkillLibrary(PostgresSkillStore(PG_URL))


def test_no_database_url_keeps_everything_in_files(tmp_path):
    config = Config(home=tmp_path)
    assert isinstance(open_store(config), SqliteStore)
    assert isinstance(open_memory(config), LocalMemory)
    assert isinstance(open_skills(config).store, FileSkillStore)


def test_a_database_url_moves_all_three_at_once(tmp_path):
    if not PG_URL:
        pytest.skip("SIMPLE_AGENT_TEST_DATABASE_URL not set")
    pytest.importorskip("psycopg")
    config = Config(home=tmp_path, database_url=PG_URL)
    from simple_agent.state_postgres import PostgresStore

    assert isinstance(open_store(config), PostgresStore)
    assert isinstance(open_memory(config), PostgresMemory)
    assert isinstance(open_skills(config).store, PostgresSkillStore)


def test_a_namespace_scopes_memory_without_touching_the_setting(tmp_path):
    config = Config(home=tmp_path, memory_namespace="acme")
    assert open_memory(config).namespace == "acme"
    assert open_memory(config, "support").namespace == "support"


def test_memory_retains_once_and_recalls_japanese(memory):
    assert "Saved" in memory.retain("デプロイ担当は運用チーム")
    assert "Already" in memory.retain("デプロイ担当は運用チーム")
    memory.retain("The release train leaves on Fridays")

    hits = memory.recall("デプロイの担当者は?")
    assert hits and hits[0].text == "デプロイ担当は運用チーム"
    assert memory.recall("") == []


def test_memory_namespaces_do_not_mix(memory, tmp_path):
    memory.retain("only for team")
    other = (
        LocalMemory(tmp_path, "other") if isinstance(memory, LocalMemory)
        else type(memory)(PG_URL, "other")
    )
    assert other.recall("only for team") == []


def test_skills_create_patch_use_and_list(library):
    assert "Created" in library.create("triage-mail", "Sort incoming mail", "1. Read it.")
    assert "already exists" in library.create("triage-mail", "x", "y")
    assert "Patched" in library.patch("triage-mail", "Read it.", "Read it twice.")
    library.mark_used("triage-mail")

    skill = library.get("triage-mail")
    assert skill.body.strip() == "1. Read it twice." and skill.meta["uses"] == "1"
    assert library.catalog() == "- triage-mail: Sort incoming mail"
    assert [s.name for s in library.all()] == ["triage-mail"]
    assert library.get("missing") is None
