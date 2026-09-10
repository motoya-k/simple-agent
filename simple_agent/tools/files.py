"""File tools: read, write, edit.

``read_file`` is marked parallel-safe; the two writers are not, so the loop
serializes them even when the model asks for several at once.
"""

from __future__ import annotations

from pathlib import Path

MAX_READ = 200_000


def register(registry) -> None:
    @registry.tool(
        name="read_file",
        description="Read a text file. Returns the content with 1-indexed line numbers.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "offset": {"type": "integer", "description": "First line to read (1-indexed)."},
                "limit": {"type": "integer", "description": "Number of lines to read."},
            },
            "required": ["path"],
        },
        parallel_safe=True,
    )
    def read_file(path: str, offset: int = 1, limit: int | None = None) -> str:
        target = Path(path).expanduser()
        if not target.exists():
            return f"No such file: {target}"
        if target.is_dir():
            entries = sorted(p.name + ("/" if p.is_dir() else "") for p in target.iterdir())
            return f"{target} is a directory:\n" + "\n".join(entries)

        text = target.read_text("utf-8", errors="replace")[:MAX_READ]
        lines = text.splitlines()
        start = max(1, offset)
        end = len(lines) if limit is None else min(len(lines), start - 1 + limit)
        numbered = [f"{i:>6}\t{lines[i - 1]}" for i in range(start, end + 1)]
        return "\n".join(numbered) or "[empty file]"

    @registry.tool(
        name="write_file",
        description="Write a file, creating parent directories. Overwrites existing content.",
        parameters={
            "type": "object",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"],
        },
    )
    def write_file(path: str, content: str) -> str:
        target = Path(path).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, "utf-8")
        return f"Wrote {len(content)} chars to {target}"

    @registry.tool(
        name="edit_file",
        description=(
            "Replace an exact string in a file. old_string must appear exactly once "
            "unless replace_all is true."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old_string": {"type": "string"},
                "new_string": {"type": "string"},
                "replace_all": {"type": "boolean"},
            },
            "required": ["path", "old_string", "new_string"],
        },
    )
    def edit_file(
        path: str, old_string: str, new_string: str, replace_all: bool = False
    ) -> str:
        target = Path(path).expanduser()
        if not target.exists():
            return f"No such file: {target}"
        text = target.read_text("utf-8")
        count = text.count(old_string)
        if count == 0:
            return "old_string not found — read the file and match it exactly."
        if count > 1 and not replace_all:
            return f"old_string appears {count} times. Add more context or set replace_all."
        target.write_text(text.replace(old_string, new_string), "utf-8")
        return f"Replaced {count if replace_all else 1} occurrence(s) in {target}"
