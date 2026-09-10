"""Memory: two small Markdown files, frozen into the system prompt.

* ``MEMORY.md`` — what the agent learned about the environment and the work.
* ``USER.md``   — who the user is and how they want to be worked with.

Two rules make this work, and both come straight from Hermes:

1. **The prompt prefix is sacred.**  The snapshot read at session start is what
   goes into the system prompt, and it never changes for the life of the
   session.  Mid-session writes land on disk and take effect at the *next*
   session start.  Rewriting the prefix would invalidate the provider's prompt
   cache on every turn and quietly multiply the user's bill.

2. **Memory is capped.**  A budget that cannot grow forces the agent to keep
   only what actually matters, and keeps the always-resident prompt cost flat.
"""

from __future__ import annotations

from pathlib import Path

MEMORY_MAX_CHARS = 2200
USER_MAX_CHARS = 1375

_SEED_MEMORY = """# Memory

Durable notes about this environment and how work gets done here.
"""

_SEED_USER = """# User

Who the user is, what they prefer, and how they want to be worked with.
"""


class MemoryStore:
    def __init__(self, directory: Path) -> None:
        self.dir = directory
        self.dir.mkdir(parents=True, exist_ok=True)
        self.memory_path = self.dir / "MEMORY.md"
        self.user_path = self.dir / "USER.md"
        if not self.memory_path.exists():
            self.memory_path.write_text(_SEED_MEMORY, "utf-8")
        if not self.user_path.exists():
            self.user_path.write_text(_SEED_USER, "utf-8")

        # The frozen snapshot. Read once, never re-read within a session.
        self._frozen_memory = self.memory_path.read_text("utf-8")
        self._frozen_user = self.user_path.read_text("utf-8")

    # -- what the system prompt sees ------------------------------------
    def snapshot(self) -> str:
        return (
            f"<memory>\n{self._frozen_memory.strip()}\n</memory>\n\n"
            f"<user>\n{self._frozen_user.strip()}\n</user>"
        )

    # -- what the memory tool writes ------------------------------------
    def read(self, target: str) -> str:
        return self._path(target).read_text("utf-8")

    def write(self, target: str, content: str) -> str:
        path = self._path(target)
        limit = MEMORY_MAX_CHARS if target == "memory" else USER_MAX_CHARS
        content = content.strip() + "\n"
        if len(content) > limit:
            return (
                f"Refused: {len(content)} chars exceeds the {limit}-char budget for "
                f"{path.name}. Rewrite it more tightly — drop what stopped mattering."
            )
        path.write_text(content, "utf-8")
        return (
            f"Saved {len(content)}/{limit} chars to {path.name}. "
            "Takes effect at the start of the next session (the prompt prefix is frozen)."
        )

    def _path(self, target: str) -> Path:
        if target == "memory":
            return self.memory_path
        if target == "user":
            return self.user_path
        raise ValueError("target must be 'memory' or 'user'")
