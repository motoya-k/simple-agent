"""mini-swe-agent as the engine — the smallest serious agent there is.

https://github.com/SWE-agent/mini-swe-agent — the Princeton/Stanford SWE-bench
baseline: a loop of about two hundred lines whose only tool is bash.  Worth
having as the opposite pole to everything else here: if a task can be done
with a shell and a model, this shows how little harness it takes.

What follows from "bash is the only tool":

* **It needs a shell.**  It runs only when the agent may use ``terminal``;
  on a route that took the shell away it refuses, rather than hand one back.
* **No MCP, no memory tools.**  Memory and skills reach it as context in the
  task text — read, not written.
* **No sessions.**  Each turn starts fresh, so the recent conversation is
  replayed into the task.

Bounded by ``agent.step_limit`` (mini's cost limit needs model prices it may
not know).  The trajectory file is where the answer is read from.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Callable

from ..loop import Turn
from ..session import session_slug
from .external import ExternalEngine, text_of

if TYPE_CHECKING:
    from ..agent import Agent

STEP_LIMIT = 30
HISTORY = 10  # earlier messages replayed into the task

# mini takes everything a command prints after this marker line as the final
# output. Models tend to submit the bare marker and leave the answer behind in
# an earlier message, so the convention is spelled out in every task.
SUBMIT = (
    "When you are done, finish with ONE command whose output is the line "
    "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT followed by your complete answer for "
    "the user, e.g.: printf 'COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\\n%s\\n' '<answer>'"
)


class MiniSweEngine(ExternalEngine):
    name = "mini-swe"
    default_command = "mini"
    # LiteLLM model prefixes.
    provider_map = {"anthropic": "anthropic", "bedrock": "bedrock", "gemini": "gemini", "openai": "openai"}

    def _dir(self, agent: "Agent"):
        path = agent.config.home / "mini-swe"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _trajectory(self, agent: "Agent"):
        return self._dir(agent) / f"{session_slug(agent.session_key)}.traj.json"

    def build_command(self, agent: "Agent", prompt: str) -> list[str]:
        if "terminal" not in agent.registry.names():
            raise RuntimeError(
                "mini-swe-agent's only tool is a shell, and this conversation may not "
                "use one (terminal is not allowed). Use another engine for this route."
            )
        command = [
            *self.command,
            "-y", "--exit-immediately",
            "-l", "0",
            "-c", "mini.yaml", "-c", f"agent.step_limit={STEP_LIMIT}",
            "-o", str(self._trajectory(agent)),
        ]
        if not self.has_flag("-m", "--model"):
            command += ["-m", f"{self.provider_map[agent.config.provider]}/{agent.config.model}"]
        return [*command, *self.args, "-t", self.task(agent, prompt)]

    def task(self, agent: "Agent", prompt: str) -> str:
        earlier = []
        for message in agent.messages[:-1][-HISTORY:]:
            text = text_of(message.get("content")).strip()
            if text:
                earlier.append(f"{message['role']}: {text}")
        parts = [f"<context>\n{agent.system}\n</context>"]
        if earlier:
            parts.append("<conversation so far>\n" + "\n\n".join(earlier) + "\n</conversation so far>")
        parts.append(prompt)
        parts.append(SUBMIT)
        return "\n\n".join(parts)

    def environment(self, agent: "Agent") -> dict[str, str]:
        env = super().environment(agent)
        config_dir = self._dir(agent) / "config"
        config_dir.mkdir(exist_ok=True)
        env["MSWEA_GLOBAL_CONFIG_DIR"] = str(config_dir)  # not the user's own .env
        env["MSWEA_CONFIGURED"] = "true"
        if agent.config.provider == "bedrock":
            env.setdefault("AWS_REGION_NAME", env.get("AWS_REGION", "ap-northeast-1"))
        return env

    def read_line(self, line: str, turn: Turn, emit: Callable[[str, str], None]) -> str | None:
        return None  # the console output is for people; the trajectory is for us

    def collect(self, agent: "Agent", turn: Turn) -> str | None:
        data: dict[str, Any] = json.loads(self._trajectory(agent).read_text("utf-8"))
        info = data.get("info") or {}
        if info.get("exit_status") in ("LimitsExceeded", "TimeExceeded"):
            turn.stopped_by = "turn_limit"
        turn.iterations = int((info.get("model_stats") or {}).get("api_calls", 0) or 0)
        answer = ""
        for message in data.get("messages") or []:
            if message.get("role") == "tool":
                turn.tool_calls += 1
            elif message.get("role") == "assistant":
                for call in message.get("tool_calls") or []:
                    command = json.loads((call.get("function") or {}).get("arguments") or "{}")
                    emit_command = command.get("command", "")
                    if "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT" not in emit_command:
                        turn.events.append(("tool", f"bash {emit_command}"))
                answer = text_of(message.get("content")) or answer
        return (info.get("submission") or "").strip() or answer.strip() or None
