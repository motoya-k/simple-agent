"""Mods: the decision made about a tool call before it becomes an action.

The two hooks fail in opposite directions, and that asymmetry is the point —
a policy that crashed has approved nothing, while a redaction that crashed
should not cost the turn. Most of these tests exist to hold that line.
"""

from __future__ import annotations

import pytest

from simple_agent.config import Config
from simple_agent.mods import Deny, Mods, load_mods
from simple_agent.profile import Profile, load_profile
from simple_agent.tools import Tool, ToolRegistry


@pytest.fixture
def config(tmp_path):
    cfg = Config(home=tmp_path)
    for directory in (cfg.mods_dir, cfg.profiles_dir, cfg.skills_dir, cfg.memories_dir):
        directory.mkdir(parents=True, exist_ok=True)
    return cfg


def write_mod(config, name, body):
    (config.mods_dir / f"{name}.py").write_text(body, "utf-8")


def registry_with(mods, fn=None):
    """A registry holding one `echo` tool, under `mods`."""
    ran = []

    def default(text="hi"):
        ran.append(text)
        return f"echo: {text}"

    registry = ToolRegistry(mods)
    registry.register(
        Tool("echo", "echo", {"type": "object"}, fn or default, parallel_safe=True)
    )
    return registry, ran


# -- before_tool ----------------------------------------------------------
def test_a_mod_can_refuse_a_call_and_the_tool_never_runs(config):
    write_mod(config, "no-shouting", """
def before_tool(name, arguments):
    if arguments.get("text", "").isupper():
        return Deny("no shouting")
""")
    registry, ran = registry_with(load_mods(config, ["no-shouting"]))

    output, is_error = registry.call("echo", {"text": "HELLO"})

    assert is_error and "no shouting" in output and "no-shouting" in output
    assert ran == []  # the tool was never reached
    assert registry.call("echo", {"text": "hello"}) == ("echo: hello", False)


def test_a_mod_can_rewrite_the_arguments(config):
    write_mod(config, "polite", """
def before_tool(name, arguments):
    return {**arguments, "text": arguments["text"] + " please"}
""")
    registry, ran = registry_with(load_mods(config, ["polite"]))

    assert registry.call("echo", {"text": "stop"})[0] == "echo: stop please"
    assert ran == ["stop please"]


def test_returning_none_means_no_opinion(config):
    write_mod(config, "quiet", "def before_tool(name, arguments):\n    return None\n")
    registry, _ = registry_with(load_mods(config, ["quiet"]))
    assert registry.call("echo", {"text": "hi"}) == ("echo: hi", False)


def test_a_crashing_policy_denies_the_call(config):
    """Fail closed: a hook that raised has not approved anything."""
    write_mod(config, "broken", """
def before_tool(name, arguments):
    raise RuntimeError("boom")
""")
    registry, ran = registry_with(load_mods(config, ["broken"]))

    output, is_error = registry.call("echo", {"text": "hi"})

    assert is_error and "broken" in output and "RuntimeError" in output
    assert ran == []


# -- after_tool -----------------------------------------------------------
def test_a_mod_can_redact_the_output(config):
    write_mod(config, "redact", """
def after_tool(name, arguments, output, is_error):
    return output.replace("sk-secret", "[redacted]")
""")
    registry, _ = registry_with(load_mods(config, ["redact"]), fn=lambda: "key is sk-secret")

    assert registry.call("echo", {})[0] == "key is [redacted]"


def test_after_tool_also_sees_failures(config):
    write_mod(config, "note-errors", """
def after_tool(name, arguments, output, is_error):
    return f"[failed] {output}" if is_error else output
""")

    def explode():
        raise ValueError("nope")

    registry, _ = registry_with(load_mods(config, ["note-errors"]), fn=explode)
    output, is_error = registry.call("echo", {})
    assert is_error and output.startswith("[failed] ValueError: nope")


def test_a_crashing_redaction_leaves_the_output_alone(config):
    """Fail open: this only ever shapes what the model reads."""
    write_mod(config, "half-broken", """
def after_tool(name, arguments, output, is_error):
    raise RuntimeError("boom")
""")
    registry, _ = registry_with(load_mods(config, ["half-broken"]))
    assert registry.call("echo", {"text": "hi"}) == ("echo: hi", False)


# -- order and loading ----------------------------------------------------
def test_mods_run_in_order_and_each_sees_the_last_one(config):
    write_mod(config, "first", """
def before_tool(name, arguments):
    return {**arguments, "text": arguments["text"] + "-1"}

def after_tool(name, arguments, output, is_error):
    return output + " <1"
""")
    write_mod(config, "second", """
def before_tool(name, arguments):
    return {**arguments, "text": arguments["text"] + "-2"}

def after_tool(name, arguments, output, is_error):
    return output + " <2"
""")
    registry, _ = registry_with(load_mods(config, ["first", "second"]))

    assert registry.call("echo", {"text": "x"})[0] == "echo: x-1-2 <1 <2"


def test_the_deployments_mods_run_before_a_profiles(tmp_path):
    config = Config(home=tmp_path, mods="house")
    config.mods_dir.mkdir(parents=True, exist_ok=True)
    write_mod(config, "house", """
def before_tool(name, arguments):
    return {**arguments, "text": "house"}
""")
    write_mod(config, "route", """
def before_tool(name, arguments):
    return {**arguments, "text": arguments["text"] + "+route"}
""")
    mods = load_mods(config, ["route"])

    assert mods.names == ["house", "route"]
    assert mods.before_tool("echo", {"text": "x"})[0] == {"text": "house+route"}


def test_a_missing_mod_raises_rather_than_starting_without_it(config):
    with pytest.raises(ValueError, match="No such mod"):
        load_mods(config, ["nope"])


def test_a_mod_that_fails_to_import_raises(config):
    write_mod(config, "bad", "import nonexistent_module_xyz\n")
    with pytest.raises(ValueError, match="failed to load"):
        load_mods(config, ["bad"])


def test_no_mods_costs_nothing(config):
    mods = load_mods(config)
    assert not mods
    registry, _ = registry_with(mods)
    assert registry.call("echo", {"text": "hi"}) == ("echo: hi", False)


def test_deny_can_be_imported_as_well_as_used_bare(config):
    write_mod(config, "explicit", """
from simple_agent.mods import Deny

def before_tool(name, arguments):
    return Deny("not here")
""")
    registry, _ = registry_with(load_mods(config, ["explicit"]))
    assert registry.call("echo", {"text": "hi"})[1] is True


# -- where they reach -----------------------------------------------------
def test_narrowing_the_toolset_keeps_the_rules(config):
    """A subset is fewer tools, not fewer rules about calling them."""
    write_mod(config, "no-echo", "def before_tool(name, arguments):\n    return Deny('never')\n")
    registry, ran = registry_with(load_mods(config, ["no-echo"]))

    narrowed = registry.subset(["echo"])

    assert narrowed.call("echo", {"text": "hi"})[1] is True
    assert ran == []


def test_a_profile_names_its_mods_from_a_file_or_the_environment(config, monkeypatch):
    (config.profiles_dir / "support.md").write_text(
        "---\nmods: redact, no-rm\n---\nsupport\n", "utf-8"
    )
    monkeypatch.delenv("SIMPLE_AGENT_SUPPORT_MODS", raising=False)
    assert load_profile(config, "support").mods == ("redact", "no-rm")

    monkeypatch.setenv("SIMPLE_AGENT_SUPPORT_MODS", "redact")
    assert load_profile(config, "support").mods == ("redact",)


def test_the_agent_loads_the_profiles_mods_at_startup(config):
    """Not on the first tool call, when it is already too late to refuse."""
    from simple_agent.providers.base import Provider, Response

    class Scripted(Provider):
        name = "scripted"

        def complete(self, *, system, messages, tools, max_tokens, model):
            return Response(text="ok", raw_content=[{"type": "text", "text": "ok"}])

    from simple_agent.agent import Agent

    with pytest.raises(ValueError, match="No such mod"):
        Agent(config, provider=Scripted(), profile=Profile("x", "x", mods=("ghost",)))

    write_mod(config, "ghost", "def before_tool(name, arguments):\n    return None\n")
    agent = Agent(config, provider=Scripted(), profile=Profile("x", "x", mods=("ghost",)))
    assert agent.mods.names == ["ghost"] and agent.registry.mods is agent.mods


def test_a_borrowed_toolset_carries_the_mods_too(config):
    """`--mcp --profile x` hands another harness x's rules, not just x's tools."""
    write_mod(config, "no-writes", """
def before_tool(name, arguments):
    if name.endswith("_file"):
        return Deny("read-only here")
""")
    (config.profiles_dir / "reader.md").write_text(
        "---\ntools: '*'\nmods: no-writes\n---\nreader\n", "utf-8"
    )
    from simple_agent.mcp import build_tool_registry

    registry = build_tool_registry(config, None, "reader")

    assert "write_file" in registry
    assert registry.call("write_file", {"path": "x", "content": "y"})[1] is True
    assert not (config.home / "x").exists()


def test_mods_are_a_separate_thing_from_the_allowlist(config):
    """A mod can refuse a tool the toolset allows; both have to agree."""
    mods = Mods([type("m", (), {"before_tool": staticmethod(lambda n, a: Deny("no"))})])
    registry, _ = registry_with(mods)
    assert "echo" in registry  # allowed by the toolset
    assert registry.call("echo", {"text": "hi"})[1] is True  # refused by the mod
