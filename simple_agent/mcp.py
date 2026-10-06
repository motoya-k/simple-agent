"""``simple-agent --mcp`` — this agent's tools, served over MCP on stdio.

How another harness (Claude Code, Codex, Goose, ...) reaches this agent's
memory, skills, and session search: register this server in that harness and
its model reads and writes the *same* memory as ours, whichever backend is
configured.  The harness owns its loop; we own what the loop can touch.

``--profile`` lends that harness one of our profiles — its toolset and its
memory namespace — and ``--tools`` narrows whatever is left, so a harness can
be handed strictly less than the profile allows but never more.

Newline-delimited JSON-RPC 2.0, the MCP stdio transport.  Only what a tool
server needs: ``initialize``, ``tools/list``, ``tools/call``, ``ping``.
"""

from __future__ import annotations

import json
import os
import sys
from typing import IO, Any

from .context import session_scope
from .tools import ToolRegistry

PROTOCOL_VERSION = "2025-06-18"
SERVER_INFO = {"name": "simple-agent", "version": "0.2.0"}


def build_tool_registry(
    config: Any, tools: list[str] | None, profile_name: str = ""
) -> ToolRegistry:
    """The toolset an Agent on this profile would get, narrowed to ``tools``.

    Both narrowings apply, in that order: a profile that withholds the
    terminal cannot have it handed back by ``--tools``.
    """
    from .memory import open_memory
    from .profile import load_profile
    from .skills import open_skills
    from .state import open_store
    from .tools import build_registry

    profile = load_profile(config, profile_name)
    registry = build_registry(
        config,
        open_memory(config, profile.namespace),
        open_skills(config),
        open_store(config),
    )
    if profile.tools is not None:
        registry = registry.subset(list(profile.tools))
    return registry if tools is None else registry.subset(tools)


def handle(registry: ToolRegistry, request: dict[str, Any], session_key: str) -> dict[str, Any] | None:
    """One JSON-RPC message in, one response out (``None`` for notifications)."""
    method = request.get("method", "")
    if "id" not in request:
        return None  # notifications/initialized and friends need no answer
    params = request.get("params") or {}

    if method == "initialize":
        result: Any = {
            "protocolVersion": params.get("protocolVersion") or PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": SERVER_INFO,
        }
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {
            "tools": [
                {"name": s["name"], "description": s["description"], "inputSchema": s["input_schema"]}
                for s in registry.schemas()
            ]
        }
    elif method == "tools/call":
        with session_scope(session_key):
            output, is_error = registry.call(params.get("name", ""), params.get("arguments") or {})
        result = {"content": [{"type": "text", "text": output}], "isError": is_error}
    else:
        return {
            "jsonrpc": "2.0",
            "id": request["id"],
            "error": {"code": -32601, "message": f"Method not found: {method}"},
        }
    return {"jsonrpc": "2.0", "id": request["id"], "result": result}


def serve(
    registry: ToolRegistry,
    *,
    session_key: str = "",
    stdin: IO[str] = sys.stdin,
    stdout: IO[str] = sys.stdout,
) -> None:
    for line in stdin:
        if not line.strip():
            continue
        try:
            request = json.loads(line)
        except ValueError:
            response: dict[str, Any] | None = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": "Parse error"},
            }
        else:
            response = handle(registry, request, session_key)
        if response is not None:
            stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            stdout.flush()


def main(argv: list[str], config: Any) -> int:
    tools: list[str] | None = None
    profile_name = ""
    # Also from the environment, for harnesses that take an MCP server as one
    # command string, where a key with spaces in it would not survive.
    session_key = os.environ.get("SIMPLE_AGENT_MCP_SESSION_KEY", "")
    args = iter(argv)
    for arg in args:
        if arg == "--tools":
            tools = [t for t in next(args, "").split(",") if t]
        elif arg == "--profile":
            profile_name = next(args, "")
        elif arg == "--session-key":
            session_key = next(args, "")
        else:
            print(f"unknown argument: {arg}", file=sys.stderr)
            return 2
    try:
        registry = build_tool_registry(config, tools, profile_name)
    except ValueError as exc:
        print(f"{exc}", file=sys.stderr)
        return 2
    serve(registry, session_key=session_key)
    return 0
