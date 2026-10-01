"""Claude Code as the engine — ``claude -p --output-format stream-json``.

A general-purpose harness, not only a coding one: its loop, context handling,
and built-in file/shell/search tools are strong, and it speaks MCP natively, so
our memory, skills, and session search attach with ``--mcp-config`` and no
bridge.

Translation from this agent's choices:

* **Model.**  ``anthropic`` passes ``--model``; ``bedrock`` also sets
  ``CLAUDE_CODE_USE_BEDROCK=1`` and the region, and Claude Code picks up the
  same AWS credentials (``AWS_PROFILE`` or keys) the Bedrock provider uses.
  Other providers are not supported and fail at start.
* **Tools.**  Built-ins are limited with ``--tools``, and every allowed tool is
  pre-approved with ``--allowedTools`` under ``--permission-mode dontAsk``, so
  a headless turn never stops on a permission prompt and never uses anything
  outside the list.  ``--strict-mcp-config`` ignores MCP servers from the
  user's own configuration.
* **Settings.**  ``--setting-sources ""`` keeps the user's personal Claude
  Code settings — hooks, plugins, permissions — out of the agent's turns.
  Pass ``--setting-sources`` in ``claude_args`` to opt back in.
* **Session.**  Each conversation gets a stable UUID derived from its session
  key: ``--session-id`` on the first turn, ``--resume`` after that.
"""

from __future__ import annotations

import json
import uuid
from typing import TYPE_CHECKING, Any, Callable

from ..loop import Turn
from .external import ExternalEngine, text_of

if TYPE_CHECKING:
    from ..agent import Agent

MCP_NAME = "simple-agent"
_NAMESPACE = uuid.UUID("5b1c1f5e-6a1d-4c35-9d4c-6d2b8a7e2f10")


class ClaudeCodeEngine(ExternalEngine):
    name = "claude-code"
    default_command = "claude"
    tool_map = {
        "terminal": ("Bash",),
        # Glob and Grep only read, so they sit with read_file.
        "read_file": ("Read", "Glob", "Grep"),
        "write_file": ("Write",),
        "edit_file": ("Edit",),
    }
    provider_map = {"anthropic": "anthropic", "bedrock": "bedrock"}

    def session_id(self, agent: "Agent") -> str:
        # The home is part of it: two agent homes on one machine share Claude
        # Code's session store, and must not share conversations.
        return str(uuid.uuid5(_NAMESPACE, f"{agent.config.home}\n{agent.session_key}"))

    def _marker(self, agent: "Agent"):
        return agent.config.home / "claude-code" / f"{self.session_id(agent)}.started"

    def build_command(self, agent: "Agent", prompt: str) -> list[str]:
        builtin, bridged = self.builtin_tools(agent), self.bridged_tools(agent)
        allowed = builtin + [f"mcp__{MCP_NAME}__{name}" for name in bridged]
        session = self.session_id(agent)
        command = [
            *self.command,
            "-p",
            "--output-format", "stream-json",
            "--verbose",  # stream-json requires it under -p
            *(["--resume", session] if self._marker(agent).exists() else ["--session-id", session]),
            "--tools", ",".join(builtin),  # "" disables every built-in
            "--permission-mode", "dontAsk",
            "--strict-mcp-config",
            "--append-system-prompt", agent.system,
        ]
        if allowed:
            command += ["--allowedTools", ",".join(allowed)]
        if bridged:
            server = self.mcp_server(agent)
            config = {"mcpServers": {MCP_NAME: {
                "command": server[0], "args": server[1:], "env": self.mcp_env(agent),
            }}}
            command += ["--mcp-config", json.dumps(config)]
        if not self.has_flag("--setting-sources"):
            command += ["--setting-sources", ""]
        if not self.has_flag("--model") and agent.config.model:
            command += ["--model", agent.config.model]
        return [*command, *self.args, prompt]

    def environment(self, agent: "Agent") -> dict[str, str]:
        env = super().environment(agent)
        if agent.config.provider == "bedrock":
            env["CLAUDE_CODE_USE_BEDROCK"] = "1"
            env.setdefault("AWS_REGION", env.get("AWS_DEFAULT_REGION") or _bedrock_region())
        return env

    def run(self, agent: "Agent", *, on_event, interrupt):
        try:
            return super().run(agent, on_event=on_event, interrupt=interrupt)
        except RuntimeError as exc:
            # Our "already started" marker is gone (a new container, a wiped
            # home) but Claude Code still has the session: resume it.
            if "already in use" not in str(exc) or self._marker(agent).exists():
                raise
            self.after_success(agent)
            return super().run(agent, on_event=on_event, interrupt=interrupt)

    def after_success(self, agent: "Agent") -> None:
        marker = self._marker(agent)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch()

    def read_event(self, event: dict[str, Any], turn: Turn, emit: Callable[[str, str], None]) -> str | None:
        kind = event.get("type")
        if kind == "assistant":
            turn.iterations += 1
            for block in (event.get("message") or {}).get("content") or []:
                if block.get("type") == "tool_use":
                    turn.tool_calls += 1
                    emit("tool", str(block.get("name", "")))
        elif kind == "result":
            usage = event.get("usage") or {}
            turn.tokens += int(usage.get("input_tokens", 0) or 0) + int(usage.get("output_tokens", 0) or 0)
            if event.get("is_error"):
                raise RuntimeError(f"claude-code: {event.get('result') or event.get('subtype')}")
            return str(event.get("result") or "") or None
        return None


def _bedrock_region() -> str:
    from ..providers.bedrock import DEFAULT_REGION
    from ..providers.sigv4 import profile_region

    return profile_region() or DEFAULT_REGION


__all__ = ["ClaudeCodeEngine", "text_of"]
