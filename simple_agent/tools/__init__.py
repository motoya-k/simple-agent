"""Tool registry — the edge of the narrow waist.

A tool is a plain Python function plus a JSON schema.  Registering one never
touches the loop; the loop only ever asks the registry for ``schemas()`` and
``call(name, args)``.

``parallel_safe`` is the one piece of metadata the loop cares about: read-only
tools fan out across a thread pool, anything that mutates state runs serially in
the order the model asked for it.

:meth:`ToolRegistry.call` is the only way a tool call becomes an action — the
loop, the MCP server and the REPL's slash commands all go through it — so it
is also where mods get their say.  A rule written once holds no matter who
asked; see :mod:`simple_agent.mods`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    fn: Callable[..., str]
    parallel_safe: bool = False

    def schema(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.parameters,
        }


class ToolRegistry:
    def __init__(self, mods: Any = None) -> None:
        self._tools: dict[str, Tool] = {}
        # Policy between the model and the tools. None = nothing to ask.
        self.mods = mods

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def tool(
        self,
        name: str,
        description: str,
        parameters: dict[str, Any],
        *,
        parallel_safe: bool = False,
    ):
        def decorator(fn: Callable[..., str]) -> Callable[..., str]:
            self.register(Tool(name, description, parameters, fn, parallel_safe))
            return fn

        return decorator

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def schemas(self) -> list[dict[str, Any]]:
        return [tool.schema() for tool in self._tools.values()]

    def subset(self, names: list[str]) -> "ToolRegistry":
        """A registry with only some tools — how the reviewer gets narrow powers.

        Entries may be shell-style patterns, so a route can allow a whole MCP
        server's read tools (``google__*_list``) without naming each one.
        """
        from fnmatch import fnmatchcase

        # The clone keeps the mods: narrowing which tools exist must not
        # quietly drop the rules about calling them.
        clone = ToolRegistry(self.mods)
        for name, tool in self._tools.items():
            if any(fnmatchcase(name, pattern) for pattern in names):
                clone.register(tool)
        return clone

    def call(self, name: str, arguments: dict[str, Any]) -> tuple[str, bool]:
        """Run a tool. Returns ``(output, is_error)``; never raises.

        A refusal from a mod comes back as an ordinary error result, so the
        model reads why it could not do that and carries on, rather than the
        turn ending in a way it cannot account for.
        """
        tool = self._tools.get(name)
        if tool is None:
            return f"Unknown tool: {name}", True
        if self.mods:
            arguments, denial = self.mods.before_tool(name, arguments)
            if denial:
                return denial, True
        output, is_error = self._run(tool, arguments)
        if self.mods:
            output = self.mods.after_tool(name, arguments, output, is_error)
        return output, is_error

    def _run(self, tool: Tool, arguments: dict[str, Any]) -> tuple[str, bool]:
        try:
            result = tool.fn(**arguments)
        except TypeError as exc:
            return f"Bad arguments for {tool.name}: {exc}", True
        except Exception as exc:  # a failing tool is a result, not a crash
            return f"{type(exc).__name__}: {exc}", True
        if not isinstance(result, str):
            result = json.dumps(result, ensure_ascii=False, default=str)
        return result, False


def build_registry(config, memory, skills, store, mods: Any = None) -> ToolRegistry:
    """Assemble the default toolset. ``memory`` is long-term memory."""
    from . import files, memory_tool, session_search, skill_tool, terminal

    registry = ToolRegistry(mods)
    terminal.register(registry, config)
    files.register(registry)
    memory_tool.register(registry, memory)
    skill_tool.register(registry, skills)
    session_search.register(registry, store)

    from ..mcp_client import register_servers

    register_servers(registry, config.home / "mcp.json")

    disabled = [p.strip() for p in getattr(config, "disabled_tools", "").split(",") if p.strip()]
    if disabled:
        from fnmatch import fnmatchcase

        registry = registry.subset([n for n in registry.names() if not any(fnmatchcase(n, p) for p in disabled)])
    return registry
