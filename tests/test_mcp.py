"""The MCP tool server external harnesses use to reach our memory and tools."""

from __future__ import annotations

import io
import json

from simple_agent.config import Config
from simple_agent.mcp import build_tool_registry, serve


def exchange(registry, *requests):
    stdin = io.StringIO("".join(json.dumps(r) + "\n" for r in requests))
    stdout = io.StringIO()
    serve(registry, session_key="k", stdin=stdin, stdout=stdout)
    return [json.loads(line) for line in stdout.getvalue().splitlines()]


def test_lists_only_the_allowed_tools_and_calls_them(tmp_path):
    config = Config(home=tmp_path)
    registry = build_tool_registry(config, ["memory_save", "skill_view"])

    init, listed, saved, missing = exchange(
        registry,
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "memory_save", "arguments": {"content": "prefers tea"}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "terminal", "arguments": {}}},
    )

    assert init["result"]["capabilities"] == {"tools": {}}
    assert sorted(t["name"] for t in listed["result"]["tools"]) == ["memory_save", "skill_view"]
    assert saved["result"]["isError"] is False
    assert missing["result"]["isError"] is True  # not on the allowlist, so it does not exist


def test_unknown_methods_and_bad_json_get_errors(tmp_path):
    registry = build_tool_registry(Config(home=tmp_path), [])
    stdin = io.StringIO('{"jsonrpc":"2.0","id":9,"method":"resources/list"}\nnot json\n')
    stdout = io.StringIO()
    serve(registry, stdin=stdin, stdout=stdout)
    first, second = (json.loads(line) for line in stdout.getvalue().splitlines())
    assert first["error"]["code"] == -32601
    assert second["error"]["code"] == -32700
