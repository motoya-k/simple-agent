"""OpenAI Responses adapter — the API the Codex models are served from.

Responses flattens a turn into a list of *items* instead of nesting blocks in
messages: text is a message item, each tool call is its own ``function_call``
item, and each result is a ``function_call_output`` item paired by
``call_id``.  This file does that flattening and its inverse, so the loop keeps
seeing the normalized transcript described in :mod:`.base`.

``store`` is off: the transcript already lives in ``state.py``, and every
request resends it, exactly as with the other adapters.  Reasoning items are
not replayed (the same trade-off the Bedrock adapter makes); the model still
works, it just re-derives its reasoning each turn.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

from .base import Provider, Response, ToolCall

API_URL = "https://api.openai.com/v1/responses"


class OpenAIResponsesProvider(Provider):
    name = "openai"

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: int = 600,
    ) -> None:
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        if not self.api_key:
            raise RuntimeError("OPENAI_API_KEY is not set. Put it in .env.")
        # Azure OpenAI and proxies serve the same shape from another URL.
        self.url = base_url or os.environ.get("OPENAI_RESPONSES_URL") or API_URL
        self.timeout = timeout

    def complete(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        max_tokens: int,
        model: str,
    ) -> Response:
        body = build_request(
            system=system, messages=messages, tools=tools, max_tokens=max_tokens, model=model
        )
        request = urllib.request.Request(
            self.url,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "content-type": "application/json",
                "authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:  # surface the API's own message
            detail = exc.read().decode("utf-8", "replace")[:2000]
            raise RuntimeError(f"OpenAI API error {exc.code}: {detail}") from exc

        return parse_response(data)


# -- normalized -> Responses --------------------------------------------------


def build_request(
    *,
    system: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    max_tokens: int,
    model: str,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": model,
        # OpenAI caches a shared prefix automatically; keeping the system
        # prompt immutable (see memory.py) is all it takes to hit it.
        "instructions": system,
        "input": [item for m in messages for item in _to_items(m)],
        "max_output_tokens": max_tokens,
        "store": False,
    }
    if tools:
        body["tools"] = [
            {
                "type": "function",
                "name": tool["name"],
                "description": tool.get("description", ""),
                "parameters": tool["input_schema"],
                # Strict mode demands every property be required; our schemas
                # have optional arguments.
                "strict": False,
            }
            for tool in tools
        ]
    return body


def _to_items(message: dict[str, Any]) -> list[dict[str, Any]]:
    role = message["role"]
    content = message.get("content")
    if isinstance(content, str):
        return [{"role": role, "content": content}] if content else []

    items: list[dict[str, Any]] = []
    for block in content or []:
        kind = block.get("type")
        if kind == "text":
            if block.get("text"):
                items.append({"role": role, "content": block["text"]})
        elif kind == "tool_use":
            items.append(
                {
                    "type": "function_call",
                    "call_id": block["id"],
                    "name": block["name"],
                    "arguments": json.dumps(block.get("input") or {}, ensure_ascii=False),
                }
            )
        elif kind == "tool_result":
            output = _result_text(block.get("content"))
            if block.get("is_error"):
                output = f"ERROR: {output}"  # Responses has no error flag
            items.append(
                {"type": "function_call_output", "call_id": block["tool_use_id"], "output": output}
            )
        else:
            raise ValueError(f"OpenAI adapter cannot send a {kind!r} block")
    return items


def _result_text(content: Any) -> str:
    if isinstance(content, list):
        content = "".join(b.get("text", "") for b in content if isinstance(b, dict))
    return str(content) if content else "(no output)"


# -- Responses -> normalized --------------------------------------------------


def parse_response(data: dict[str, Any]) -> Response:
    text_parts: list[str] = []
    tool_calls: list[ToolCall] = []
    raw_content: list[dict[str, Any]] = []

    for item in data.get("output") or []:
        kind = item.get("type")
        if kind == "message":
            text = "".join(
                c.get("text", "") for c in item.get("content") or [] if c.get("type") == "output_text"
            )
            if text:
                text_parts.append(text)
                raw_content.append({"type": "text", "text": text})
        elif kind == "function_call":
            try:
                arguments = json.loads(item.get("arguments") or "{}")
            except json.JSONDecodeError:
                # Hand the bad JSON to the tool layer, which reports it back
                # to the model as an error instead of crashing the turn.
                arguments = {"_raw_arguments": item.get("arguments")}
            tool_calls.append(ToolCall(id=item["call_id"], name=item["name"], arguments=arguments))
            raw_content.append(
                {"type": "tool_use", "id": item["call_id"], "name": item["name"], "input": arguments}
            )
        # reasoning items are not replayed; see the module docstring.

    usage = data.get("usage") or {}
    if tool_calls:
        stop_reason = "tool_use"
    elif (data.get("incomplete_details") or {}).get("reason") == "max_output_tokens":
        stop_reason = "max_tokens"
    else:
        stop_reason = "end_turn"
    return Response(
        text="".join(text_parts),
        tool_calls=tool_calls,
        raw_content=raw_content,
        input_tokens=usage.get("input_tokens", 0),
        output_tokens=usage.get("output_tokens", 0),
        stop_reason=stop_reason,
    )
