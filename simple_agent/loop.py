"""The agent loop.

Call the model.  If it asked for tools, run them, append the results, and go
around again.  If it did not, that response is the answer.  Everything else in
this repo is scaffolding around these twenty lines.

The guards exist because an agent that cannot stop is not an agent:

* ``max_iterations`` — a hard ceiling on tool-using round trips.
* ``token_budget``   — a spend ceiling, checked against real usage numbers.
* ``interrupt``      — a callable the host sets when the user says stop.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable

from .providers.base import Provider, ToolCall
from .tools import ToolRegistry


@dataclass
class Budget:
    max_iterations: int = 90
    token_budget: int = 2_000_000
    iterations: int = 0
    tokens: int = 0

    @property
    def exhausted(self) -> bool:
        return self.iterations >= self.max_iterations or self.tokens >= self.token_budget

    def charge(self, input_tokens: int, output_tokens: int) -> None:
        self.tokens += input_tokens + output_tokens


@dataclass
class Turn:
    """What one call to :func:`run_conversation` produced."""

    text: str = ""
    stopped_by: str = "answer"  # answer | budget | interrupt
    tool_calls: int = 0
    tokens: int = 0
    events: list[tuple[str, str]] = field(default_factory=list)


def run_conversation(
    *,
    provider: Provider,
    model: str,
    system: str,
    messages: list[dict[str, Any]],
    registry: ToolRegistry,
    max_tokens: int = 8192,
    budget: Budget | None = None,
    max_parallel_tools: int = 8,
    on_event: Callable[[str, str], None] | None = None,
) -> Turn:
    budget = budget or Budget()
    turn = Turn()
    schemas = registry.schemas()

    def emit(kind: str, text: str) -> None:
        turn.events.append((kind, text))
        if on_event:
            on_event(kind, text)

    while not budget.exhausted:
        response = provider.complete(
            system=system,
            messages=messages,
            tools=schemas,
            max_tokens=max_tokens,
            model=model,
        )
        budget.iterations += 1
        budget.charge(response.input_tokens, response.output_tokens)
        turn.tokens += response.input_tokens + response.output_tokens

        messages.append(provider.assistant_message(response))

        if not response.wants_tools:
            turn.text = response.text
            return turn

        if response.text.strip():
            emit("thinking", response.text.strip())

        results = _run_tools(registry, response.tool_calls, max_parallel_tools, emit)
        turn.tool_calls += len(results)
        messages.append({"role": "user", "content": results})

    turn.stopped_by = "budget"
    turn.text = (
        f"Stopped after {budget.iterations} steps without reaching an answer "
        "(iteration or token budget exhausted)."
    )
    return turn


def _run_tools(
    registry: ToolRegistry,
    calls: list[ToolCall],
    max_parallel: int,
    emit: Callable[[str, str], None],
) -> list[dict[str, Any]]:
    """Read-only tools fan out; anything that mutates state runs in order.

    Order matters twice over: two writes to the same file must not race, and the
    model asked for them in a sequence it may have reasoned about.
    """
    results: list[dict[str, Any] | None] = [None] * len(calls)

    def execute(index: int, call: ToolCall) -> None:
        emit("tool", f"{call.name} {_preview(call.arguments)}")
        output, is_error = registry.call(call.name, call.arguments)
        block: dict[str, Any] = {
            "type": "tool_result",
            "tool_use_id": call.id,
            "content": output,
        }
        if is_error:
            block["is_error"] = True
        results[index] = block

    parallel = [(i, c) for i, c in enumerate(calls) if _is_parallel_safe(registry, c)]
    serial = [(i, c) for i, c in enumerate(calls) if not _is_parallel_safe(registry, c)]

    if len(parallel) > 1:
        with ThreadPoolExecutor(max_workers=min(max_parallel, len(parallel))) as pool:
            list(pool.map(lambda pair: execute(*pair), parallel))
    else:
        for index, call in parallel:
            execute(index, call)

    for index, call in serial:
        execute(index, call)

    return [block for block in results if block is not None]


def _is_parallel_safe(registry: ToolRegistry, call: ToolCall) -> bool:
    tool = registry.get(call.name)
    return bool(tool and tool.parallel_safe)


def _preview(arguments: dict[str, Any], limit: int = 90) -> str:
    parts = []
    for key, value in arguments.items():
        text = " ".join(str(value).split())
        parts.append(f"{key}={text[:limit]}{'…' if len(text) > limit else ''}")
    return " ".join(parts)
