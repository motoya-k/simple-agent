"""Amazon Bedrock Converse adapter.

Converse is Bedrock's model-agnostic shape: one request format for Claude,
Nova, Llama, Mistral and the rest, so a model swap is a config change rather
than a new adapter.  The price is a translation step.  The transcript stays in
the normalized (Anthropic-shaped) format described in :mod:`.base`; this file
narrows it into Converse on the way out and widens the reply back on the way
in, so nothing outside it ever sees a Converse payload.

Two ways to authenticate, both on ``urllib`` with no boto3:

* a **Bedrock API key** (``AWS_BEARER_TOKEN_BEDROCK``), sent as a bearer token.
  Keys are minted per region, so the key and the region must agree.
* **IAM credentials** — ``AWS_ACCESS_KEY_ID`` / ``AWS_SECRET_ACCESS_KEY`` or an
  ``AWS_PROFILE`` in ``~/.aws/credentials`` — signed with SigV4 (see
  :mod:`.sigv4`).  This is what an existing AWS setup usually already has.

The API key wins when both are present, as it does in the AWS SDKs.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .base import Provider, Response, ToolCall
from .sigv4 import Credentials, load_credentials, profile_region, sign

DEFAULT_REGION = "ap-northeast-1"

# Converse rejects a tool result with empty text, but "the command printed
# nothing" is a real answer the model needs to see.
EMPTY_TOOL_RESULT = "(no output)"


class BedrockProvider(Provider):
    name = "bedrock"

    def __init__(
        self,
        api_key: str | None = None,
        region: str | None = None,
        timeout: int = 600,
        credentials: Credentials | None = None,
    ) -> None:
        self.api_key = api_key or os.environ.get("AWS_BEARER_TOKEN_BEDROCK", "")
        self.credentials = None if self.api_key else (credentials or load_credentials())
        if not self.api_key and self.credentials is None:
            raise RuntimeError(
                "No Bedrock credentials. Set AWS_BEARER_TOKEN_BEDROCK (a Bedrock API "
                "key), AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY, or AWS_PROFILE."
            )
        self.region = (
            region
            or os.environ.get("AWS_REGION")
            or os.environ.get("AWS_DEFAULT_REGION")
            or profile_region()
            or DEFAULT_REGION
        )
        self.timeout = timeout

    def endpoint(self, model: str) -> str:
        # Inference profile ids contain ':' and '.', so quote the whole segment.
        model_id = urllib.parse.quote(model, safe="")
        return f"https://bedrock-runtime.{self.region}.amazonaws.com/model/{model_id}/converse"

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
        url = self.endpoint(model)
        payload = json.dumps(body).encode("utf-8")
        headers = {"content-type": "application/json"}
        if self.api_key:
            headers["authorization"] = f"Bearer {self.api_key}"
        else:
            assert self.credentials is not None
            headers = sign(
                method="POST", url=url, headers=headers, body=payload,
                credentials=self.credentials, region=self.region, service="bedrock",
            )
        request = urllib.request.Request(url, data=payload, headers=headers, method="POST")

        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:  # surface the API's own message
            detail = exc.read().decode("utf-8", "replace")[:2000]
            raise RuntimeError(f"Bedrock API error {exc.code}: {detail}") from exc

        return parse_response(data)


# -- normalized -> Converse ---------------------------------------------------


def build_request(
    *,
    system: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    max_tokens: int,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "messages": [_to_converse_message(m) for m in messages],
        # Same single cache breakpoint as the Anthropic adapter: the system
        # prompt is immutable for the life of the session (see memory.py).
        "system": [{"text": system}, {"cachePoint": {"type": "default"}}],
        "inferenceConfig": {"maxTokens": max_tokens},
    }
    if tools:
        body["toolConfig"] = {
            "tools": [
                {
                    "toolSpec": {
                        "name": tool["name"],
                        "description": tool.get("description", ""),
                        "inputSchema": {"json": tool["input_schema"]},
                    }
                }
                for tool in tools
            ]
        }
    return body


def _to_converse_message(message: dict[str, Any]) -> dict[str, Any]:
    content = message.get("content")
    if isinstance(content, str):
        blocks = [{"text": content}] if content else []
    else:
        blocks = [b for b in (_to_converse_block(block) for block in content or []) if b]
    return {"role": message["role"], "content": blocks}


def _to_converse_block(block: dict[str, Any]) -> dict[str, Any] | None:
    kind = block.get("type")
    if kind == "text":
        # Converse rejects empty text blocks; they carry nothing anyway.
        return {"text": block["text"]} if block.get("text") else None
    if kind == "tool_use":
        return {
            "toolUse": {
                "toolUseId": block["id"],
                "name": block["name"],
                "input": block.get("input") or {},
            }
        }
    if kind == "tool_result":
        return {
            "toolResult": {
                "toolUseId": block["tool_use_id"],
                "content": [{"text": _result_text(block.get("content"))}],
                "status": "error" if block.get("is_error") else "success",
            }
        }
    raise ValueError(f"Bedrock adapter cannot send a {kind!r} block")


def _result_text(content: Any) -> str:
    if isinstance(content, list):
        content = "".join(b.get("text", "") for b in content if isinstance(b, dict))
    return str(content) if content else EMPTY_TOOL_RESULT


# -- Converse -> normalized ---------------------------------------------------


def parse_response(data: dict[str, Any]) -> Response:
    text_parts: list[str] = []
    tool_calls: list[ToolCall] = []
    raw_content: list[dict[str, Any]] = []

    for block in (data.get("output") or {}).get("message", {}).get("content") or []:
        if "text" in block:
            text_parts.append(block["text"])
            raw_content.append({"type": "text", "text": block["text"]})
        elif "toolUse" in block:
            use = block["toolUse"]
            arguments = use.get("input") or {}
            tool_calls.append(ToolCall(id=use["toolUseId"], name=use["name"], arguments=arguments))
            raw_content.append(
                {"type": "tool_use", "id": use["toolUseId"], "name": use["name"], "input": arguments}
            )
        # reasoningContent and other block kinds are not replayed: the loop
        # never sends them back, and dropping them keeps the transcript normalized.

    usage = data.get("usage") or {}
    return Response(
        text="".join(text_parts),
        tool_calls=tool_calls,
        raw_content=raw_content,
        input_tokens=usage.get("inputTokens", 0),
        output_tokens=usage.get("outputTokens", 0),
        # Converse already uses the normalized names for every reason the loop
        # branches on (end_turn / tool_use / max_tokens).
        stop_reason=data.get("stopReason", ""),
    )
