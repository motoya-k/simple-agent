"""Anthropic Messages adapter — the one concrete provider in this repo.

Zero dependencies on purpose: ``urllib`` keeps the whole request/response path
visible in one screen.  Swapping in the official SDK is a drop-in change that
touches only this file.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

from .base import Provider, Response, ToolCall

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"


class AnthropicProvider(Provider):
    name = "anthropic"

    def __init__(self, api_key: str | None = None, timeout: int = 600) -> None:
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        if not self.api_key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not set. Copy .env.example to .env and fill it in."
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
        body: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": messages,
            # A single cache breakpoint at the end of the system prompt. The
            # prefix is immutable for the life of the session (see memory.py),
            # so every turn after the first reads the prompt from cache.
            "system": [
                {
                    "type": "text",
                    "text": system,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
        }
        if tools:
            body["tools"] = tools

        payload = json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            API_URL,
            data=payload,
            headers={
                "content-type": "application/json",
                "x-api-key": self.api_key,
                "anthropic-version": API_VERSION,
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:  # surface the API's own message
            detail = exc.read().decode("utf-8", "replace")[:2000]
            raise RuntimeError(f"Anthropic API error {exc.code}: {detail}") from exc

        return self._normalize(data)

    @staticmethod
    def _normalize(data: dict[str, Any]) -> Response:
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        content = data.get("content") or []

        for block in content:
            if block.get("type") == "text":
                text_parts.append(block.get("text", ""))
            elif block.get("type") == "tool_use":
                tool_calls.append(
                    ToolCall(
                        id=block["id"],
                        name=block["name"],
                        arguments=block.get("input") or {},
                    )
                )

        usage = data.get("usage") or {}
        return Response(
            text="".join(text_parts),
            tool_calls=tool_calls,
            raw_content=content,
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
            stop_reason=data.get("stop_reason", ""),
        )
