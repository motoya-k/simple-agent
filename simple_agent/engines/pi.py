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

The process handling is shared with the other external engines; see
:mod:`.external`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from ..loop import Turn
from ..session import session_slug
from .external import ExternalEngine, last_user_text, text_of

if TYPE_CHECKING:
    from ..agent import Agent

BRIDGE = Path(__file__).with_name("pi_bridge.ts")

__all__ = ["PiEngine", "BRIDGE", "last_user_text"]


class PiEngine(ExternalEngine):
    name = "pi"
    default_command = "pi"
    tool_map = {
        "terminal": ("bash",),
        "read_file": ("read",),
        "write_file": ("write",),
        "edit_file": ("edit",),
    }
    provider_map = {
        "anthropic": "anthropic",
        "bedrock": "amazon-bedrock",
        "gemini": "google",
        "openai": "openai",
    }

    def build_command(self, agent: "Agent", prompt: str) -> list[str]:
        session_dir = agent.config.home / "pi"
        session_dir.mkdir(parents=True, exist_ok=True)
        builtin, bridged = self.builtin_tools(agent), self.bridged_tools(agent)
        model: list[str] = []
        if not self.has_flag("--provider"):
            model += ["--provider", self.provider_map[agent.config.provider]]
        if not self.has_flag("--model") and agent.config.model:
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

    def environment(self, agent: "Agent") -> dict[str, str]:
        """pi has no MCP client; pi_bridge.ts starts our server from this."""
        bridged = self.bridged_tools(agent)
        return {
            **super().environment(agent),
            "SIMPLE_AGENT_MCP_COMMAND": json.dumps(self.mcp_server(agent)) if bridged else "[]",
        }

    # Kept for callers and tests that ask for the bridge's environment by name.
    bridge_env = environment

    def read_event(self, event: dict[str, Any], turn: Turn, emit: Callable[[str, str], None]) -> str | None:
        kind = event.get("type")
        if kind == "tool_execution_start":
            turn.tool_calls += 1
            emit("tool", str(event.get("toolName", "")))
        elif kind == "turn_end":
            turn.iterations += 1
        elif kind == "message_end":
            message = event.get("message") or {}
            if message.get("role") == "assistant":
                usage = message.get("usage") or {}
                turn.tokens += int(usage.get("input", 0) or 0) + int(usage.get("output", 0) or 0)
                return text_of(message.get("content")) or None
        return None
