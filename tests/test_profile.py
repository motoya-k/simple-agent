"""Profiles: who the agent is on a route, and the two halves that travel together.

A profile that narrows the toolset must also withhold learning — otherwise an
inbox anyone can write to could leave a note that a trusted conversation later
reads as the team's own knowledge. These tests hold those two together.
"""

from __future__ import annotations

import pytest

from simple_agent.agent import Agent
from simple_agent.config import Config
from simple_agent.memory import LocalMemory
from simple_agent.profile import BUILT_IN, Profile, load_profile
from simple_agent.providers.base import Provider, Response


class Scripted(Provider):
    name = "scripted"

    def __init__(self, text="done"):
        self.text, self.calls = text, []

    def complete(self, *, system, messages, tools, max_tokens, model):
        self.calls.append({"system": system, "messages": list(messages), "tools": tools})
        return Response(text=self.text, raw_content=[{"type": "text", "text": self.text}])


@pytest.fixture
def config(tmp_path):
    cfg = Config(home=tmp_path)
    for directory in (cfg.memories_dir, cfg.skills_dir, cfg.profiles_dir, cfg.shell_state_dir):
        directory.mkdir(parents=True, exist_ok=True)
    return cfg


def write_profile(config, name, text):
    (config.profiles_dir / f"{name}.md").write_text(text, "utf-8")


# -- the built-ins --------------------------------------------------------
def test_terminal_is_the_default_and_holds_nothing_back(config):
    profile = load_profile(config)
    assert profile.name == "terminal"
    assert profile.tools is None  # every tool the config allows
    assert profile.learning is True


def test_email_is_read_only_and_may_not_teach(config):
    profile = load_profile(config, "email")
    assert profile.tools == ("skill_view",)
    assert profile.learning is False
    assert profile.setting("imap_mailbox") == "INBOX"


def test_an_unknown_profile_is_an_error_not_a_silent_default(config):
    """A typo must not quietly hand an inbox the full toolset."""
    with pytest.raises(ValueError, match="No such profile"):
        load_profile(config, "emial")


# -- files ----------------------------------------------------------------
def test_a_file_amends_a_built_in_without_replacing_what_it_omits(config):
    write_profile(config, "email", """---
namespace: support
imap_host: imap.example.com
email_allow: "@example.com"
---

You answer support mail. Never promise a refund.
""")
    profile = load_profile(config, "email")

    assert profile.instructions == "You answer support mail. Never promise a refund."
    assert profile.namespace == "support"
    assert profile.setting("imap_host") == "imap.example.com"
    assert profile.setting_list("email_allow") == ("@example.com",)
    assert profile.setting("imap_mailbox") == "INBOX"  # kept from the built-in
    assert profile.tools == ("skill_view",) and profile.learning is False


def test_a_file_can_add_a_profile_that_is_not_built_in(config):
    write_profile(config, "night-shift", """---
tools: skill_view, session_search
learning: false
---
Watch the queue. Escalate nothing before 7am.
""")
    profile = load_profile(config, "night-shift")
    assert profile.tools == ("skill_view", "session_search")
    assert profile.learning is False
    assert profile.instructions.startswith("Watch the queue.")


def test_a_profile_can_ask_for_every_tool(config):
    write_profile(config, "ops", "---\ntools: '*'\n---\nYou run the deploys.\n")
    assert load_profile(config, "ops").tools is None


# -- the environment still wins ------------------------------------------
def test_env_overrides_the_fields_under_the_profile_name(config, monkeypatch):
    write_profile(config, "email", "---\ntools: skill_view\n---\nmail\n")
    monkeypatch.setenv("SIMPLE_AGENT_EMAIL_TOOLS", "skill_view,memory_search")
    monkeypatch.setenv("SIMPLE_AGENT_EMAIL_LEARNING", "1")
    monkeypatch.setenv("SIMPLE_AGENT_EMAIL_NAMESPACE", "acme")

    profile = load_profile(config, "email")
    assert profile.tools == ("skill_view", "memory_search")
    assert profile.learning is True
    assert profile.namespace == "acme"


def test_env_overrides_a_setting_so_a_container_needs_no_file(config, monkeypatch):
    write_profile(config, "email", "---\nimap_host: in-the-file\n---\nmail\n")
    monkeypatch.setenv("SIMPLE_AGENT_IMAP_HOST", "imap.gmail.com")
    assert load_profile(config, "email").setting("imap_host") == "imap.gmail.com"


# -- what the agent does with it -----------------------------------------
def test_the_profile_writes_the_system_prompt(config):
    profile = Profile(name="pm", instructions="You keep the roadmap honest.")
    agent = Agent(config, provider=Scripted(), profile=profile)
    assert agent.system.startswith("You keep the roadmap honest.")


def test_the_profile_narrows_the_toolset(config):
    agent = Agent(config, provider=Scripted(), profile=BUILT_IN["email"])
    assert agent.registry.names() == ["skill_view"]


def test_a_profile_that_may_not_learn_runs_no_review(config, monkeypatch):
    spawned = []
    monkeypatch.setattr(
        "simple_agent.agent.spawn_background_review", lambda *a, **k: spawned.append(a)
    )
    agent = Agent(config, provider=Scripted(), profile=BUILT_IN["email"])
    assert agent.learning is False
    agent.run("hello")
    assert spawned == []


def test_learning_off_in_the_config_beats_a_profile_that_wants_it(tmp_path):
    """A profile cannot turn learning on where the deployment turned it off."""
    config = Config(home=tmp_path, learning=False)
    agent = Agent(config, provider=Scripted(), profile=BUILT_IN["terminal"])
    assert agent.learning is False


def test_the_profile_namespace_scopes_both_memory_and_the_conversation(config):
    team = Profile(name="support", instructions="support", namespace="support-team")
    agent = Agent(config, provider=Scripted(), profile=team)

    assert agent.memory.namespace == "support-team"
    assert agent.session_key.startswith("support-team:")
    # And the other team cannot see what this one saved.
    agent.memory.retain("refunds go through billing")
    assert LocalMemory(config.memories_dir, "default").recall("refunds") == []


def test_an_explicit_toolset_still_wins_over_the_profile(config):
    """A host that narrows further is allowed; the profile is the ceiling it starts from."""
    agent = Agent(config, provider=Scripted(), profile=BUILT_IN["terminal"], tools=["skill_view"])
    assert agent.registry.names() == ["skill_view"]


# -- the email host reads its mailbox out of the profile ------------------
def test_serve_email_takes_its_mailbox_and_router_from_the_profile(config, monkeypatch):
    write_profile(config, "support", """---
tools: skill_view
learning: false
imap_host: imap.example.com
imap_user: support@example.com
email_allow: "@example.com"
---
You answer support mail.
""")
    for key in ("IMAP_HOST", "IMAP_USER", "IMAP_MAILBOX", "EMAIL_ALLOW", "SUPPORT_TOOLS"):
        monkeypatch.delenv(f"SIMPLE_AGENT_{key}", raising=False)
    monkeypatch.setenv("SIMPLE_AGENT_IMAP_PASSWORD", "secret")

    opened: dict = {}

    class FakeSource:
        platform = "email"

        def __init__(self, **kwargs):
            opened.update(kwargs)

    routers = []

    class FakeHost:
        def __init__(self, cfg, *, sources, router, sinks=()):
            routers.append(router)

        async def serve(self):
            return None

    monkeypatch.setattr("simple_agent.mail.ImapSource", FakeSource)
    monkeypatch.setattr("simple_agent.mail.open_ledger", lambda cfg: None)
    monkeypatch.setattr("simple_agent.host.Host", FakeHost)

    from simple_agent.cli import serve_email

    assert serve_email(config, "support") == 0
    assert opened["host"] == "imap.example.com"
    assert opened["user"] == "support@example.com"
    assert opened["mailbox"] == "INBOX"  # the built-in's default, kept
    assert opened["password"] == "secret"  # the one thing that is env-only
    assert routers[0].allow == ("@example.com",)
    assert routers[0].profile.name == "support"
    assert routers[0].profile.learning is False


def test_serve_email_says_what_is_missing_and_where_it_goes(config, monkeypatch, capsys):
    for key in ("IMAP_HOST", "IMAP_USER", "IMAP_PASSWORD", "EMAIL_ALLOW"):
        monkeypatch.delenv(f"SIMPLE_AGENT_{key}", raising=False)

    from simple_agent.cli import serve_email

    assert serve_email(config) == 1
    message = capsys.readouterr().err
    assert "imap_host" in message and "SIMPLE_AGENT_IMAP_PASSWORD" in message
    assert str(config.profiles_dir / "email.md") in message
