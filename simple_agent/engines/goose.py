"""Goose as the engine — ``goose run --output-format stream-json``.

https://github.com/aaif-goose/goose — a general-purpose agent built around MCP
("extensions"), used well beyond coding.  Our memory, skills, and session
search attach with ``--with-extension``; ``--no-profile`` keeps the user's own
default extensions out of the agent's turns.

Translation from this agent's choices:

* **Model.**  ``--provider`` / ``--model``; ``bedrock`` maps to Goose's
  ``aws_bedrock``, which reads the same ``AWS_PROFILE`` / ``AWS_REGION``.
* **Tools.**  Goose's file and shell tools come as one bundle, the
  ``developer`` builtin, so it cannot express "read files but no shell".  The
  bundle is used only when the agent may use all four of our file and shell
  tools; otherwise our own implementations of whichever are allowed go over
  MCP with everything else.  Either way nothing is widened.
* **Session.**  A stable name derived from the session key: ``--name`` on the
  first turn, ``--resume --name`` after that.
"""

from __future__ import annotations

import shlex
from typing import TYPE_CHECKING, Any, Callable

from ..loop import Turn
from ..session import session_slug
from .external import ExternalEngine

if TYPE_CHECKING:
    from ..agent import Agent

DEVELOPER = frozenset({"terminal", "read_file", "write_file", "edit_file"})


class GooseEngine(ExternalEngine):
    name = "goose"
    default_command = "goose"
    provider_map = {"anthropic": "anthropic", "bedrock": "aws_bedrock", "openai": "openai"}

    def __init__(self, command: str = "", args: str = "") -> None:
        super().__init__(command, args)
        self._pending = ""  # assistant text since the last tool result

    def builtin_tools(self, agent: "Agent") -> list[str]:
        return ["developer"] if DEVELOPER <= set(agent.registry.names()) else []

    def bridged_tools(self, agent: "Agent") -> list[str]:
        names = agent.registry.names()
        if DEVELOPER <= set(names):
            return [n for n in names if n not in DEVELOPER]
        return list(names)

    def _marker(self, agent: "Agent"):
        return agent.config.home / "goose" / f"{self._name(agent)}.started"

    def _name(self, agent: "Agent") -> str:
        return session_slug(agent.session_key)

    def build_command(self, agent: "Agent", prompt: str) -> list[str]:
        self._pending = ""
        resume = self._marker(agent).exists()
        command = [
            *self.command,
            "run",
            "--output-format", "stream-json",
            "--no-profile",
            *(["--resume"] if resume else []),
            "--name", self._name(agent),
            "--system", agent.system,
        ]
        # A resumed session restores its extensions, and adding one again is
        # an error ("extension name ... is already in use"). The toolset of a
        # conversation does not change between turns, so the restored one is
        # the right one.
        if not resume:
            for builtin in self.builtin_tools(agent):
                command += ["--with-builtin", builtin]
        if not resume and self.bridged_tools(agent):
            # One command string; the session key travels in the environment.
            server = self.mcp_server(agent)
            server = server[: server.index("--session-key")]
            command += ["--with-extension", "simple-agent:" + shlex.join(server)]
        if not self.has_flag("--provider"):
            command += ["--provider", self.provider_map[agent.config.provider]]
        if not self.has_flag("--model") and agent.config.model:
            command += ["--model", agent.config.model]
        return [*command, *self.args, "--text", prompt]

    def after_success(self, agent: "Agent") -> None:
        marker = self._marker(agent)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch()

    def read_event(self, event: dict[str, Any], turn: Turn, emit: Callable[[str, str], None]) -> str | None:
        kind = event.get("type")
        if kind == "message":
            message = event.get("message") or {}
            for block in message.get("content") or []:
                if block.get("type") == "toolRequest":
                    turn.tool_calls += 1
                    call = (block.get("toolCall") or {}).get("value") or {}
                    emit("tool", str(call.get("name", "")))
                elif block.get("type") == "toolResponse":
                    self._pending = ""  # what follows is a new answer
                elif block.get("type") == "text" and message.get("role") == "assistant":
                    # The reply streams as several messages; join them.
                    self._pending += block.get("text", "")
            if message.get("role") == "assistant":
                turn.iterations += 1
            return self._pending or None
        if kind == "complete":
            turn.tokens += int(event.get("total_tokens", 0) or 0)
        elif kind == "error":
            raise RuntimeError(f"goose: {event.get('error')}")
        return None
