"""Making room in a conversation that will not end.

A terminal session ends when you close it.  A chat thread does not: it is
still there next week, and an agent that answers "I have used up my budget"
forever is broken, not safe.  So a long-lived conversation needs a way to
drop weight and keep going.

What to drop is the hard part, and it has a hard constraint the token count
never mentions: **a tool call and its result must be dropped together.**  The
model asked to run something; the answer to that request is the next message.
Keep one without the other and the provider rejects the whole conversation.
So the cut points here are chosen by message shape first and size second.

The built-in :class:`TailCompactor` keeps the opening and the recent tail and
throws the middle away, with a note saying so.  It calls no model, which is
why it can be the default.  A summarising compactor implements the same two
methods and gets used instead — that is the seam this module exists to draw.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

#: Prefixed to the first surviving message. Written to be read as background,
#: not as a fresh instruction: a note that says "earlier we discussed X" is
#: read by some models as "please do X again".
COMPACTION_NOTE = (
    "[Earlier turns in this conversation were dropped to make room. "
    "They are gone, not summarized — if you need a detail from before, "
    "search past sessions instead of guessing. Treat the message below as "
    "the current request.]\n\n"
)


@runtime_checkable
class Compactor(Protocol):
    """Decides when a conversation is too long, and what to keep."""

    def should_compact(self, messages: list[dict[str, Any]], prompt_tokens: int) -> bool:
        ...

    def compact(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        ...


def _blocks(message: dict[str, Any]) -> list[dict[str, Any]]:
    content = message.get("content")
    return content if isinstance(content, list) else []


def carries_tool_use(message: dict[str, Any]) -> bool:
    return any(b.get("type") == "tool_use" for b in _blocks(message))


def carries_tool_result(message: dict[str, Any]) -> bool:
    return any(b.get("type") == "tool_result" for b in _blocks(message))


def is_clean_break(message: dict[str, Any]) -> bool:
    """True when a conversation can end here without orphaning a tool call.

    A finished assistant answer — one that did not ask for a tool — is the
    only place where nothing downstream is waiting for a reply.
    """
    return message.get("role") == "assistant" and not carries_tool_use(message)


def is_resumable_start(message: dict[str, Any]) -> bool:
    """True when a conversation can *begin* at this message.

    A user turn that is not a bundle of tool results: it does not refer back
    to a request the model can no longer see.
    """
    return message.get("role") == "user" and not carries_tool_result(message)


class TailCompactor:
    """Keep the opening and the recent tail; drop the middle.

    ``keep_head`` exists because the first exchange usually carries the task
    itself, and ``keep_tail`` because the recent turns carry the state.  The
    middle is where the expensive, already-acted-on tool output accumulates.
    """

    def __init__(
        self,
        *,
        context_limit: int = 200_000,
        threshold: float = 0.5,
        keep_head: int = 2,
        keep_tail: int = 20,
    ) -> None:
        self.context_limit = context_limit
        self.threshold = threshold
        self.keep_head = keep_head
        self.keep_tail = keep_tail

    @property
    def threshold_tokens(self) -> int:
        return int(self.context_limit * self.threshold)

    def should_compact(self, messages: list[dict[str, Any]], prompt_tokens: int) -> bool:
        if prompt_tokens <= 0 or len(messages) <= self.keep_head + self.keep_tail:
            return False
        return prompt_tokens >= self.threshold_tokens

    def compact(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        head_end = self._head_end(messages)
        tail_start = self._tail_start(messages, head_end)
        if tail_start <= head_end:
            return list(messages)  # nothing safe to drop; leave it alone

        tail = [dict(m) for m in messages[tail_start:]]
        tail[0] = _prefix_note(tail[0])
        return list(messages[:head_end]) + tail

    def _head_end(self, messages: list[dict[str, Any]]) -> int:
        """Last index of the kept opening — never mid tool call."""
        limit = min(self.keep_head, len(messages))
        for index in range(limit - 1, -1, -1):
            if is_clean_break(messages[index]):
                return index + 1
        return 0

    def _tail_start(self, messages: list[dict[str, Any]], head_end: int) -> int:
        """First index of the kept tail — never a dangling tool result."""
        start = max(head_end, len(messages) - self.keep_tail)
        for index in range(start, len(messages)):
            if is_resumable_start(messages[index]):
                return index
        return len(messages)


def _prefix_note(message: dict[str, Any]) -> dict[str, Any]:
    content = message.get("content")
    if isinstance(content, str):
        return {**message, "content": COMPACTION_NOTE + content}
    if isinstance(content, list):
        return {**message, "content": [{"type": "text", "text": COMPACTION_NOTE}, *content]}
    return message
