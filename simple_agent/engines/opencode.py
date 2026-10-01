"""OpenCode as the engine — ``opencode run --format json``.

https://github.com/sst/opencode — an open-source (MIT) agent with broad
provider support, including Bedrock with SSO and IAM roles.

OpenCode takes its per-run settings as inline JSON in
``OPENCODE_CONFIG_CONTENT``, merged over the user's own config, so everything
the agent decides goes there:

* **Tools.**  ``"*": false`` first, then only the mapped built-ins and our MCP
  server's tools switched back on.  Nothing outside the agent's (possibly
  narrowed) registry is callable.
* **Memory and skills.**  Our MCP server as a ``local`` MCP entry.
* **System prompt.**  Written to a file under ``<home>/opencode/`` and listed
  in ``instructions``, which OpenCode appends to its own prompt.
* **Model.**  ``-m <provider>/<model>``, e.g. ``amazon-bedrock/jp.anthropic…``.
* **Session.**  OpenCode picks the id; the first turn's ``sessionID`` is
  stored and passed back with ``--session`` on later turns.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Callable

from ..loop import Turn
from ..session import session_slug
from .external import ExternalEngine

if TYPE_CHECKING:
    from ..agent import Agent

MCP_NAME = "simple-agent"


class OpenCodeEngine(ExternalEngine):
    name = "opencode"
    default_command = "opencode"
    tool_map = {
        "terminal": ("bash",),
        "read_file": ("read", "glob", "grep", "list"),
        "write_file": ("write",),
        "edit_file": ("edit",),
    }
    provider_map = {
        "anthropic": "anthropic",
        "bedrock": "amazon-bedrock",
        "gemini": "google",
        "openai": "openai",
    }

    def __init__(self, command: str = "", args: str = "") -> None:
        super().__init__(command, args)
        self._pending = ""
        self._session = ""

    def _dir(self, agent: "Agent"):
        path = agent.config.home / "opencode"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _id_file(self, agent: "Agent"):
        return self._dir(agent) / f"{session_slug(agent.session_key)}.id"

    def build_command(self, agent: "Agent", prompt: str) -> list[str]:
        self._pending, self._session = "", ""
        command = [*self.command, "run", "--format", "json"]
        id_file = self._id_file(agent)
        if id_file.exists():
            command += ["--session", id_file.read_text().strip()]
        if not self.has_flag("-m", "--model"):
            provider = self.provider_map[agent.config.provider]
            command += ["--model", f"{provider}/{agent.config.model}"]
        return [*command, *self.args, prompt]

    def environment(self, agent: "Agent") -> dict[str, str]:
        system = self._dir(agent) / f"{session_slug(agent.session_key)}.system.md"
        system.write_text(agent.system, encoding="utf-8")

        tools: dict[str, bool] = {"*": False}
        tools.update({name: True for name in self.builtin_tools(agent)})
        config: dict[str, Any] = {"tools": tools, "instructions": [str(system)]}
        if self.bridged_tools(agent):
            tools[f"{MCP_NAME}_*"] = True
            server = self.mcp_server(agent)
            config["mcp"] = {
                MCP_NAME: {
                    "type": "local",
                    "command": server,
                    "environment": {"SIMPLE_AGENT_HOME": str(agent.config.home)},
                }
            }
        return {**super().environment(agent), "OPENCODE_CONFIG_CONTENT": json.dumps(config)}

    def after_success(self, agent: "Agent") -> None:
        if self._session and not self._id_file(agent).exists():
            self._id_file(agent).write_text(self._session)

    def read_event(self, event: dict[str, Any], turn: Turn, emit: Callable[[str, str], None]) -> str | None:
        self._session = self._session or str(event.get("sessionID") or "")
        kind = event.get("type")
        part = event.get("part") or {}
        if kind == "text":
            self._pending += part.get("text", "")
            return self._pending or None
        if kind == "tool_use":
            turn.tool_calls += 1
            emit("tool", str(part.get("tool", "")))
            self._pending = ""  # what follows is a new answer
        elif kind == "step_start":
            turn.iterations += 1
            self._pending = ""  # each step's text replaces the last one's
        elif kind == "step_finish":
            tokens = part.get("tokens") or {}
            turn.tokens += int(tokens.get("input", 0) or 0) + int(tokens.get("output", 0) or 0)
        elif kind == "error":
            raise RuntimeError(f"opencode: {event.get('error')}")
        return None
