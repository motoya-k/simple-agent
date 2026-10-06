"""Mods — small Python files that stand between the model and a tool.

Everything else in this repo is a seam you *implement*: a provider translates,
a Source receives, a tool acts.  A mod is the one thing none of them can be —
a decision made about a tool call that neither the model nor the tool should
be trusted to make.  "Never ``rm -rf`` on this route."  "Strip the API key out
of whatever the shell prints."  "Rewrite a path so it cannot leave the project."

A mod is a file at ``~/.simple-agent/mods/<name>.py`` with either hook, or
both::

    SECRET = os.environ["SOME_TOKEN"]

    def before_tool(name, arguments):
        if name == "terminal" and "rm -rf" in arguments.get("command", ""):
            return Deny("rm -rf is not allowed here")
        return None                      # no opinion

    def after_tool(name, arguments, output, is_error):
        return output.replace(SECRET, "[redacted]")

``before_tool`` may return ``None`` (no opinion), a :class:`Deny` (the call
does not run and the model is told why), or a replacement ``arguments`` dict.
``Deny`` is already in the file's scope; importing it works too.
``after_tool`` returns the output the model will see, or ``None`` to leave it
alone.  Mods run in the order they are named, each one seeing what the last
one decided.

A profile names which mods it runs, and ``Config.mods`` names the ones that
run on every route, so a deployment-wide rule cannot be dropped by a profile:

    mods: no-rm, redact-secrets

**They fail in opposite directions, on purpose.**  A ``before_tool`` that
raises *denies* the call: a policy hook that crashes and waves the call
through is the one outcome worth avoiding, and the same goes for a mod a
profile names but that is not there — :func:`load_mods` raises rather than
starting without it.  An ``after_tool`` that raises is ignored and the
original output stands: it only ever shapes what the model reads, and losing
a redaction pass should not cost the turn.

Two things to know before writing one. A mod is Python in this process, not an
MCP server in another: it is trusted code with the agent's own powers, so a
mod is something you wrote or read, never something you installed. And
read-only tools fan out across threads (see :func:`simple_agent.loop._run_tools`),
so a hook must be safe to call from several at once — keep it pure, or lock
what it touches.
"""

from __future__ import annotations

import importlib.util
import logging
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

log = logging.getLogger(__name__)

_LOAD_LOCK = threading.Lock()  # importing twice under one name races


@dataclass(frozen=True)
class Deny:
    """Returned by ``before_tool`` to stop a call. The reason reaches the model."""

    reason: str = ""


class Mods:
    """The loaded mods for one route, asked in order at every tool call."""

    def __init__(self, modules: Iterable[Any] = ()) -> None:
        self.modules = list(modules)

    def __bool__(self) -> bool:
        return bool(self.modules)

    @property
    def names(self) -> list[str]:
        return [getattr(m, "__simple_agent_mod__", m.__name__) for m in self.modules]

    def before_tool(self, name: str, arguments: dict[str, Any]) -> tuple[dict[str, Any], str]:
        """``(arguments, denial)``. A non-empty denial means the call must not run."""
        for module in self.modules:
            hook = getattr(module, "before_tool", None)
            if hook is None:
                continue
            mod_name = getattr(module, "__simple_agent_mod__", module.__name__)
            try:
                decision = hook(name, dict(arguments))
            except Exception as exc:
                # Fail closed: a policy that crashed has not approved anything.
                log.exception("mod %s before_tool failed", mod_name)
                return arguments, f"Blocked: mod {mod_name!r} failed ({type(exc).__name__})."
            if isinstance(decision, Deny):
                return arguments, f"Blocked by mod {mod_name!r}: {decision.reason or 'not allowed'}"
            if decision is False:
                return arguments, f"Blocked by mod {mod_name!r}."
            if isinstance(decision, dict):
                arguments = decision
        return arguments, ""

    def after_tool(
        self, name: str, arguments: dict[str, Any], output: str, is_error: bool
    ) -> str:
        """The output the model will see. A hook that fails leaves it untouched."""
        for module in self.modules:
            hook = getattr(module, "after_tool", None)
            if hook is None:
                continue
            try:
                replacement = hook(name, dict(arguments), output, is_error)
            except Exception:
                # Fail open: this only shapes what the model reads.
                log.exception(
                    "mod %s after_tool failed", getattr(module, "__simple_agent_mod__", "?")
                )
                continue
            if isinstance(replacement, str):
                output = replacement
        return output


def load_mods(config: Any, names: Iterable[str] = ()) -> Mods:
    """Load ``config.mods`` and then ``names``, in order. Missing ones raise.

    The deployment's own mods come first, so a profile cannot get in front of
    a rule the deployment set.
    """
    wanted: list[str] = []
    for name in (*_split(getattr(config, "mods", "")), *names):
        name = name.strip()
        if name and name not in wanted:
            wanted.append(name)
    if not wanted:
        return Mods()
    directory = Path(getattr(config, "mods_dir", Path(".")))
    return Mods([_load(directory, name) for name in wanted])


def _load(directory: Path, name: str) -> Any:
    path = directory / f"{name}.py"
    if not path.exists():
        raise ValueError(f"No such mod: {name!r} (expected {path})")
    spec = importlib.util.spec_from_file_location(f"simple_agent_mod_{name}", path)
    if spec is None or spec.loader is None:
        raise ValueError(f"Mod {name!r} is not importable: {path}")
    module = importlib.util.module_from_spec(spec)
    module.__simple_agent_mod__ = name  # type: ignore[attr-defined]
    module.Deny = Deny  # type: ignore[attr-defined]  # in scope without an import
    with _LOAD_LOCK:
        sys.modules[spec.name] = module
        try:
            spec.loader.exec_module(module)
        except Exception as exc:
            sys.modules.pop(spec.name, None)
            raise ValueError(f"Mod {name!r} failed to load: {type(exc).__name__}: {exc}") from exc
    return module


def _split(value: str) -> list[str]:
    return [part.strip() for part in str(value).split(",") if part.strip()]


__all__ = ["Deny", "Mods", "load_mods"]
