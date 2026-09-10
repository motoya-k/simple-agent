"""``skill_view`` and ``skill_manage`` — how the agent reads and grows skills."""

from __future__ import annotations


def register(registry, library) -> None:
    @registry.tool(
        name="skill_view",
        description=(
            "Load the full text of a skill. The system prompt lists only names and "
            "descriptions; read the body before following a procedure."
        ),
        parameters={
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
        parallel_safe=True,
    )
    def skill_view(name: str) -> str:
        skill = library.get(name)
        if skill is None:
            return f"No such skill: {name}. Known: {', '.join(s.name for s in library.all()) or '(none)'}"
        library.mark_used(name)
        return skill.body

    @registry.tool(
        name="skill_manage",
        description=(
            "Create or improve a skill — a durable procedure for a CLASS of task. "
            "Write one when a non-obvious workflow succeeded, when an error was worked "
            "around, or when the user corrected how you work. Prefer patching an "
            "existing skill over creating a near-duplicate."
        ),
        parameters={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["list", "create", "patch", "append"]},
                "name": {"type": "string"},
                "description": {
                    "type": "string",
                    "description": "One line, for create. This is what future sessions see first.",
                },
                "body": {"type": "string", "description": "Full Markdown body, for create."},
                "old_text": {"type": "string", "description": "Exact text to replace, for patch."},
                "new_text": {"type": "string", "description": "Replacement text, for patch."},
                "text": {"type": "string", "description": "Text to add, for append."},
            },
            "required": ["action"],
        },
    )
    def skill_manage(
        action: str,
        name: str = "",
        description: str = "",
        body: str = "",
        old_text: str = "",
        new_text: str = "",
        text: str = "",
    ) -> str:
        if action == "list":
            skills = library.all()
            if not skills:
                return "(no skills yet)"
            return "\n".join(
                f"- {s.name} [{s.status}, used {s.meta.get('uses', '0')}x]: {s.description}"
                for s in skills
            )
        if not name:
            return "name is required for this action."
        if action == "create":
            return library.create(name, description, body)
        if action == "patch":
            return library.patch(name, old_text, new_text)
        if action == "append":
            return library.append(name, text)
        return f"Unknown action: {action}"
