"""The shared half of every engine that runs an external harness as a subprocess.

Each such engine answers three questions — how to call the harness for one
turn, how to read its event stream, and how to translate this agent's choices
into its flags — and inherits everything else from :class:`ExternalEngine`:
starting the process, streaming events, interrupting it, reporting failures,
and appending the answer to the transcript.

The rules every external engine follows (see :mod:`simple_agent.engines`):

* The configured provider/model is translated, never configured twice.
* Tools come only from the agent's registry, possibly narrowed, and are never
  widened: file and shell tools map onto the harness's built-ins; the rest
  (memory, skills, session search) arrive over ``simple-agent --mcp``.
* An unsupported provider fails when the agent is built, via ``supports``.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import tempfile
from typing import TYPE_CHECKING, Any, Callable

from ..loop import Turn
from . import Engine, EventCallback

if TYPE_CHECKING:
    from ..agent import Agent


class ExternalEngine(Engine):
    #: simple-agent tool name -> the harness's built-in tool name(s).
    tool_map: dict[str, tuple[str, ...]] = {}
    #: simple-agent provider name -> the harness's provider name.
    provider_map: dict[str, str] = {}
    default_command = ""

    def __init__(self, command: str = "", args: str = "") -> None:
        self.command = shlex.split(command) or shlex.split(self.default_command)
        self.args = shlex.split(args)

    def supports(self, provider: str) -> bool:
        return provider in self.provider_map

    # -- what each engine defines ----------------------------------------
    def build_command(self, agent: "Agent", prompt: str) -> list[str]:
        raise NotImplementedError

    def environment(self, agent: "Agent") -> dict[str, str]:
        return {
            **os.environ,
            "SIMPLE_AGENT_HOME": str(agent.config.home),
            "SIMPLE_AGENT_MCP_SESSION_KEY": agent.session_key,
        }

    def read_event(self, event: dict[str, Any], turn: Turn, emit: Callable[[str, str], None]) -> str | None:
        """Update ``turn`` from one event; return the answer text if it carries one."""
        raise NotImplementedError

    def after_success(self, agent: "Agent") -> None:
        """Hook for engines that must remember something once a turn succeeded."""

    # -- shared helpers ---------------------------------------------------
    def builtin_tools(self, agent: "Agent") -> list[str]:
        names = agent.registry.names()
        return sorted({t for n in names for t in self.tool_map.get(n, ())})

    def bridged_tools(self, agent: "Agent") -> list[str]:
        return [n for n in agent.registry.names() if n not in self.tool_map]

    def mcp_server(self, agent: "Agent") -> list[str]:
        """The argv that serves this agent's bridged tools, same allowlist."""
        return [
            sys.executable, "-m", "simple_agent", "--mcp",
            "--tools", ",".join(self.bridged_tools(agent)),
            "--session-key", agent.session_key,
        ]

    def has_flag(self, *flags: str) -> bool:
        """Whether the user's pass-through args already set one of ``flags``."""
        return any(a == f or a.startswith(f + "=") for a in self.args for f in flags)

    # -- the run ----------------------------------------------------------
    def run(self, agent: "Agent", *, on_event: EventCallback, interrupt: Callable[[], bool]) -> Turn:
        turn = Turn()

        def emit(kind: str, text: str) -> None:
            turn.events.append((kind, text))
            if on_event:
                on_event(kind, text)

        answer = ""
        # stderr goes to a file, not a pipe: a harness that logs a lot while we
        # are busy reading stdout would otherwise fill the pipe and stall.
        with tempfile.TemporaryFile("w+") as stderr:
            process = subprocess.Popen(
                self.build_command(agent, last_user_text(agent.messages)),
                cwd=agent.cwd,
                env=self.environment(agent),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=stderr,
                text=True,
            )
            assert process.stdout is not None
            for line in process.stdout:
                if interrupt():
                    process.terminate()
                    turn.stopped_by = "interrupt"
                    break
                text = self.read_event(_json(line), turn, emit)
                if text:
                    answer = text
            code = process.wait()
            stderr.seek(0)
            detail = stderr.read().strip()[-2000:]

        if turn.stopped_by != "interrupt":
            if code != 0:
                raise RuntimeError(f"{self.name} exited with {code}: {detail}")
            self.after_success(agent)
        turn.text = answer
        # Keep the transcript valid for our side: every user turn gets an
        # assistant turn, even when the harness was stopped before it answered.
        agent.messages.append(
            {"role": "assistant", "content": [{"type": "text", "text": answer or "(no answer)"}]}
        )
        return turn


def last_user_text(messages: list[dict[str, Any]]) -> str:
    """The text of the last user turn — tool results are ours, not the harness's."""
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content
        return "\n".join(b.get("text", "") for b in content or [] if b.get("type") == "text")
    return ""


def text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    return "".join(b.get("text", "") for b in content or [] if isinstance(b, dict) and b.get("type") == "text")


def _json(line: str) -> dict[str, Any]:
    try:
        value = json.loads(line)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}
