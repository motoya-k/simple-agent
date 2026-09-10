"""The learning loop: a second, cheaper agent that reviews each finished turn.

After every turn the main conversation hands a transcript to a reviewer running
on a small model, in a background thread, with a *narrow* toolset — memory and
skills only.  It cannot run commands, cannot answer the user, and cannot touch
the live conversation.  The user never waits for it.

Two passes, because the questions are different:

* **Memory** — did the user reveal something about themselves, or about how they
  want to be worked with?
* **Skills** — did anything happen that a future session should start out
  already knowing?

The skill prompt is deliberately pushy.  A reviewer that treats "do nothing" as
the safe default never learns anything, so the prompt names that as the failure
mode: a pass that changes nothing is a missed opportunity, not a neutral one.
"""

from __future__ import annotations

import threading
from typing import Any

from .loop import Budget, run_conversation

MEMORY_REVIEW_PROMPT = """Review the conversation above and consider saving to memory.

1. Did the user reveal things about themselves — their role, preferences, \
constraints, or personal details worth remembering?
2. Did the user express expectations about how you should behave, their working \
style, or ways they want you to operate?

Save anything durable with the memory tool. Both files are size-capped, so \
prefer rewriting a line over appending a new one. If nothing is worth saving, \
reply exactly 'Nothing to save.' and stop."""

SKILL_REVIEW_PROMPT = """Review the conversation above and update the skill library. Be ACTIVE — \
most sessions produce at least one skill update, even a small one. A pass that \
changes nothing is a missed learning opportunity, not a neutral outcome.

Signals that warrant action (any one is enough):
  - The user corrected your style, tone, format, or verbosity. 'Stop doing X', \
'too verbose', 'just give me the answer', 'remember this' are first-class skill \
signals — embed the preference so the next session starts already knowing it.
  - The user corrected your workflow or the order you did things in. Record the \
correction as an explicit step or a pitfall.
  - A non-obvious technique, fix, or debugging path emerged that a future \
session would benefit from.
  - A skill you loaded this session turned out to be wrong or incomplete. Patch \
it now.

Prefer the earliest action that fits: patch a skill you actually used this \
session; then patch a skill that covers the same class of task; only then create \
a new one. Aim for a few class-level skills with rich bodies, not a long flat \
list of one-session entries.

If truly nothing applies, reply exactly 'Nothing to learn.' and stop."""

REVIEW_TOOLS = ["memory", "skill_manage", "skill_view"]


def spawn_background_review(agent, transcript: list[dict[str, Any]]) -> threading.Thread:
    """Fire and forget. Failures are swallowed — learning must never break a turn."""
    thread = threading.Thread(
        target=_review,
        args=(agent, transcript),
        name="bg-review",
        daemon=True,
    )
    thread.start()
    return thread


def _review(agent, transcript: list[dict[str, Any]]) -> None:
    registry = agent.registry.subset(REVIEW_TOOLS)
    system = (
        "You are the reviewer for an AI agent. You are shown a finished conversation "
        "between that agent and its user. You do not talk to the user and you do not "
        "continue the work. Your only job is to decide what should be remembered or "
        "written down, and to act on it with the tools you have.\n\n"
        f"Current memory:\n{agent.memory.snapshot()}\n\n"
        f"Current skills:\n{agent.skills.catalog()}"
    )

    for prompt in (MEMORY_REVIEW_PROMPT, SKILL_REVIEW_PROMPT):
        messages = list(transcript) + [{"role": "user", "content": prompt}]
        try:
            run_conversation(
                provider=agent.provider,
                model=agent.config.review_model,
                system=system,
                messages=messages,
                registry=registry,
                max_tokens=2048,
                budget=Budget(max_iterations=6, token_budget=200_000),
            )
        except Exception:
            return  # a reviewer that crashes is a no-op, not an incident
