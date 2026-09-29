"""Adapter translation tests — no network.

Each adapter must carry a tool call and its result through its own wire format
and back without losing the pairing the loop depends on.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from simple_agent.providers import gemini, openai_responses  # noqa: E402

TOOLS = [
    {
        "name": "terminal",
        "description": "Run a command",
        "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}},
    }
]

TRANSCRIPT = [
    {"role": "user", "content": "list files"},
    {
        "role": "assistant",
        "content": [
            {"type": "text", "text": "Looking."},
            {"type": "tool_use", "id": "c1", "name": "terminal", "input": {"command": "ls"}},
        ],
    },
    {
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": "c1", "content": "", "is_error": False}
        ],
    },
]


# -- Gemini ---------------------------------------------------------------


def test_gemini_names_each_result_after_its_call():
    body = gemini.build_request(system="sys", messages=TRANSCRIPT, tools=TOOLS, max_tokens=100)

    assert body["systemInstruction"] == {"parts": [{"text": "sys"}]}
    assert [c["role"] for c in body["contents"]] == ["user", "model", "user"]
    call = body["contents"][1]["parts"][1]["functionCall"]
    assert call == {"id": "c1", "name": "terminal", "args": {"command": "ls"}}
    result = body["contents"][2]["parts"][0]["functionResponse"]
    assert result == {"id": "c1", "name": "terminal", "response": {"result": "(no output)"}}
    decl = body["tools"][0]["functionDeclarations"][0]
    assert decl["parametersJsonSchema"] == TOOLS[0]["input_schema"]


def test_gemini_round_trips_the_thought_signature():
    response = gemini.parse_response(
        {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {"text": "thinking", "thought": True},
                            {
                                "functionCall": {"name": "terminal", "args": {"command": "pwd"}},
                                "thoughtSignature": "sig",
                            },
                        ]
                    },
                    "finishReason": "STOP",
                }
            ],
            "usageMetadata": {"promptTokenCount": 7, "candidatesTokenCount": 3},
        }
    )

    assert response.text == ""
    assert response.stop_reason == "tool_use"
    assert (response.input_tokens, response.output_tokens) == (7, 3)
    call = response.tool_calls[0]
    assert call.id  # minted when the model omits one
    assert response.raw_content[0]["gemini_signature"] == "sig"

    replay = gemini.build_request(
        system="",
        messages=[{"role": "assistant", "content": response.raw_content}],
        tools=[],
        max_tokens=10,
    )
    assert replay["contents"][0]["parts"][0]["thoughtSignature"] == "sig"


# -- OpenAI Responses -----------------------------------------------------


def test_openai_flattens_turns_into_items():
    body = openai_responses.build_request(
        system="sys", messages=TRANSCRIPT, tools=TOOLS, max_tokens=100, model="gpt-5-codex"
    )

    assert body["instructions"] == "sys"
    assert body["store"] is False
    assert body["input"] == [
        {"role": "user", "content": "list files"},
        {"role": "assistant", "content": "Looking."},
        {"type": "function_call", "call_id": "c1", "name": "terminal", "arguments": '{"command": "ls"}'},
        {"type": "function_call_output", "call_id": "c1", "output": "(no output)"},
    ]
    assert body["tools"][0]["parameters"] == TOOLS[0]["input_schema"]


def test_openai_parses_calls_and_skips_reasoning():
    response = openai_responses.parse_response(
        {
            "output": [
                {"type": "reasoning", "summary": []},
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": "Running it."}],
                },
                {
                    "type": "function_call",
                    "call_id": "c9",
                    "name": "terminal",
                    "arguments": '{"command": "pwd"}',
                },
            ],
            "usage": {"input_tokens": 11, "output_tokens": 5},
        }
    )

    assert response.text == "Running it."
    assert response.stop_reason == "tool_use"
    assert response.tool_calls[0].arguments == {"command": "pwd"}
    assert [b["type"] for b in response.raw_content] == ["text", "tool_use"]


def test_openai_reports_truncation_as_max_tokens():
    response = openai_responses.parse_response(
        {"output": [], "status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}}
    )
    assert response.stop_reason == "max_tokens"
