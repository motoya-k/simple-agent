"""Provider seam — the one place that knows how to talk to an LLM.

Design philosophy (carried over from Hermes):

    The core is a narrow waist. Everything provider-specific lives behind
    ``Provider``; the conversation loop never imports an SDK, never branches
    on a vendor name, and never sees a vendor-shaped payload.

Hermes ships adapters for ``chat_completions`` / ``anthropic_messages`` /
``bedrock_converse`` / ``gemini_native`` / ``codex_responses`` plus a fallback
chain across ~30 provider plugins.  This repo implements exactly one adapter
(Anthropic) and keeps the seam so a second one is an additive file, not a
change to the loop.  See DESIGN.md § Providers.

Normalized wire format
----------------------
Messages are dicts::

    {"role": "user" | "assistant", "content": str | [block, ...]}

Blocks are one of::

    {"type": "text",        "text": str}
    {"type": "tool_use",    "id": str, "name": str, "input": dict}
    {"type": "tool_result", "tool_use_id": str, "content": str, "is_error": bool}

This happens to match the Anthropic Messages shape.  That is deliberate: it is
the richest of the common shapes, so every other adapter can *narrow* into its
own wire format instead of guessing at information the loop never carried.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class Response:
    """One assistant turn, normalized."""

    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    raw_content: list[dict[str, Any]] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    stop_reason: str = ""

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


class Provider(ABC):
    """Implement these two members and the whole agent works on your backend."""

    name: str = "unnamed"

    @abstractmethod
    def complete(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        max_tokens: int,
        model: str,
    ) -> Response:
        """Send one request and return a normalized :class:`Response`."""

    def assistant_message(self, response: Response) -> dict[str, Any]:
        """Turn a response back into a message to append to the transcript.

        Kept on the provider because round-tripping the assistant turn is the
        one place vendors disagree in a way the loop must not care about.
        """
        return {"role": "assistant", "content": response.raw_content}
