"""The learning loop: a second, cheaper agent that reviews each finished turn.

After every turn the main conversation hands a transcript to a reviewer running
on a small model, in a background thread, with a *narrow* toolset — memory and
skills only.  It cannot run commands, cannot answer the user, and cannot touch
the live conversation.  The user never waits for it.

Two passes, because the questions are different:

* **Memory** — did the conversation reveal something this *team* knows or
  expects — a convention, an owner, a decision, a way of working?  Saved to
  long-term memory, shared by every future conversation.
* **Skills** — did a *method* emerge that would work anywhere?  Written as an
  abstract procedure, with the team facts it needs left to memory.

What neither pass touches is short-term memory: the task itself stays in the
transcript and is never promoted.

The skill prompt is deliberately pushy.  A reviewer that treats "do nothing" as
the safe default never learns anything, so the prompt names that as the failure
mode: a pass that changes nothing is a missed opportunity, not a neutral one.

Both review prompts are adapted from hermes-agent's ``agent/background_review.py``
(https://github.com/NousResearch/hermes-agent), Copyright (c) 2025 Nous Research,
MIT License.  See LICENSE.
"""

from __future__ import annotations

import threading
from contextvars import copy_context
from typing import Any

from .loop import Budget, run_conversation
from .review_gate import default_gate

MEMORY_REVIEW_PROMPT = """Review the conversation above for TEAM KNOWLEDGE worth keeping in \
long-term memory — things anyone working with this team is expected to know.

Save (one self-contained fact per memory_save call, readable without this \
conversation):
  - Conventions and rules: 'releases go out on Fridays', 'reports are written \
in Japanese'.
  - Who owns what, and the names and roles of systems, places and people.
  - Decisions and their reasons, so they are not re-litigated.
  - How this team or its people want work done: tone, format, verbosity, \
language. 'Stop doing X' and 'too verbose' belong here.

Do NOT save:
  - What is only true for this conversation — the task in progress, files \
just read, intermediate results. That is short-term memory; the transcript \
already has it.
  - Procedures that would work at any company. Those are skills, handled by a \
separate pass.
  - Secrets, credentials, or one speaker's unconfirmed claim about the team.

Search with memory_search first: skip what is already known, and when \
something contradicts a saved fact, save the corrected fact with the date. \
If nothing is worth saving, reply exactly 'Nothing to save.' and stop."""

SKILL_REVIEW_PROMPT = """Review the conversation above and update the skill library. Be ACTIVE — \
most sessions produce at least one skill update, even a small one. A pass that \
changes nothing is a missed learning opportunity, not a neutral outcome.

Skills are ABSTRACT procedures: how to do a class of task, written so they \
would still be correct at another company. Team facts — hosts, URLs, names, \
owners, conventions, preferences — are saved to long-term memory by a separate \
pass; in a skill, refer to them by role ('the staging host', 'the team's \
report language') so the agent recalls the value when it runs.

Signals that warrant action (any one is enough):
  - The user corrected your workflow or the order you did things in. Record the \
correction as an explicit step or a pitfall.
  - A non-obvious technique, fix, or debugging path emerged that a future \
session would benefit from.
  - A skill you loaded this session turned out to be wrong or incomplete. Patch \
it now.
  - A skill contains a team-specific value. Replace it with its role.

Prefer the earliest action that fits: patch a skill you actually used this \
session; then patch a skill that covers the same class of task; only then create \
a new one. Aim for a few class-level skills with rich bodies, not a long flat \
list of one-session entries.

If truly nothing applies, reply exactly 'Nothing to learn.' and stop."""

REVIEW_TOOLS = ["memory_search", "memory_save", "skill_manage", "skill_view"]


def spawn_background_review(agent, transcript: list[dict[str, Any]]) -> threading.Thread:
    """Fire and forget. Failures are swallowed — learning must never break a turn."""
    # copy_context so the reviewer's tools see the same conversation the turn
    # belonged to. A fresh thread otherwise starts with an empty context and
    # would resolve session-scoped state to whatever the default is.
    thread = threading.Thread(
        target=copy_context().run,
        args=(_review, agent, transcript),
        name="bg-review",
        daemon=True,
    )
    thread.start()
    return thread


def _review(agent, transcript: list[dict[str, Any]], gate: Any = None) -> None:
    # An optional cheap pre-check that skips passes with nothing to work on.
    # See review_gate.py; without TYPESAFE_API_KEY every pass runs.
    gate = gate or default_gate(agent.config.review_gate_threshold)
    wanted = gate.passes(transcript) if gate else {"memory", "skills"}
    passes = [
        prompt
        for name, prompt in (("memory", MEMORY_REVIEW_PROMPT), ("skills", SKILL_REVIEW_PROMPT))
        if name in wanted
    ]
    if not passes:
        return

    registry = agent.registry.subset(REVIEW_TOOLS)
    system = (
        "You are the reviewer for an AI agent. You are shown a finished conversation "
        "between that agent and its user. You do not talk to the user and you do not "
        "continue the work. Your only job is to decide what should be remembered or "
        "written down, and to act on it with the tools you have.\n\n"
        f"Long-term memory namespace: {agent.memory.namespace}\n\n"
        f"Current skills:\n{agent.skills.catalog()}"
    )

    for prompt in passes:
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
                max_turn_iterations=6,
            )
        except Exception:
            return  # a reviewer that crashes is a no-op, not an incident
