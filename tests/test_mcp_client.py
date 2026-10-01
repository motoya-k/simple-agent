"""Importing tools from an external MCP server — against a tiny fake server."""

from __future__ import annotations

import json
import sys
import textwrap

from simple_agent.mcp_client import load_servers, register_servers
from simple_agent.tools import ToolRegistry

FAKE_SERVER = textwrap.dedent(
    """
    import json, sys
    tools = [
        {"name": "list_events", "description": "List events", "annotations": {"readOnlyHint": True},
         "inputSchema": {"type": "object", "properties": {"day": {"type": "string"}}}},
        {"name": "create.event", "description": "Create an event",
         "inputSchema": {"type": "object", "properties": {"title": {"type": "string"}}}},
    ]
    for line in sys.stdin:
        msg = json.loads(line)
        if "id" not in msg or "method" not in msg:  # notifications, replies to us
            continue
        method, args = msg["method"], msg.get("params", {}).get("arguments", {})
        if method == "initialize":
            # a server may ask the client something first; the client must cope
            print(json.dumps({"jsonrpc": "2.0", "id": "srv-1", "method": "roots/list"}), flush=True)
            result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "fake"}}
        elif method == "tools/list":
            result = {"tools": tools}
        elif method == "tools/call" and msg["params"]["name"] == "list_events":
            result = {"content": [{"type": "text", "text": "standup on " + args.get("day", "?")}]}
        else:
            result = {"content": [{"type": "text", "text": "calendar is read-only"}], "isError": True}
        print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result}), flush=True)
    """
)


def write_config(tmp_path, servers):
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps({"mcpServers": servers}))
    return path


def test_imports_tools_under_the_server_name_and_calls_them(tmp_path):
    script = tmp_path / "server.py"
    script.write_text(FAKE_SERVER)
    path = write_config(tmp_path, {"cal": {"command": sys.executable, "args": [str(script)]}})
    registry = ToolRegistry()

    register_servers(registry, path)

    assert registry.names() == ["cal__create_event", "cal__list_events"]  # "." made safe
    assert registry.get("cal__list_events").parallel_safe is True  # readOnlyHint
    assert registry.get("cal__create_event").parallel_safe is False
    assert registry.call("cal__list_events", {"day": "Mon"}) == ("standup on Mon", False)
    output, is_error = registry.call("cal__create_event", {"title": "x"})
    assert is_error and "read-only" in output


def test_patterns_narrow_imported_tools_like_any_other(tmp_path):
    script = tmp_path / "server.py"
    script.write_text(FAKE_SERVER)
    registry = ToolRegistry()
    register_servers(registry, write_config(tmp_path, {"cal2": {"command": sys.executable, "args": [str(script)]}}))

    assert registry.subset(["cal2__list_*"]).names() == ["cal2__list_events"]


def test_a_server_that_cannot_start_is_skipped(tmp_path, caplog):
    registry = ToolRegistry()
    path = write_config(tmp_path, {
        "missing": {"command": str(tmp_path / "no-such-binary")},
        "off": {"command": sys.executable, "disabled": True},
    })
    register_servers(registry, path)
    assert registry.names() == []
    assert "not available" in caplog.text


def test_plain_and_wrapped_config_shapes(tmp_path):
    (tmp_path / "a.json").write_text(json.dumps({"x": {"command": "c"}}))
    (tmp_path / "b.json").write_text(json.dumps({"mcpServers": {"x": {"command": "c"}, "bad": {}}}))
    assert list(load_servers(tmp_path / "a.json")) == ["x"]
    assert list(load_servers(tmp_path / "b.json")) == ["x"]
    assert load_servers(tmp_path / "none.json") == {}
