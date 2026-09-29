"""Review gate: decide which review passes are worth running, before paying for them.

The background reviewer (see review.py) runs a full LLM conversation per pass
after every turn.  Most turns teach nothing, and a small model still costs a
real request to find that out.  This gate asks TypeSafe's Jev — a model that
answers typed yes/no questions in well under a second and bills input tokens
only — whether each pass has anything to work with, and skips the ones that
do not.

The gate fails *open*.  No key, a network error, or an unexpected payload all
mean "run every pass": the gate exists to save money, and the review prompt's
own premise is that a skipped lesson costs more than a wasted review.  For the
same reason the default threshold is low, and Jev's weaker accuracy on
Japanese text pushes it lower still.

https://docs.typesafe.ai/api
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

API_URL = "https://api.typesafe.ai/v1/systemone"
DEFAULT_MODEL = "jev-latest"

# Jev reads at most 32k tokens of state; stay well under it.
MAX_STATE_CHARS = 24_000
MAX_BLOCK_CHARS = 1_500

# One yes/no question per review pass, keyed by the pass name review.py uses.
QUESTIONS: dict[str, dict[str, Any]] = {
    "memory": {
        "type": "noul",
        "instructions": (
            "Does the conversation reveal durable knowledge about this team or its "
            "people — a convention, who owns what, a system's name, a decision, or "
            "how they want the assistant to work?"
        ),
        "criteria": {
            "true": "A team fact or preference worth knowing in future conversations",
            "false": "Only the task at hand; nothing that holds beyond this conversation",
        },
    },
    "skills": {
        "type": "noul",
        "instructions": (
            "Does this exchange contain a reusable method a future session should "
            "already know: a correction of the assistant's workflow, a non-obvious "
            "fix or technique, or a sign that existing guidance was wrong?"
        ),
        "criteria": {
            "true": "A correction or reusable lesson happened",
            "false": "A routine exchange with nothing to generalize",
        },
    },
}


class JevReviewGate:
    def __init__(
        self,
        api_key: str,
        *,
        model: str | None = None,
        threshold: float = 0.15,
        url: str | None = None,
        timeout: int = 10,
    ) -> None:
        self.api_key = api_key
        self.model = model or os.environ.get("TYPESAFE_DEFAULT_MODEL") or DEFAULT_MODEL
        self.threshold = threshold
        base = os.environ.get("TYPESAFE_BASE_URL")
        self.url = url or (f"{base.rstrip('/')}/v1/systemone" if base else API_URL)
        self.timeout = timeout

    def passes(self, transcript: list[dict[str, Any]]) -> set[str]:
        """The review passes worth running for the turn that just finished."""
        try:
            answers = self._ask(last_turn_state(transcript))
            return {name for name in QUESTIONS if answers[name]["noul"] >= self.threshold}
        except Exception:
            return set(QUESTIONS)  # fail open; see the module docstring

    def _ask(self, state: list[dict[str, str]]) -> dict[str, Any]:
        body = {"state": state, "model": self.model, "questions": QUESTIONS}
        request = urllib.request.Request(
            self.url,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={
                "content-type": "application/json",
                "authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))["answers"]


def default_gate(threshold: float) -> JevReviewGate | None:
    """A gate when TYPESAFE_API_KEY is set, otherwise None (every pass runs)."""
    api_key = os.environ.get("TYPESAFE_API_KEY", "")
    return JevReviewGate(api_key, threshold=threshold) if api_key else None


def last_turn_state(transcript: list[dict[str, Any]]) -> list[dict[str, str]]:
    """The turn that just finished, as ``[{role, text}]`` Jev can read.

    Earlier turns were already reviewed after they finished, so only the newest
    one can hold something new.  A turn starts at the last user message that is
    not just tool results.
    """
    start = 0
    for index, message in enumerate(transcript):
        if message["role"] == "user" and not _is_tool_results(message):
            start = index

    state: list[dict[str, str]] = []
    for message in transcript[start:]:
        text = _readable(message.get("content"))
        if text:
            state.append({"role": message["role"], "text": text})

    # Keep the user's opening message and the most recent steps if over budget.
    while len(state) > 2 and sum(len(s["text"]) for s in state) > MAX_STATE_CHARS:
        del state[1]
    return state


def _is_tool_results(message: dict[str, Any]) -> bool:
    content = message.get("content")
    return isinstance(content, list) and bool(content) and all(
        block.get("type") == "tool_result" for block in content
    )


def _readable(content: Any) -> str:
    if isinstance(content, str):
        return content[:MAX_BLOCK_CHARS]
    parts: list[str] = []
    for block in content or []:
        kind = block.get("type")
        if kind == "text":
            parts.append(block.get("text", ""))
        elif kind == "tool_use":
            args = json.dumps(block.get("input") or {}, ensure_ascii=False)
            parts.append(f"[called {block['name']} {args}]")
        elif kind == "tool_result":
            result = block.get("content")
            if isinstance(result, list):
                result = "".join(b.get("text", "") for b in result if isinstance(b, dict))
            parts.append(f"[tool result: {result}]")
    return "\n".join(p[:MAX_BLOCK_CHARS] for p in parts if p)
