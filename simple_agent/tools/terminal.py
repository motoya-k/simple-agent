"""Terminal tool with snapshot-restored shell sessions.

Every call starts a brand-new ``bash``.  Before the command runs we restore the
exported environment, shell functions, and working directory captured at the end
of the previous call; afterwards we snapshot them again.  The model experiences
one continuous shell, but no long-lived process can wedge the agent.

Hermes puts this behind ``BaseEnvironment`` and ships local / docker / ssh /
singularity / modal / daytona backends that each implement only ``_run_bash``.
This repo implements the local one and keeps the seam visible below.
See DESIGN.md § Terminal backends.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

MAX_OUTPUT = 30_000


class LocalEnvironment:
    """The one backend implemented here. Other backends implement ``run`` only."""

    def __init__(self, state_dir: Path, timeout: int = 120) -> None:
        self.state_dir = state_dir
        self.timeout = timeout
        self.env_file = state_dir / "env.sh"
        self.fn_file = state_dir / "functions.sh"
        self.cwd_file = state_dir / "cwd"

    def _script(self, command: str) -> str:
        return f"""
[ -f {self.env_file} ] && . {self.env_file} 2>/dev/null
[ -f {self.fn_file} ]  && . {self.fn_file}  2>/dev/null
[ -f {self.cwd_file} ] && cd "$(cat {self.cwd_file})" 2>/dev/null

{command}
__exit=$?

declare -px > {self.env_file} 2>/dev/null
declare -f  > {self.fn_file}  2>/dev/null
pwd > {self.cwd_file}
exit $__exit
"""

    def run(self, command: str, timeout: int | None = None) -> str:
        try:
            proc = subprocess.run(
                ["bash", "-lc", self._script(command)],
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
    env = LocalEnvironment(config.shell_state_dir, config.command_timeout)

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
                    "description": f"Seconds before the command is killed (default {config.command_timeout}).",
                },
            },
            "required": ["command"],
        },
    )
    def terminal(command: str, timeout: int | None = None) -> str:
        return env.run(command, timeout)
