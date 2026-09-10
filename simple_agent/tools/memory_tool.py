"""The ``memory`` tool — read/append/replace the two memory files."""

from __future__ import annotations


def register(registry, store) -> None:
    @registry.tool(
        name="memory",
        description=(
            "Read or update durable memory. target='memory' holds environment and "
            "working knowledge; target='user' holds who the user is and how they want "
            "to be worked with. Writes take effect next session — the current system "
            "prompt is frozen. Both files are size-capped, so edit rather than pile on."
        ),
        parameters={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["read", "append", "replace"]},
                "target": {"type": "string", "enum": ["memory", "user"]},
                "content": {
                    "type": "string",
                    "description": "Text to append, or the full new file for replace.",
                },
            },
            "required": ["action", "target"],
        },
    )
    def memory(action: str, target: str, content: str = "") -> str:
        if action == "read":
            return store.read(target)
        if action == "append":
            if not content.strip():
                return "Nothing to append."
            existing = store.read(target).rstrip()
            return store.write(target, f"{existing}\n{content.strip()}")
        if action == "replace":
            return store.write(target, content)
        return f"Unknown action: {action}"
