"""Engine seam — who runs the turn.

Providers made the *model* swappable; this makes the *harness* swappable.  An
engine takes an agent whose transcript already ends with the user's message,
does whatever it takes to answer, appends what it produced to that transcript,
and returns a :class:`~simple_agent.loop.Turn`.

The built-in engine is the loop in :mod:`simple_agent.loop`.  Another engine can
hand the whole turn to an external harness (see :mod:`.pi`) and keep only the
answer.  Everything around the turn stays with the agent either way: session
identity, the transcript on disk, memory, the background review, and the
Source / Router / Sink that decided the message should run at all.

Adding an engine is one file plus one line in ``_REGISTRY`` — the same rule as
providers.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, Callable

from ..loop import Turn, run_conversation

if TYPE_CHECKING:
    from ..agent import Agent

EventCallback = Callable[[str, str], None] | None


class Engine(ABC):
    name: str = "unnamed"

    def supports(self, provider: str) -> bool:
        """Whether this engine can run with the configured provider.

        Checked when the agent is built, so an impossible combination fails at
        start rather than on the first message.
        """
        return True

    @abstractmethod
    def run(self, agent: "Agent", *, on_event: EventCallback, interrupt: Callable[[], bool]) -> Turn:
        """Answer the last user message in ``agent.messages``, appending to it."""


class LoopEngine(Engine):
    """The loop in this repo: our provider, our tools, our budget."""

    name = "loop"

    def run(self, agent: "Agent", *, on_event: EventCallback, interrupt: Callable[[], bool]) -> Turn:
        return run_conversation(
            provider=agent.provider,
            model=agent.config.model,
            system=agent.system,
            messages=agent.messages,
            registry=agent.registry,
            budget=agent.budget,
            on_event=on_event,
            interrupt=interrupt,
        )


_REGISTRY: dict[str, str] = {
    "loop": "simple_agent.engines:LoopEngine",
    "pi": "simple_agent.engines.pi:PiEngine",
    "claude-code": "simple_agent.engines.claude_code:ClaudeCodeEngine",
    "goose": "simple_agent.engines.goose:GooseEngine",
    "opencode": "simple_agent.engines.opencode:OpenCodeEngine",
    "hermes": "simple_agent.engines.hermes:HermesEngine",
    "mini-swe": "simple_agent.engines.mini_swe:MiniSweEngine",
}


def get_engine(name: str, **kwargs: Any) -> Engine:
    import importlib

    try:
        target = _REGISTRY[name]
    except KeyError:
        known = ", ".join(sorted(_REGISTRY))
        raise RuntimeError(f"Unknown engine {name!r}. Known engines: {known}") from None
    module_path, class_name = target.split(":")
    return getattr(importlib.import_module(module_path), class_name)(**kwargs)


__all__ = ["Engine", "LoopEngine", "get_engine"]
