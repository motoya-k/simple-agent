"""``Agent`` — the narrow waist.

One class, reusable from anywhere: a REPL, a chat gateway, a cron job, a
subagent.  It owns the transcript, assembles the system prompt exactly once, and
delegates the actual work to :func:`simple_agent.loop.run_conversation`.

Everything it holds is an injected collaborator — provider, tools, memory,
skills, store — so a different host swaps parts without forking the core.
"""

from __future__ import annotations

import os
import platform
import time
from typing import Any, Callable

from .config import Config
from .loop import Budget, Turn, run_conversation
from .memory import MemoryStore
from .providers import get_provider
from .review import spawn_background_review
from .skills import SkillLibrary
from .state import Store
from .tools import build_registry

IDENTITY = """You are a capable, self-improving assistant working alongside a user on their \
own machine.

How you work:
- Prefer acting over asking. Use the terminal and file tools to find things out \
instead of asking the user to describe them.
- Report what actually happened. If a command failed, say so and show the error.
- Be brief. The user is reading a terminal, not a document.

Your memory below was frozen when this session started. Writing to it takes \
effect next session — that is deliberate, and keeps the prompt prefix stable so \
it stays cached.

Your skills are procedures you wrote for yourself in earlier sessions. Only the \
names and descriptions are listed here; read a skill with skill_view before \
following it. When you learn something a future session would want, write it \
down with skill_manage."""


class Agent:
    def __init__(self, config: Config | None = None, *, cwd: str | None = None) -> None:
        self.config = config or Config.load()
        self.cwd = cwd or os.getcwd()

        self.provider = get_provider(self.config.provider)
        self.memory = MemoryStore(self.config.memories_dir)
        self.skills = SkillLibrary(self.config.skills_dir)
        self.store = Store(self.config.state_db)
        self.registry = build_registry(self.config, self.memory, self.skills, self.store)

        self.skills.curate()  # age skills once per session, never delete
        self.session_id = self.store.new_session(self.cwd)
        self.messages: list[dict[str, Any]] = []
        self.budget = Budget(
            max_iterations=self.config.max_iterations,
            token_budget=self.config.token_budget,
        )
        # Frozen for the life of the session. See memory.py for why.
        self.system = self._build_system_prompt()

    def _build_system_prompt(self) -> str:
        return "\n\n".join(
            [
                IDENTITY,
                f"<environment>\n"
                f"os: {platform.system()} {platform.release()}\n"
                f"cwd: {self.cwd}\n"
                f"date: {time.strftime('%Y-%m-%d')}\n"
                f"</environment>",
                self.memory.snapshot(),
                f"<skills>\n{self.skills.catalog()}\n</skills>",
            ]
        )

    def run(
        self,
        user_input: str,
        *,
        on_event: Callable[[str, str], None] | None = None,
    ) -> Turn:
        self.messages.append({"role": "user", "content": user_input})
        self.store.add_message(self.session_id, "user", user_input)

        turn = run_conversation(
            provider=self.provider,
            model=self.config.model,
            system=self.system,
            messages=self.messages,
            registry=self.registry,
            max_tokens=self.config.max_tokens,
            budget=self.budget,
            max_parallel_tools=self.config.max_parallel_tools,
            on_event=on_event,
        )

        self.store.add_message(self.session_id, "assistant", turn.text)
        if self.config.learning:
            spawn_background_review(self, list(self.messages))
        return turn
