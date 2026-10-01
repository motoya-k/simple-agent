"""Import tools from external MCP servers — the other direction of :mod:`.mcp`.

Servers are declared in ``<home>/mcp.json`` in the format Claude Desktop,
Cursor, and Claude Code already use, so an existing entry can be pasted in::

    {"mcpServers": {"google": {"command": "uvx", "args": ["..."], "env": {"KEY": "..."}}}}

Each server's tools join the agent's registry as ``<server>__<tool>``.  From
there they are ordinary tools: a route's allowlist narrows them like any other
(``google__calendar_*`` works), and external harnesses receive them through
``simple-agent --mcp`` like every other bridged tool — so a server is declared
once and every engine can use it.

One process per server, shared by every agent in this process and started on
first use.  A server that fails to start is logged and skipped; the agent runs
without it rather than not at all.

Stdio transport only, standard library only.
"""

from __future__ import annotations

import atexit
import itertools
import json
import logging
import os
import queue
import re
import subprocess
import threading
from pathlib import Path
from typing import Any

from .tools import Tool, ToolRegistry

log = logging.getLogger(__name__)

PROTOCOL_VERSION = "2025-06-18"
CLIENT_INFO = {"name": "simple-agent", "version": "0.2.0"}
TIMEOUT = 120.0
_SAFE = re.compile(r"[^A-Za-z0-9_-]")


class McpError(RuntimeError):
    pass


class McpClient:
    """One stdio MCP server: start it, list its tools, call them."""

    def __init__(self, name: str, command: str, args: list[str], env: dict[str, str] | None = None) -> None:
        self.name = name
        self.process = subprocess.Popen(
            [command, *args],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env={**os.environ, **(env or {})},
            text=True,
            bufsize=1,
        )
        self._ids = itertools.count(1)
        self._waiting: dict[int, queue.Queue] = {}
        self._lock = threading.Lock()
        threading.Thread(target=self._read, name=f"mcp-{name}", daemon=True).start()
        self.request(
            "initialize",
            {"protocolVersion": PROTOCOL_VERSION, "capabilities": {}, "clientInfo": CLIENT_INFO},
        )
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _send(self, message: dict[str, Any]) -> None:
        assert self.process.stdin is not None
        with self._lock:
            self.process.stdin.write(json.dumps(message) + "\n")
            self.process.stdin.flush()

    def _read(self) -> None:
        assert self.process.stdout is not None
        for line in self.process.stdout:
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if "method" in message:
                if "id" in message:  # a request from the server: we offer nothing
                    self._send({
                        "jsonrpc": "2.0", "id": message["id"],
                        "error": {"code": -32601, "message": "Not supported by this client"},
                    })
                continue
            waiter = self._waiting.pop(message.get("id"), None)
            if waiter is not None:
                waiter.put(message)
        for waiter in list(self._waiting.values()):  # the server exited
            waiter.put({"error": {"message": f"MCP server {self.name!r} exited"}})

    def request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        request_id = next(self._ids)
        waiter: queue.Queue = queue.Queue(maxsize=1)
        self._waiting[request_id] = waiter
        self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}})
        try:
            reply = waiter.get(timeout=TIMEOUT)
        except queue.Empty:
            self._waiting.pop(request_id, None)
            raise McpError(f"{self.name}: {method} timed out after {TIMEOUT:.0f}s") from None
        if "error" in reply:
            raise McpError(f"{self.name}: {reply['error'].get('message', reply['error'])}")
        return reply.get("result")

    def tools(self) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        cursor = None
        while True:
            result = self.request("tools/list", {"cursor": cursor} if cursor else {}) or {}
            found += result.get("tools") or []
            cursor = result.get("nextCursor")
            if not cursor:
                return found

    def call(self, tool: str, arguments: dict[str, Any]) -> tuple[str, bool]:
        result = self.request("tools/call", {"name": tool, "arguments": arguments}) or {}
        parts = []
        for block in result.get("content") or []:
            if block.get("type") == "text":
                parts.append(block.get("text", ""))
            else:  # images, resources: say what came back rather than drop it
                parts.append(f"[{block.get('type', 'content')} omitted]")
        if result.get("structuredContent") is not None and not parts:
            parts.append(json.dumps(result["structuredContent"], ensure_ascii=False))
        return "\n".join(parts), bool(result.get("isError"))

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()


_clients: dict[tuple, McpClient | None] = {}
_clients_lock = threading.Lock()


def _client(name: str, spec: dict[str, Any]) -> McpClient | None:
    key = (name, spec.get("command"), tuple(spec.get("args") or ()), json.dumps(spec.get("env") or {}, sort_keys=True))
    with _clients_lock:
        if key not in _clients:
            try:
                _clients[key] = McpClient(name, spec["command"], list(spec.get("args") or []), spec.get("env"))
            except (OSError, KeyError, McpError) as exc:
                log.warning("MCP server %r not available: %s", name, exc)
                _clients[key] = None  # do not retry on every agent
        return _clients[key]


@atexit.register
def _close_all() -> None:
    for client in _clients.values():
        if client is not None:
            client.close()


def load_servers(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text("utf-8"))
    servers = data.get("mcpServers", data)
    return {name: spec for name, spec in servers.items() if isinstance(spec, dict) and spec.get("command")}


def register_servers(registry: ToolRegistry, path: Path) -> None:
    """Add every tool of every server in ``path`` to ``registry``."""
    for server, spec in load_servers(path).items():
        if spec.get("disabled"):
            continue
        client = _client(server, spec)
        if client is None:
            continue
        try:
            tools = client.tools()
        except McpError as exc:
            log.warning("MCP server %r: could not list tools: %s", server, exc)
            continue
        for tool in tools:
            registry.register(_wrap(client, server, tool))


def _wrap(client: McpClient, server: str, tool: dict[str, Any]) -> Tool:
    remote = tool["name"]
    # Model APIs accept [A-Za-z0-9_-]{1,64} as tool names.
    name = _SAFE.sub("_", f"{server}__{remote}")[:64]

    def call(**arguments: Any) -> str:
        output, is_error = client.call(remote, arguments)
        if is_error:
            raise McpError(output or f"{remote} failed")
        return output

    annotations = tool.get("annotations") or {}
    return Tool(
        name=name,
        description=tool.get("description") or f"{remote} (from MCP server {server})",
        parameters=tool.get("inputSchema") or {"type": "object", "properties": {}},
        fn=call,
        # Only a server that says so is trusted to run concurrently.
        parallel_safe=bool(annotations.get("readOnlyHint")),
    )
