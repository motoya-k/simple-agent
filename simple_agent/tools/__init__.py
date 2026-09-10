"""Tool registry — the edge of the narrow waist.

A tool is a plain Python function plus a JSON schema.  Registering one never
touches the loop; the loop only ever asks the registry for ``schemas()`` and
``call(name, args)``.

``parallel_safe`` is the one piece of metadata the loop cares about: read-only
tools fan out across a thread pool, anything that mutates state runs serially in
the order the model asked for it.
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
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

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
        """A registry with only some tools — how the reviewer gets narrow powers."""
        clone = ToolRegistry()
        for name in names:
            tool = self._tools.get(name)
            if tool is not None:
                clone.register(tool)
        return clone

    def call(self, name: str, arguments: dict[str, Any]) -> tuple[str, bool]:
        """Run a tool. Returns ``(output, is_error)``; never raises."""
        tool = self._tools.get(name)
        if tool is None:
            return f"Unknown tool: {name}", True
        try:
            result = tool.fn(**arguments)
        except TypeError as exc:
            return f"Bad arguments for {name}: {exc}", True
        except Exception as exc:  # a failing tool is a result, not a crash
            return f"{type(exc).__name__}: {exc}", True
        if not isinstance(result, str):
            result = json.dumps(result, ensure_ascii=False, default=str)
        return result, False


def build_registry(config, memory, skills, store) -> ToolRegistry:
    """Assemble the default toolset."""
    from . import files, memory_tool, session_search, skill_tool, terminal

    registry = ToolRegistry()
    terminal.register(registry, config)
    files.register(registry)
    memory_tool.register(registry, memory)
    skill_tool.register(registry, skills)
    session_search.register(registry, store)
    return registry
