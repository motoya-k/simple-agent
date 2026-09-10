"""``session_search`` — full-text recall over every past conversation."""

from __future__ import annotations

import json


def register(registry, store) -> None:
    @registry.tool(
        name="session_search",
        description=(
            "Search every past conversation by keyword. Use it when the user refers to "
            "something you discussed before, or when you suspect a similar problem was "
            "already solved. Results come back with surrounding context."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "description": "Max results (default 5)."},
            },
            "required": ["query"],
        },
        parallel_safe=True,
    )
    def session_search(query: str, limit: int = 5) -> str:
        hits = store.search(query, limit)
        if not hits:
            return f"No past conversation matched {query!r}."
        return json.dumps(hits, ensure_ascii=False, indent=2)
