"""Claude Code as the engine; this repo as everything around it.

``simple-agent --mcp`` lends another harness *this agent's tools*.  This is the
other direction: the harness is Claude Code, and what surrounds it — who the
agent is on each route, where messages arrive, where answers go, and where the
conversation is kept — is still this repo.  Nothing in ``simple_agent`` imports
this file; it is an example of the seams, not part of them.

What gets replaced:

* ``Agent`` and :func:`~simple_agent.loop.run_conversation` → ``claude -p``.
  The model call, the tool-calling loop, retries and compaction are Claude
  Code's problem now.
* ``providers/`` → Claude Code's own model configuration (``ANTHROPIC_API_KEY``,
  or ``CLAUDE_CODE_USE_BEDROCK=1`` and a task role).
* ``tools/`` and ``~/.simple-agent/mcp.json`` → the MCP config rendered here and
  handed over with ``--mcp-config``.
* long-term memory → Hindsight, over MCP, one memory bank per namespace.

What does not, and is why this is one file rather than a fork:
:class:`~simple_agent.host.Host` (concurrency, draining, dead letters), the
Sources and Sinks, :class:`~simple_agent.profile.Profile`, the session key, and
the transcript store.

The engine is told to keep nothing: ``--no-session-persistence``, and the
transcript comes back out of Postgres on every turn.  See :meth:`_prompt`.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from pathlib import Path
from string import Template
from typing import Any, Iterable, Mapping

from simple_agent.loop import Turn
from simple_agent.profile import Profile
from simple_agent.session import SessionSource
from simple_agent.state import Store, open_store

log = logging.getLogger(__name__)

#: The MCP server name this repo is declared under in ``mcp.json``.  Claude
#: Code names a tool ``mcp__<server>__<tool>``, so this string is half of the
#: name of every built-in tool, and the file and this module have to agree.
LOCAL_SERVER = "simple_agent"

#: Tools that write what *other* conversations later read: this agent's own
#: memory and skills, and Hindsight's ``retain``.  ``learning: false`` has to
#: mean exactly these, or a route anyone can write to could leave a note that a
#: trusted session reads back as the team's own knowledge.  A memory server
#: added to ``mcp.json`` belongs in this tuple the same day.
LEARNING_TOOLS = (
    f"mcp__{LOCAL_SERVER}__memory_save",
    f"mcp__{LOCAL_SERVER}__skill_manage",
    "mcp__hindsight__retain",
)

#: ``${NAME}`` in the MCP template. Nothing else is substituted.
_VARIABLE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

#: Credentials ``--bare`` can use. It also refuses every other kind — no
#: keychain, no OAuth login nobody is there to perform — so it is right in a
#: container and wrong on the laptop of someone who is logged in
#: interactively, where it fails with "Not logged in". Hence: on when there is
#: a key to use, off when there is not.
BARE_CREDENTIALS = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
)

#: Messages of history handed to the engine. The engine cannot compact what it
#: does not keep, so the window is ours to choose; a Slack thread is rarely
#: long and mail never is.
TRANSCRIPT_MESSAGES = 40

#: A turn that has not finished by now is not going to. Longer than the host's
#: SIGTERM grace on purpose: a deploy interrupts a turn in flight after 90s and
#: the message, never acknowledged, is replayed by the next task — a wedged
#: engine does not get to hold a deploy open for ten minutes.
TURN_TIMEOUT = 600.0


class ClaudeCodeAgent:
    """One conversation, answered by ``claude -p``.

    Implements the three things :class:`~simple_agent.host.Host` asks of an
    agent — ``run``, ``interrupt``, and (for
    :class:`~simple_agent.registry.AgentRegistry`) a ``store`` and a
    ``session_id`` so the registry can tell when somebody else has written to
    this conversation.

    ``mcp_config`` is a path, not JSON: a config holds tokens, and an argument
    is readable by every process on the machine.
    """

    def __init__(
        self,
        config: Any,
        *,
        session_key: str,
        source: SessionSource,
        profile: Profile,
        mcp_config: Path,
        servers: Iterable[str],
        store: Store | None = None,
        binary: str = "",
        model: str = "",
        max_usd: str = "",
        timeout: float = TURN_TIMEOUT,
    ) -> None:
        self.config = config
        self.profile = profile
        self.source = source
        self.session_key = session_key
        self.mcp_config = mcp_config
        self.timeout = timeout
        self.binary = binary or os.environ.get("CLAUDE_BINARY", "claude")
        self.model = model or os.environ.get("CLAUDE_MODEL", "")
        self.max_usd = max_usd or os.environ.get("CLAUDE_MAX_BUDGET_USD", "")

        self.store = store or open_store(config)
        # Pick the conversation back up. Without this a restarted host starts
        # every thread over, which reads to the people in it as the agent
        # having forgotten the last hour.
        self.session_id = self.store.latest_session_for_key(session_key) or self.store.new_session(
            os.getcwd(), session_key=session_key
        )

        # Where it is goes in the system prompt, as it does for a local Agent:
        # an assistant that knows it is in a shared channel behaves differently
        # from one that thinks it is alone with one person.
        self.system = f"{profile.instructions}\n\nWhere you are: {source.description}."
        # Built here, so a profile that asks for something impossible fails at
        # startup rather than on the first message that arrives.
        self.argv = self._build_argv(allowed_tools(profile, servers))
        self._process: subprocess.Popen | None = None

    # -- one turn -------------------------------------------------------
    def run(self, text: str, on_event=None) -> Turn:
        """Answer one message. Blocking, like ``Agent.run``; the host threads it."""
        self.store.add_message(self.session_id, "user", text)
        prompt = self._prompt()
        proc = subprocess.Popen(
            [*self.argv, "--mcp-config", str(self.mcp_config)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self._process = proc
        try:
            out, err = proc.communicate(prompt, timeout=self.timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, err = proc.communicate()
            raise RuntimeError(f"claude -p gave up after {self.timeout:.0f}s") from None
        finally:
            self._process = None

        result = _parse(out)
        answer = str(result.get("result") or "").strip()
        if proc.returncode != 0 or result.get("is_error") or not answer:
            # The transcript keeps the unanswered question, which is also what
            # the people in the thread can see. The next turn carries it along.
            raise RuntimeError(
                f"claude -p failed (exit {proc.returncode}): "
                f"{result.get('result') or err.strip()[-400:] or 'no output'}"
            )
        denied = result.get("permission_denials") or []
        if denied:
            # Not an error: the allowlist did its job. Worth a line, because a
            # route that keeps reaching for a tool it may not use is either a
            # profile that is too narrow or a prompt that is wrong about itself.
            log.info(
                "%s: %d tool call(s) denied by the allowlist: %s",
                self.profile.name,
                len(denied),
                ", ".join(sorted({str(d.get("tool_name", "?")) for d in denied})),
            )
        self.store.add_message(self.session_id, "assistant", answer)
        usage = result.get("usage") or {}
        return Turn(
            text=answer,
            # Not tool_calls: the engine does not report how many it made, and
            # a number that is actually something else is worse than nothing.
            tokens=int(usage.get("input_tokens", 0)) + int(usage.get("output_tokens", 0)),
            iterations=int(result.get("num_turns") or 1),
        )

    def interrupt(self) -> None:
        """Stop the turn in flight — SIGTERM, so the engine can finish writing."""
        proc = self._process
        if proc is not None and proc.poll() is None:
            proc.terminate()

    # -- the prompt -----------------------------------------------------
    def _prompt(self) -> str:
        """The conversation as text, because the engine keeps no copy of it.

        Two ways to continue a conversation with ``claude -p``, and this is the
        second:

        * ``--resume <id>`` — the engine keeps the transcript on its own disk.
          Cheaper per turn, and the conversation then belongs to one container.
        * ``--no-session-persistence`` and hand the history over each turn —
          Postgres is the only copy, so any container can pick up any
          conversation, and a task that is replaced mid-thread loses nothing.

        A service that runs as more than one task, on disks that do not outlive
        it, needs the second. The cost is the history re-read every turn, which
        is what the window in :data:`TRANSCRIPT_MESSAGES` bounds.
        """
        rows = self.store.conversation(self.session_id)[-TRANSCRIPT_MESSAGES:]
        latest = _plain(rows[-1]["content"]) if rows else ""
        earlier = [
            f"{'assistant' if row['role'] == 'assistant' else 'user'}: {_plain(row['content'])}"
            for row in rows[:-1]
        ]
        if not earlier:
            return latest
        return (
            "This conversation so far, oldest first. You are the assistant:\n\n"
            + "\n\n".join(earlier)
            + "\n\nThe new message, which you are answering now:\n\n"
            + latest
        )

    # -- the command ----------------------------------------------------
    def _build_argv(self, allowed: tuple[str, ...]) -> list[str]:
        argv = [
            self.binary,
            "-p",
            "--output-format",
            "json",
            # The transcript is in Postgres; see _prompt.
            "--no-session-persistence",
            # Only the servers we hand over, whatever else is configured.
            "--strict-mcp-config",
            # Built-ins off: no shell, no file edits, no assumption that this
            # process sits in a repository. The agent acts through MCP only.
            "--tools",
            "",
            # Nobody is at a keyboard. Anything outside the allowlist is denied
            # instead of waiting for an answer that will never come.
            "--permission-prompts",
            "none",
            # Replaces Claude Code's own prompt rather than appending to it:
            # this agent answers mail and Slack, and the profile says how.
            "--system-prompt",
            self.system,
        ]
        if _bare():
            # No CLAUDE.md, no hooks, no auto-memory, no user settings: a
            # server is not somebody's laptop, and what this agent may do is
            # the profile, not whatever is in the image's home directory.
            argv.append("--bare")
        if allowed:
            argv += ["--allowedTools", ",".join(allowed)]
        if self.model:
            argv += ["--model", self.model]
        if self.max_usd:
            argv += ["--max-budget-usd", self.max_usd]
        return argv


# -- profile → allowlist ------------------------------------------------
def allowed_tools(
    profile: Profile, servers: Iterable[str], local_server: str = LOCAL_SERVER
) -> tuple[str, ...]:
    """A profile's toolset in Claude Code's names, with both halves enforced.

    Three translations, each easy to get subtly and silently wrong:

    * an MCP tool is ``<server>__<tool>`` here and ``mcp__<server>__<tool>``
      there;
    * a pattern (``notion__*``) has no equivalent there, so it becomes the
      whole server (``mcp__notion``) — which is what the pattern asked for,
      writes included;
    * a built-in name (``skill_view``) reaches that harness through
      ``simple-agent --mcp``, so it is prefixed with this repo's server name.

    ``tools: '*'`` becomes every declared server rather than no allowlist at
    all: with ``--permission-prompts none`` a missing allowlist is not
    permissive, it is every tool call failing.

    And the half that is not the toolset: a profile with ``learning: false``
    may not hold a tool that writes long-term memory.  Raising here is the
    point — the two halves of :mod:`simple_agent.profile`'s boundary cannot be
    loosened one at a time by accident.
    """
    servers = tuple(servers)
    if profile.tools is None:
        names = tuple(f"mcp__{server}" for server in servers)
    else:
        names = tuple(_translate(name, local_server) for name in profile.tools)
    undeclared = sorted({name.split("__")[1] for name in names} - set(servers))
    if undeclared:
        raise ValueError(
            f"profile {profile.name!r} allows tools from {', '.join(undeclared)}, which "
            f"mcp.json does not declare. A tool that is not there is not an error when it is "
            f"called: it is the model saying it cannot do the thing, in a channel, once a day. "
            f"Add the server or narrow the profile."
        )
    if not profile.learning:
        teaching = [name for name in names if _teaches(name)]
        if teaching:
            raise ValueError(
                f"profile {profile.name!r} has learning: false but allows "
                f"{', '.join(teaching)}. Name the read tools instead "
                f"(hindsight__recall, hindsight__reflect, skill_view), or turn learning on "
                f"knowing that this route can then teach every other conversation."
            )
    return names


def _translate(name: str, local_server: str) -> str:
    server, _, tool = name.partition("__")
    if not tool:  # a built-in: this repo serves it over MCP
        if "*" in name:
            raise ValueError(f"no wildcard for a built-in tool: {name!r}")
        return f"mcp__{local_server}__{name}"
    if tool == "*":
        return f"mcp__{server}"  # the whole server, writes included
    if "*" in tool:
        raise ValueError(
            f"Claude Code allowlists a whole server or an exact tool, and {name!r} is "
            f"neither. Use {server}__* for all of it, or name the tools."
        )
    return f"mcp__{server}__{tool}"


def _teaches(name: str) -> bool:
    """Does this grant include a tool that writes long-term memory or skills?"""
    return any(tool == name or tool.startswith(name + "__") for tool in LEARNING_TOOLS)


# -- the MCP config -----------------------------------------------------
def write_mcp_config(
    template: Path, destination: Path, *, namespace: str, env: Mapping[str, str] | None = None
) -> tuple[Path, tuple[str, ...]]:
    """Render ``mcp.json`` for one namespace and leave it where only we can read it.

    Two substitutions.  ``${VARS}`` come from the environment, so the template
    holds no secret and is safe to commit — the same rule as a profile file.
    ``${namespace}`` is the team: Hindsight keeps one memory bank per URL, so
    the setting that decides whose long-term memory this is ends up *in* the
    URL, and two teams on one deployment share no bank.

    Written to a file rather than passed as ``--mcp-config '{...}'`` because
    arguments are world-readable and this one holds every token.

    A variable the environment does not set is an error, named at startup.  The
    alternative is worse than a crash: the literal string ``${NOTION_TOKEN}``
    reaching Notion as a token, and a server that fails to start being logged
    and skipped — a tool quietly missing for the rest of the deployment.  A
    server you do not run is a server you delete from the template.
    """
    values = dict(env if env is not None else os.environ)
    values["namespace"] = namespace
    raw = template.read_text("utf-8")
    missing = sorted({name for name in _VARIABLE.findall(raw) if name not in values})
    if missing:
        raise ValueError(f"{template} needs these in the environment: {', '.join(missing)}")
    text = Template(raw).substitute(values)
    servers = json.loads(text).get("mcpServers") or {}  # fails at startup, not on a message
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(text, encoding="utf-8")
    destination.chmod(0o600)
    return destination, tuple(servers)


# -- odds and ends ------------------------------------------------------
def _bare() -> bool:
    return any(os.environ.get(name) for name in BARE_CREDENTIALS)



def _parse(out: str) -> dict[str, Any]:
    """The last JSON object on stdout — ``--output-format json`` prints one."""
    for line in reversed(out.strip().splitlines()):
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    return {}


def _plain(content: Any) -> str:
    """A stored message as readable text.

    Ours are always strings, but a transcript written by this repo's own Agent
    under the same session key holds tool calls as blocks, and a conversation
    that was handled by both must still read as a conversation.
    """
    if isinstance(content, str):
        return content
    parts = []
    for block in content or ():
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            parts.append(str(block.get("text", "")))
        elif block.get("type") == "tool_use":
            parts.append(f"[used {block.get('name', 'a tool')}]")
    return "\n".join(part for part in parts if part)
