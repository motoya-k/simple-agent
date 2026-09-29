"""pi as the engine — hand the turn to https://github.com/badlogic/pi-mono.

Each turn runs ``pi -p --mode json`` as a subprocess and reads its event stream.
pi keeps its own session file per conversation (under ``<home>/pi/``), so it
resumes with its own tool history; simple-agent's transcript records the
user's message and pi's final answer, which is what search, compaction and the
background review need.

pi takes over only the loop and its context handling.  Everything else is
still chosen here, once, and handed over:

* **The model.**  ``provider`` and ``model`` are translated into pi's
  ``--provider`` / ``--model``, so switching the harness does not mean
  configuring the LLM twice.  ``pi_args`` can still override them.
* **Tools and memory.**  The agent's file and shell tools map onto pi's
  built-ins; the rest — memory, skills, session search — reach pi through
  ``pi_bridge.ts``, an extension that talks to ``simple-agent --mcp``.  So pi
  reads and writes the same memory backend the loop would.
* **What is allowed.**  Both halves come from the agent's (possibly narrowed)
  registry and are never widened.  A route that took the terminal away keeps
  it away under pi too.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from ..loop import Turn
from ..session import session_slug
from . import Engine, EventCallback

if TYPE_CHECKING:
    from ..agent import Agent

# simple-agent tool name -> pi built-in tool name.
TOOL_MAP = {
    "terminal": "bash",
    "read_file": "read",
    "write_file": "write",
    "edit_file": "edit",
}

# simple-agent provider name -> pi provider name.
PROVIDER_MAP = {
    "anthropic": "anthropic",
    "bedrock": "amazon-bedrock",
    "gemini": "google",
    "openai": "openai",
}

BRIDGE = Path(__file__).with_name("pi_bridge.ts")


class PiEngine(Engine):
    name = "pi"

    def __init__(self, command: str = "pi", args: str = "") -> None:
        self.command = shlex.split(command) or ["pi"]
        self.args = shlex.split(args)

    def supports(self, provider: str) -> bool:
        return provider in PROVIDER_MAP

    def build_command(self, agent: "Agent", prompt: str) -> list[str]:
        session_dir = agent.config.home / "pi"
        session_dir.mkdir(parents=True, exist_ok=True)
        names = agent.registry.names()
        builtin = sorted({TOOL_MAP[n] for n in names if n in TOOL_MAP})
        bridged = [n for n in names if n not in TOOL_MAP]
        model: list[str] = []
        if "--provider" not in self.args:
            model += ["--provider", PROVIDER_MAP[agent.config.provider]]
        if "--model" not in self.args and agent.config.model:
            model += ["--model", agent.config.model]
        return [
            *self.command,
            "-p",
            "--mode", "json",
            "--session", str(session_dir / f"{session_slug(agent.session_key)}.jsonl"),
            # --tools also filters extension tools, so the bridged ones are
            # listed too; with nothing allowed at all, pi gets no tools.
            *(["--tools", ",".join(builtin + bridged)] if builtin or bridged else ["--no-tools"]),
            *(["-e", str(BRIDGE)] if bridged else []),
            "--append-system-prompt", agent.system,
            *model,
            *self.args,
            prompt,
        ]

    def bridge_env(self, agent: "Agent") -> dict[str, str]:
        """How the bridge extension starts our MCP server, with the same allowlist."""
        bridged = [n for n in agent.registry.names() if n not in TOOL_MAP]
        server = [
            sys.executable, "-m", "simple_agent", "--mcp",
            "--tools", ",".join(bridged),
            "--session-key", agent.session_key,
        ]
        return {
            **os.environ,
            "SIMPLE_AGENT_HOME": str(agent.config.home),
            "SIMPLE_AGENT_MCP_COMMAND": json.dumps(server) if bridged else "[]",
        }

    def run(self, agent: "Agent", *, on_event: EventCallback, interrupt: Callable[[], bool]) -> Turn:
        turn = Turn()
        command = self.build_command(agent, last_user_text(agent.messages))
        process = subprocess.Popen(
            command,
            cwd=agent.cwd,
            env=self.bridge_env(agent),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        answer = ""
        assert process.stdout is not None
        for line in process.stdout:
            if interrupt():
                process.terminate()
                turn.stopped_by = "interrupt"
                break
            event = _json(line)
            kind = event.get("type")
            if kind == "tool_execution_start":
                turn.tool_calls += 1
                _emit(turn, on_event, "tool", str(event.get("toolName", "")))
            elif kind == "turn_end":
                turn.iterations += 1
            elif kind == "message_end":
                message = event.get("message") or {}
                if message.get("role") == "assistant":
                    answer = assistant_text(message) or answer
                    turn.tokens += _tokens(message)
        stderr = process.stderr.read() if process.stderr else ""
        code = process.wait()

        if turn.stopped_by != "interrupt" and code != 0:
            raise RuntimeError(f"pi exited with {code}: {stderr.strip()[:2000]}")
        turn.text = answer
        # Keep the transcript valid for our side: every user turn gets an
        # assistant turn, even when pi was stopped before it said anything.
        agent.messages.append(
            {"role": "assistant", "content": [{"type": "text", "text": answer or "(no answer)"}]}
        )
        return turn


def last_user_text(messages: list[dict[str, Any]]) -> str:
    """The text of the last user turn — tool results are ours, not pi's."""
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content
        return "\n".join(b.get("text", "") for b in content or [] if b.get("type") == "text")
    return ""


def assistant_text(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    return "".join(b.get("text", "") for b in content or [] if b.get("type") == "text")


def _tokens(message: dict[str, Any]) -> int:
    usage = message.get("usage") or {}
    return int(usage.get("input", 0) or 0) + int(usage.get("output", 0) or 0)


def _json(line: str) -> dict[str, Any]:
    try:
        value = json.loads(line)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _emit(turn: Turn, on_event: EventCallback, kind: str, text: str) -> None:
    turn.events.append((kind, text))
    if on_event:
        on_event(kind, text)
