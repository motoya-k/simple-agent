"""Terminal tool with snapshot-restored shell sessions.

Every call starts a brand-new ``bash``.  Before the command runs we restore the
exported environment, shell functions, and working directory captured at the end
of the previous call; afterwards we snapshot them again.  The model experiences
one continuous shell, but no long-lived process can wedge the agent.

**The snapshot belongs to one conversation.**  Kept in a single shared
directory, every conversation in the process would inherit whatever the last
one exported and wherever it had ``cd``-ed — one person's working directory
silently becoming another person's.  So the snapshot path is derived from the
session key at call time (see :mod:`simple_agent.context`), which also means a
resumed thread walks back into the shell it left.

Hermes puts this behind ``BaseEnvironment`` and ships local / docker / ssh /
singularity / modal / daytona backends that each implement only ``_run_bash``.
This repo implements the local one and keeps the seam visible below.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from ..context import current_session_key
from ..session import session_slug

MAX_OUTPUT = 30_000
DEFAULT_LANE = "default"


class LocalEnvironment:
    """The one backend implemented here. Other backends implement ``run`` only."""

    def __init__(self, state_dir: Path, timeout: int = 120) -> None:
        self.state_dir = state_dir
        self.timeout = timeout

    def lane(self, session_key: str = "") -> Path:
        """The directory holding one conversation's shell snapshot."""
        key = session_key or current_session_key()
        path = self.state_dir / (session_slug(key) if key else DEFAULT_LANE)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _script(self, command: str, lane: Path) -> str:
        env_file = lane / "env.sh"
        fn_file = lane / "functions.sh"
        cwd_file = lane / "cwd"
        return f"""
[ -f {env_file} ] && . {env_file} 2>/dev/null
[ -f {fn_file} ]  && . {fn_file}  2>/dev/null
[ -f {cwd_file} ] && cd "$(cat {cwd_file})" 2>/dev/null

{command}
__exit=$?

declare -px > {env_file} 2>/dev/null
declare -f  > {fn_file}  2>/dev/null
pwd > {cwd_file}
exit $__exit
"""

    def run(self, command: str, timeout: int | None = None) -> str:
        try:
            proc = subprocess.run(
                ["bash", "-lc", self._script(command, self.lane())],
                capture_output=True,
                text=True,
                timeout=timeout or self.timeout,
            )
        except subprocess.TimeoutExpired:
            return f"[timed out after {timeout or self.timeout}s]"

        parts: list[str] = []
        if proc.stdout:
            parts.append(proc.stdout.rstrip())
        if proc.stderr:
            parts.append(f"[stderr]\n{proc.stderr.rstrip()}")
        if proc.returncode != 0:
            parts.append(f"[exit {proc.returncode}]")

        output = "\n".join(parts) or "[no output]"
        if len(output) > MAX_OUTPUT:
            half = MAX_OUTPUT // 2
            omitted = len(output) - MAX_OUTPUT
            output = f"{output[:half]}\n\n... [{omitted} chars omitted] ...\n\n{output[-half:]}"
        return output


def register(registry, config) -> None:
    env = LocalEnvironment(config.shell_state_dir)

    @registry.tool(
        name="terminal",
        description=(
            "Run a bash command. The working directory, exported environment, and "
            "shell functions persist across calls, so this behaves like one long-lived "
            "shell session."
        ),
        parameters={
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "The bash command to run."},
                "timeout": {
                    "type": "integer",
                    "description": f"Seconds before the command is killed (default {env.timeout}).",
                },
            },
            "required": ["command"],
        },
    )
    def terminal(command: str, timeout: int | None = None) -> str:
        return env.run(command, timeout)
