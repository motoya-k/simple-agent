"""``Agent`` — the narrow waist.

One class, reusable from anywhere: a REPL, a chat gateway, a cron job, a
subagent.  It owns the transcript, assembles the system prompt exactly once,
and runs each turn through :func:`simple_agent.loop.run_conversation`.

Everything it holds is an injected collaborator — provider, tools, memory,
skills, store, compactor — so a different host swaps parts without forking the
core.  Nothing below imports a vendor SDK, a chat platform, or a renderer.

*Who* it is comes from a :class:`~simple_agent.profile.Profile`: the system
prompt's instructions, the toolset, whether the turn may teach long-term
memory, and which namespace it reads.  The class holds no identity of its own,
so the same core serves a terminal and an inbox without a branch.

Two responsibilities are easy to miss and hard to add later:

* **The transcript outlives the process.**  Every message is written to the
  store as it happens, including the tool calls, and a conversation resumed by
  session key comes back whole.  A chat thread is not a terminal session; it
  is still there tomorrow.
* **The transcript has to be able to shrink.**  See
  :mod:`simple_agent.compaction`.
"""

from __future__ import annotations

import os
import platform
import time
from typing import Any, Callable, Iterable

from .compaction import Compactor, TailCompactor
from .config import Config
from .context import session_scope
from .loop import Budget, Turn, run_conversation
from .memory import LongTermMemory, format_recall, open_memory
from .profile import Profile, load_profile
from .providers import get_provider
from .review import spawn_background_review
from .session import SessionSource, build_session_key, is_shared_multi_user_session
from .skills import SkillLibrary, open_skills
from .state import Store, open_store
from .tools import build_registry

SHARED_CONVERSATION_NOTE = """This conversation is shared: more than one person can speak \
into it, and messages you see may come from different people. Do not assume the \
last speaker is the same as the previous one. Save something to long-term memory \
only when it is what the team holds, not one speaker's claim."""


class Agent:
    def __init__(
        self,
        config: Config | None = None,
        *,
        cwd: str | None = None,
        source: SessionSource | None = None,
        session_key: str | None = None,
        resume: bool = True,
        profile: Profile | None = None,
        provider: Any = None,
        memory: LongTermMemory | None = None,
        skills: SkillLibrary | None = None,
        store: Store | None = None,
        compactor: Compactor | None = None,
        tools: Iterable[str] | None = None,
    ) -> None:
        self.config = config or Config.load()
        self.profile = profile or load_profile(self.config)
        # One namespace for both: the team whose knowledge this conversation
        # reads is the team whose conversation it is.
        self.namespace = self.profile.namespace or self.config.memory_namespace
        self.cwd = cwd or os.getcwd()
        self.source = source or SessionSource.local(self.cwd)
        self.session_key = session_key or build_session_key(
            self.source, profile=self.namespace
        )
        self.shared = is_shared_multi_user_session(self.source)

        # Built here so a missing API key fails at start, not on the first turn.
        self.provider = provider or get_provider(self.config.provider)
        self.skills = skills or open_skills(self.config)
        self.store = store or open_store(self.config)
        self.compactor = compactor or TailCompactor()

        self.memory = memory or open_memory(self.config, self.namespace)

        self.registry = build_registry(self.config, self.memory, self.skills, self.store)
        if tools is None:
            tools = self.profile.tools
        if tools is not None:
            # A narrowed toolset is how an untrusted route (an inbox anyone can
            # write to) runs without a terminal. See profile.py.
            self.registry = self.registry.subset(list(tools))
        # The profile and the config both have to allow it: a profile cannot
        # turn learning on where the deployment turned it off.
        self.learning = self.config.learning and self.profile.learning
        self.skills.curate()  # age skills once per session, never delete

        self.messages: list[dict[str, Any]] = []
        # Long-term memories already brought into this conversation. Once
        # recalled, a memory is part of the transcript; it is not added again.
        self._recalled: set[str] = set()
        self.session_id = self._open_session(resume)
        # id(message) -> the store row it was written to, so a message that
        # gets revised in place can supersede its own row instead of doubling.
        self._rows: dict[int, int | None] = {id(m): None for m in self.messages}

        self.budget = Budget()
        self._interrupted = False
        # Frozen for the life of the session: it would otherwise invalidate
        # the provider's prompt cache on every change. Long-term memory is
        # recalled per turn into the user message instead — see memory.py.
        self.system = self._build_system_prompt()

    # -- session lifecycle ----------------------------------------------
    def _open_session(self, resume: bool) -> str:
        """Continue this conversation if it already exists, else start it."""
        if resume:
            existing = self.store.latest_session_for_key(self.session_key)
            if existing:
                self.messages = self.store.conversation(existing)
                return existing
        return self.store.new_session(self.cwd, self.session_key)

    def _build_system_prompt(self) -> str:
        sections = [
            self.profile.instructions,
            f"<environment>\n"
            f"os: {platform.system()} {platform.release()}\n"
            f"cwd: {self.cwd}\n"
            f"date: {time.strftime('%Y-%m-%d')}\n"
            f"conversation: {self.source.description}\n"
            f"</environment>",
        ]
        if self.shared:
            sections.append(SHARED_CONVERSATION_NOTE)
        sections.append(f"<skills>\n{self.skills.catalog()}\n</skills>")
        return "\n\n".join(sections)

    # -- running --------------------------------------------------------
    def interrupt(self) -> None:
        """Ask the current turn to stop. Safe to call from another thread."""
        self._interrupted = True

    def run(
        self,
        user_input: str,
        *,
        on_event: Callable[[str, str], None] | None = None,
    ) -> Turn:
        self._interrupted = False
        with session_scope(
            self.session_key, session_id=self.session_id, source=self.source
        ):
            self._compact_if_needed()
            self._append_user(self._with_recall(user_input))

            try:
                turn = run_conversation(
                    provider=self.provider,
                    model=self.config.model,
                    system=self.system,
                    messages=self.messages,
                    registry=self.registry,
                    budget=self.budget,
                    on_event=on_event,
                    interrupt=lambda: self._interrupted,
                )
            finally:
                # Persist whatever the turn produced even when it failed
                # partway. A transcript missing its middle cannot be resumed,
                # and a crash is exactly when resuming matters.
                self._persist_new_messages()

            if self.learning and turn.complete:
                spawn_background_review(self, list(self.messages))
            return turn

    # -- transcript -----------------------------------------------------
    def _append_user(self, text: str) -> None:
        """Add the user's message — and settle any debt the last turn left.

        A turn can end between "the model asked to run three commands" and
        "here is what they printed": Ctrl-C, a crash, a killed container. The
        provider treats that transcript as malformed and refuses the whole
        conversation, so one interruption would otherwise brick a thread for
        good. The missing answers are filled in here, in the *same* user turn
        as the new message — tool results must lead a user turn, and a second
        user turn beside the first would break the alternation the provider
        also requires.
        """
        pending = self._unanswered_tool_calls()
        if pending:
            self.messages.append(
                {"role": "user", "content": [*pending, {"type": "text", "text": text}]}
            )
            return

        # A user turn already at the end means the process died between the
        # tool results and the reply. Fold into it rather than beside it.
        if self.messages and self.messages[-1].get("role") == "user":
            last = self.messages[-1]
            content = last.get("content")
            if isinstance(content, list):
                merged = [*content, {"type": "text", "text": text}]
            else:
                merged = f"{content}\n\n{text}"
            row = self._rows.pop(id(last), None)
            self.messages[-1] = {"role": "user", "content": merged}
            if row is not None:
                new_row = self.store.replace_message(self.session_id, row, merged)
                self._rows[id(self.messages[-1])] = new_row
            return

        self.messages.append({"role": "user", "content": text})

    def _with_recall(self, text: str) -> str:
        """Bring the team knowledge this message needs into the conversation.

        Only on routes that may read long-term memory: an untrusted route (an
        inbox anyone can write to) is not given ``memory_search``, and must not
        be handed the team's knowledge unasked either. A backend that is down
        costs the turn its recall, never the turn itself.
        """
        if "memory_search" not in self.registry:
            return text
        try:
            recalled = self.memory.recall(text)
        except Exception:
            return text
        fresh = [m for m in recalled if (m.id or m.text) not in self._recalled]
        if not fresh:
            return text
        self._recalled.update(m.id or m.text for m in fresh)
        return f"{format_recall(fresh, self.memory.namespace)}\n\n{text}"

    def _unanswered_tool_calls(self) -> list[dict[str, Any]]:
        """Result blocks owed to tool calls that never ran."""
        if not self.messages:
            return []
        last = self.messages[-1]
        if last.get("role") != "assistant":
            return []
        content = last.get("content")
        return [
            {
                "type": "tool_result",
                "tool_use_id": block["id"],
                "content": "[not run — the previous turn ended early]",
                "is_error": True,
            }
            for block in (content if isinstance(content, list) else [])
            if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("id")
        ]

    def _persist_new_messages(self) -> None:
        """Write everything this turn appended, once.

        Tracked by object identity rather than position: compaction rewrites
        the list around the messages it keeps, so an index into it goes stale
        while the objects themselves do not.
        """
        for message in self.messages:
            marker = id(message)
            if marker in self._rows:
                continue
            self._rows[marker] = self.store.add_message(
                self.session_id, message["role"], message["content"]
            )

    def _compact_if_needed(self) -> None:
        """Drop weight before the next request, not after it is rejected."""
        if not self.compactor.should_compact(self.messages, self.budget.last_prompt_tokens):
            return
        before = len(self.messages)
        self.messages = self.compactor.compact(self.messages)
        if len(self.messages) < before:
            # Compaction changes what the model sees, not what happened. The
            # store keeps the full record; only the live window shrinks.
            # Everything left is already on disk — compaction runs before the
            # turn's first new message, so nothing unsaved can be in the list.
            # The one rewritten message keeps its original row: the store holds
            # the full record, and only the live window changed.
            self._rows = {id(m): self._rows.get(id(m)) for m in self.messages}
            self.budget.last_prompt_tokens = 0
            # What was recalled may have been in the dropped turns. Short-term
            # memory forgot it, so long-term memory may supply it again.
            self._recalled.clear()

