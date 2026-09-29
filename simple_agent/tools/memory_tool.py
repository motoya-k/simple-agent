"""``memory_search`` and ``memory_save`` — the agent's hands on long-term memory.

Short-term memory (this conversation) needs no tool: it is the transcript.
These two reach the other layer, the team's shared knowledge. See memory.py.
"""

from __future__ import annotations

from ..context import current_source


def register(registry, memory) -> None:
    @registry.tool(
        name="memory_search",
        description=(
            "Search long-term memory: what this team already knows — conventions, "
            "owners, system names, past decisions and why. Relevant items are recalled "
            "automatically with each message; search when you need something they "
            "did not cover."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "description": "Max results (default 8)."},
            },
            "required": ["query"],
        },
        parallel_safe=True,
    )
    def memory_search(query: str, limit: int = 8) -> str:
        hits = memory.recall(query, limit)
        if not hits:
            return f"Nothing in long-term memory matched {query!r}."
        return "\n".join(f"- {m.text}" for m in hits)

    @registry.tool(
        name="memory_save",
        description=(
            "Save one piece of team knowledge to long-term memory, shared with every "
            "future conversation on this team. Save what anyone here is expected to "
            "know: conventions, who owns what, names of systems, decisions and why, "
            "how this team wants work done. Do NOT save what is only true for this "
            "conversation (that is already in the transcript), procedures that would "
            "work at any company (write a skill), or secrets. One self-contained "
            "fact per call."
        ),
        parameters={
            "type": "object",
            "properties": {"content": {"type": "string"}},
            "required": ["content"],
        },
    )
    def memory_save(content: str) -> str:
        if not content.strip():
            return "Nothing to save."
        source = current_source()
        return memory.retain(content.strip(), context=source.description if source else "")
