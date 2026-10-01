"""Hermes Agent as the engine — ``hermes chat -Q -q``.

https://github.com/NousResearch/hermes-agent — the self-improving agent this
repo started as a rewrite of.  Running it as an engine closes the loop: its
own loop and context handling, with this agent's model choice, memory, skills,
and permissions.

Translation from this agent's choices:

* **Isolation.**  ``HERMES_HOME`` is ``<home>/hermes``, written fresh each turn
  with only our MCP server, so the user's own Hermes config, rules, and
  servers stay out (plus ``--ignore-rules``).  Hermes keeps its sessions there.
* **Model.**  ``--provider`` / ``-m``; ``bedrock`` reads the same
  ``AWS_PROFILE`` / ``AWS_REGION``.
* **Tools.**  ``-t`` lists exactly what may load.  Hermes's file tools come as
  one ``file`` toolset (read and write together), so the built-ins are used
  only when all four file/shell tools are allowed; otherwise our own file
  tools go over MCP.  With nothing allowed it gets the empty ``safe`` toolset.
  Hermes's own memory and skills are never enabled: ours are.
* **System prompt.**  ``HERMES_EPHEMERAL_SYSTEM_PROMPT``, which Hermes adds to
  its own and never writes to history.
* **Output.**  Plain text on stdout (no event stream); the session id comes on
  stderr as ``session_id: …`` and is passed back with ``--resume``.

Needs the MCP extra: ``pip install 'hermes-agent[mcp]'``.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any, Callable

from ..loop import Turn
from ..session import session_slug
from .external import ExternalEngine

if TYPE_CHECKING:
    from ..agent import Agent

MCP_NAME = "simple-agent"
BUILTINS = frozenset({"terminal", "read_file", "write_file", "edit_file"})
_SESSION = re.compile(r"^session_id:\s*(\S+)", re.M)
# Lines Hermes prints on stdout that are not the answer.
_NOISE = re.compile(r"^\s*(⚠|Warning: )")


class HermesEngine(ExternalEngine):
    name = "hermes"
    default_command = "hermes"
    provider_map = {"anthropic": "anthropic", "bedrock": "bedrock"}

    def __init__(self, command: str = "", args: str = "") -> None:
        super().__init__(command, args)
        self._lines: list[str] = []

    def builtin_tools(self, agent: "Agent") -> list[str]:
        return ["terminal", "file"] if BUILTINS <= set(agent.registry.names()) else []

    def bridged_tools(self, agent: "Agent") -> list[str]:
        names = agent.registry.names()
        return [n for n in names if n not in BUILTINS] if BUILTINS <= set(names) else list(names)

    def _home(self, agent: "Agent"):
        path = agent.config.home / "hermes"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _id_file(self, agent: "Agent"):
        return self._home(agent) / f"{session_slug(agent.session_key)}.session"

    def build_command(self, agent: "Agent", prompt: str) -> list[str]:
        self._lines = []
        toolsets = self.builtin_tools(agent)
        if self.bridged_tools(agent):
            toolsets.append(f"mcp-{MCP_NAME}")
        command = [
            *self.command, "chat", "-Q",
            "-t", ",".join(toolsets) or "safe",
            "--ignore-rules",
        ]
        if "terminal" in toolsets:
            # Headless: nobody is there to approve a command. The route
            # already decided a shell is allowed at all.
            command.append("--yolo")
        id_file = self._id_file(agent)
        if id_file.exists():
            command += ["--resume", id_file.read_text().strip()]
        if not self.has_flag("--provider"):
            command += ["--provider", self.provider_map[agent.config.provider]]
        if not self.has_flag("-m", "--model") and agent.config.model:
            command += ["-m", agent.config.model]
        return [*command, *self.args, "-q", prompt]

    def environment(self, agent: "Agent") -> dict[str, str]:
        home = self._home(agent)
        servers: dict[str, Any] = {}
        if self.bridged_tools(agent):
            server = self.mcp_server(agent)
            # Hermes does not pass its environment to MCP servers.
            servers[MCP_NAME] = {"command": server[0], "args": server[1:], "env": self.mcp_env(agent)}
        # JSON is valid YAML, so no YAML writer is needed. It may hold the
        # database URL, so only the owner can read it.
        config = home / "config.yaml"
        config.write_text(json.dumps({"mcp_servers": servers}, indent=2), "utf-8")
        config.chmod(0o600)
        return {
            **super().environment(agent),
            "HERMES_HOME": str(home),
            "HERMES_EPHEMERAL_SYSTEM_PROMPT": agent.system,
        }

    def read_line(self, line: str, turn: Turn, emit: Callable[[str, str], None]) -> str | None:
        if not _NOISE.match(line):
            self._lines.append(line.rstrip("\n"))
        return "\n".join(self._lines).strip() or None

    def after_success(self, agent: "Agent") -> None:
        found = _SESSION.search(getattr(self, "stderr", "") or "")
        if found:
            self._id_file(agent).write_text(found.group(1))
