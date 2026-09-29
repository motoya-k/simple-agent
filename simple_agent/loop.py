"""The agent loop.

Call the model.  If it asked for tools, run them, append the results, and go
around again.  If it did not, that response is the answer.  Everything else in
this repo is scaffolding around these twenty lines.

The guards exist because an agent that cannot stop is not an agent:

* ``max_turn_iterations`` — how far one request may go before giving an answer.
* ``Budget.max_iterations`` / ``Budget.token_budget`` — session-wide ceilings,
  checked against real usage numbers.
* ``interrupt`` — a callable the host sets when the user says stop.

The two iteration limits are separate on purpose.  A per-turn cap is what
catches a model stuck in a loop *now*; a session cap is what catches a
conversation that has been quietly expensive all afternoon.  Collapsing them
into one number means either the first turn can run away, or a long healthy
conversation dies of old age.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable

from .providers.base import Provider, ToolCall
from .tools import ToolRegistry


@dataclass
class Budget:
    """Session-wide spend, carried across every turn of one conversation."""

    max_iterations: int = 600
    token_budget: int = 2_000_000
    iterations: int = 0
    tokens: int = 0
    #: Input tokens on the most recent call — i.e. how big the context is now.
    #: Cumulative totals cannot answer that; this is what compaction reads.
    last_prompt_tokens: int = 0

    @property
    def exhausted(self) -> bool:
        return self.iterations >= self.max_iterations or self.tokens >= self.token_budget

    def charge(self, input_tokens: int, output_tokens: int) -> None:
        self.tokens += input_tokens + output_tokens
        self.last_prompt_tokens = input_tokens


@dataclass
class Turn:
    """What one call to :func:`run_conversation` produced."""

    text: str = ""
    stopped_by: str = "answer"  # answer | budget | turn_limit | interrupt
    tool_calls: int = 0
    tokens: int = 0
    iterations: int = 0
    events: list[tuple[str, str]] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return self.stopped_by == "answer"


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
    max_turn_iterations: int = 30,
    on_event: Callable[[str, str], None] | None = None,
    interrupt: Callable[[], bool] | None = None,
) -> Turn:
    budget = budget or Budget()
    turn = Turn()
    schemas = registry.schemas()

    def emit(kind: str, text: str) -> None:
        turn.events.append((kind, text))
        if on_event:
            on_event(kind, text)

    def stopping() -> bool:
        return bool(interrupt and interrupt())

    while True:
        if stopping():
            return _halt(turn, messages, "interrupt", "Stopped at your request.")
        if budget.exhausted:
            return _halt(
                turn,
                messages,
                "budget",
                f"Stopped after {budget.iterations} steps in this conversation "
                "(session iteration or token budget exhausted).",
            )
        if turn.iterations >= max_turn_iterations:
            return _halt(
                turn,
                messages,
                "turn_limit",
                f"Stopped after {turn.iterations} steps on this request without "
                "reaching an answer.",
            )

        response = provider.complete(
            system=system,
            messages=messages,
            tools=schemas,
            max_tokens=max_tokens,
            model=model,
        )
        budget.iterations += 1
        budget.charge(response.input_tokens, response.output_tokens)
        turn.iterations += 1
        turn.tokens += response.input_tokens + response.output_tokens

        messages.append(provider.assistant_message(response))

        if not response.wants_tools:
            turn.text = response.text
            return turn

        if response.text.strip():
            emit("thinking", response.text.strip())

        results, interrupted = _run_tools(
            registry, response.tool_calls, max_parallel_tools, emit, stopping
        )
        turn.tool_calls += sum(1 for r in results if not r.get("_cancelled"))
        messages.append({"role": "user", "content": [_clean(r) for r in results]})

        if interrupted:
            return _halt(turn, messages, "interrupt", "Stopped at your request.")


def _halt(turn: Turn, messages: list[dict[str, Any]], reason: str, text: str) -> Turn:
    """End the turn, and say so *in the transcript*.

    The notice is appended as a real assistant turn rather than returned only
    to the host. A conversation that stopped is a fact the next turn needs:
    without it, the model resumes as though its last tool calls had simply
    succeeded, and the stored transcript no longer matches what was said.
    """
    turn.stopped_by = reason
    turn.text = text
    messages.append({"role": "assistant", "content": text})
    return turn


def _clean(block: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in block.items() if not k.startswith("_")}


def _run_tools(
    registry: ToolRegistry,
    calls: list[ToolCall],
    max_parallel: int,
    emit: Callable[[str, str], None],
    stopping: Callable[[], bool],
) -> tuple[list[dict[str, Any]], bool]:
    """Run the requested tools, fanning out only where it is safe to.

    Read-only tools go wide; anything that mutates state runs alone.  But the
    fan-out happens *within* the order the model asked for, never across it:
    consecutive read-only calls form one batch, and a write ends the batch.

    Running every read first and every write after would be faster and wrong.
    Asked to write a file and then read it, the read would run first and
    return the old contents — and because results are reassembled in the
    requested order, the model would never see that anything was out of turn.

    Returns the result blocks and whether the run was cut short.  A cancelled
    call still gets a result block: the provider requires an answer for every
    tool call in the preceding message, so an interrupt that simply stopped
    would leave a transcript that cannot be resumed.
    """
    results: list[dict[str, Any] | None] = [None] * len(calls)
    interrupted = False

    def execute(index: int) -> None:
        call = calls[index]
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

    index = 0
    while index < len(calls):
        if stopping():
            interrupted = True
            break

        if not _is_parallel_safe(registry, calls[index]):
            execute(index)
            index += 1
            continue

        end = index
        while end < len(calls) and _is_parallel_safe(registry, calls[end]):
            end += 1
        batch = list(range(index, end))

        if len(batch) > 1:
            with ThreadPoolExecutor(max_workers=min(max_parallel, len(batch))) as pool:
                list(pool.map(execute, batch))
        else:
            execute(index)
        index = end

    for position, call in enumerate(calls):
        if results[position] is None:
            results[position] = {
                "type": "tool_result",
                "tool_use_id": call.id,
                "content": "[not run — the turn was interrupted]",
                "is_error": True,
                "_cancelled": True,
            }

    return [block for block in results if block is not None], interrupted


def _is_parallel_safe(registry: ToolRegistry, call: ToolCall) -> bool:
    tool = registry.get(call.name)
    return bool(tool and tool.parallel_safe)


def _preview(arguments: dict[str, Any], limit: int = 90) -> str:
    parts = []
    for key, value in arguments.items():
        text = " ".join(str(value).split())
        parts.append(f"{key}={text[:limit]}{'…' if len(text) > limit else ''}")
    return " ".join(parts)
