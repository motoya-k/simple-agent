"""pi as the engine — hand the turn to https://github.com/badlogic/pi-mono.

Each turn runs ``pi -p --mode json`` as a subprocess and reads its event stream.
pi keeps its own session file per conversation (under ``<home>/pi/``), so it
resumes with its own tool history; simple-agent's transcript records the
user's message and pi's final answer, which is what search, compaction and the
background review need.

What pi takes over: the loop, its tools, its context handling, and the model
call (configure pi's provider and model through ``pi_args``, e.g.
``--provider amazon-bedrock --model jp.anthropic.claude-sonnet-4-6``).

What stays here: memory and the skill catalogue reach pi through
``--append-system-prompt``, and the toolset is **never widened**.  The agent's
tools are mapped to pi's by name; a tool with no pi counterpart is dropped, and
an agent whose narrowed toolset maps to nothing runs pi with ``--no-tools``.
A route that took the terminal away keeps it away under pi too.
"""

from __future__ import annotations

import json
import shlex
import subprocess
from typing import TYPE_CHECKING, Any, Callable

from ..loop import Turn
from ..session import session_slug
from . import Engine, EventCallback

if TYPE_CHECKING:
    from ..agent import Agent

# simple-agent tool name -> pi built-in tool name.
TOOL_MAP = {
    "terminal": "bash",
    "read_file": "read",
    "write_file": "write",
    "edit_file": "edit",
}


class PiEngine(Engine):
    name = "pi"

    def __init__(self, command: str = "pi", args: str = "") -> None:
        self.command = shlex.split(command) or ["pi"]
        self.args = shlex.split(args)

    def build_command(self, agent: "Agent", prompt: str) -> list[str]:
        session_dir = agent.config.home / "pi"
        session_dir.mkdir(parents=True, exist_ok=True)
        tools = sorted({TOOL_MAP[n] for n in agent.registry.names() if n in TOOL_MAP})
        return [
            *self.command,
            "-p",
            "--mode", "json",
            "--session", str(session_dir / f"{session_slug(agent.session_key)}.jsonl"),
            *(["--tools", ",".join(tools)] if tools else ["--no-tools"]),
            "--append-system-prompt", agent.system,
            *self.args,
            prompt,
        ]

    def run(self, agent: "Agent", *, on_event: EventCallback, interrupt: Callable[[], bool]) -> Turn:
        turn = Turn()
        command = self.build_command(agent, last_user_text(agent.messages))
        process = subprocess.Popen(
            command,
            cwd=agent.cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        answer = ""
        assert process.stdout is not None
        for line in process.stdout:
            if interrupt():
                process.terminate()
                turn.stopped_by = "interrupt"
                break
            event = _json(line)
            kind = event.get("type")
            if kind == "tool_execution_start":
                turn.tool_calls += 1
                _emit(turn, on_event, "tool", str(event.get("toolName", "")))
            elif kind == "turn_end":
                turn.iterations += 1
            elif kind == "message_end":
                message = event.get("message") or {}
                if message.get("role") == "assistant":
                    answer = assistant_text(message) or answer
                    turn.tokens += _tokens(message)
        stderr = process.stderr.read() if process.stderr else ""
        code = process.wait()

        if turn.stopped_by != "interrupt" and code != 0:
            raise RuntimeError(f"pi exited with {code}: {stderr.strip()[:2000]}")
        turn.text = answer
        # Keep the transcript valid for our side: every user turn gets an
        # assistant turn, even when pi was stopped before it said anything.
        agent.messages.append(
            {"role": "assistant", "content": [{"type": "text", "text": answer or "(no answer)"}]}
        )
        return turn


def last_user_text(messages: list[dict[str, Any]]) -> str:
    """The text of the last user turn — tool results are ours, not pi's."""
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content
        return "\n".join(b.get("text", "") for b in content or [] if b.get("type") == "text")
    return ""


def assistant_text(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    return "".join(b.get("text", "") for b in content or [] if b.get("type") == "text")


def _tokens(message: dict[str, Any]) -> int:
    usage = message.get("usage") or {}
    return int(usage.get("input", 0) or 0) + int(usage.get("output", 0) or 0)


def _json(line: str) -> dict[str, Any]:
    try:
        value = json.loads(line)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _emit(turn: Turn, on_event: EventCallback, kind: str, text: str) -> None:
    turn.events.append((kind, text))
    if on_event:
        on_event(kind, text)
