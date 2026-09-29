"""Google Gemini adapter (native ``generateContent``).

Same pattern as the Bedrock adapter: the transcript stays normalized, this file
narrows it into Gemini's ``contents``/``parts`` on the way out and widens the
reply back on the way in.

Two Gemini quirks shape the translation:

* A ``functionResponse`` is matched to its call by *name*, not id, so the
  request builder remembers which name each ``tool_use`` id belonged to.
* Gemini 3 signs its function calls (``thoughtSignature``) and rejects a
  history that drops the signature.  It is kept on the normalized ``tool_use``
  block under ``gemini_signature`` and sent back unchanged.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any

from .base import Provider, Response, ToolCall

API_BASE = "https://generativelanguage.googleapis.com/v1beta"

# Gemini's finish reasons, narrowed to the names the rest of the agent uses.
STOP_REASONS = {"STOP": "end_turn", "MAX_TOKENS": "max_tokens"}


class GeminiProvider(Provider):
    name = "gemini"

    def __init__(self, api_key: str | None = None, timeout: int = 600) -> None:
        self.api_key = api_key or os.environ.get("GEMINI_API_KEY", "")
        if not self.api_key:
            raise RuntimeError(
                "GEMINI_API_KEY is not set. Create a key in Google AI Studio and put it in .env."
            )
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
        body = build_request(system=system, messages=messages, tools=tools, max_tokens=max_tokens)
        url = f"{API_BASE}/models/{urllib.parse.quote(model, safe='')}:generateContent"
        request = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers={"content-type": "application/json", "x-goog-api-key": self.api_key},
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:  # surface the API's own message
            detail = exc.read().decode("utf-8", "replace")[:2000]
            raise RuntimeError(f"Gemini API error {exc.code}: {detail}") from exc

        return parse_response(data)


# -- normalized -> Gemini -----------------------------------------------------


def build_request(
    *,
    system: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    max_tokens: int,
) -> dict[str, Any]:
    names: dict[str, str] = {}  # tool_use id -> tool name, for functionResponse
    body: dict[str, Any] = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [_to_content(m, names) for m in messages],
        "generationConfig": {"maxOutputTokens": max_tokens},
    }
    if tools:
        body["tools"] = [
            {
                "functionDeclarations": [
                    {
                        "name": tool["name"],
                        "description": tool.get("description", ""),
                        # The full JSON Schema field; ``parameters`` only takes
                        # an OpenAPI subset and rejects common keywords.
                        "parametersJsonSchema": tool["input_schema"],
                    }
                    for tool in tools
                ]
            }
        ]
    return body


def _to_content(message: dict[str, Any], names: dict[str, str]) -> dict[str, Any]:
    role = "model" if message["role"] == "assistant" else "user"
    content = message.get("content")
    if isinstance(content, str):
        parts = [{"text": content}] if content else []
    else:
        parts = [p for p in (_to_part(block, names) for block in content or []) if p]
    return {"role": role, "parts": parts}


def _to_part(block: dict[str, Any], names: dict[str, str]) -> dict[str, Any] | None:
    kind = block.get("type")
    if kind == "text":
        return {"text": block["text"]} if block.get("text") else None
    if kind == "tool_use":
        names[block["id"]] = block["name"]
        part: dict[str, Any] = {
            "functionCall": {"id": block["id"], "name": block["name"], "args": block.get("input") or {}}
        }
        if block.get("gemini_signature"):
            part["thoughtSignature"] = block["gemini_signature"]
        return part
    if kind == "tool_result":
        key = "error" if block.get("is_error") else "result"
        return {
            "functionResponse": {
                "id": block["tool_use_id"],
                "name": names.get(block["tool_use_id"], ""),
                "response": {key: _result_text(block.get("content"))},
            }
        }
    raise ValueError(f"Gemini adapter cannot send a {kind!r} block")


def _result_text(content: Any) -> str:
    if isinstance(content, list):
        content = "".join(b.get("text", "") for b in content if isinstance(b, dict))
    return str(content) if content else "(no output)"


# -- Gemini -> normalized -----------------------------------------------------


def parse_response(data: dict[str, Any]) -> Response:
    text_parts: list[str] = []
    tool_calls: list[ToolCall] = []
    raw_content: list[dict[str, Any]] = []

    candidate = (data.get("candidates") or [{}])[0]
    for part in (candidate.get("content") or {}).get("parts") or []:
        if part.get("thought"):
            continue  # thought summaries are for humans, not for replay
        if "functionCall" in part:
            call = part["functionCall"]
            # Older models omit the id; the transcript needs one to pair results.
            call_id = call.get("id") or f"call_{uuid.uuid4().hex[:12]}"
            arguments = call.get("args") or {}
            tool_calls.append(ToolCall(id=call_id, name=call["name"], arguments=arguments))
            block = {"type": "tool_use", "id": call_id, "name": call["name"], "input": arguments}
            if part.get("thoughtSignature"):
                block["gemini_signature"] = part["thoughtSignature"]
            raw_content.append(block)
        elif part.get("text"):
            text_parts.append(part["text"])
            raw_content.append({"type": "text", "text": part["text"]})

    usage = data.get("usageMetadata") or {}
    finish = candidate.get("finishReason", "")
    return Response(
        text="".join(text_parts),
        tool_calls=tool_calls,
        raw_content=raw_content,
        input_tokens=usage.get("promptTokenCount", 0),
        output_tokens=usage.get("candidatesTokenCount", 0),
        stop_reason="tool_use" if tool_calls else STOP_REASONS.get(finish, finish.lower()),
    )
