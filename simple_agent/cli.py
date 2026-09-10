"""A terminal REPL — one of several possible hosts for the same Agent core."""

from __future__ import annotations

import sys

from .agent import Agent
from .config import Config, load_dotenv

DIM = "\033[2m"
BOLD = "\033[1m"
CYAN = "\033[36m"
RESET = "\033[0m"

HELP = """Commands:
  /memory            show the frozen memory snapshot
  /skills            list skills with status and use count
  /search <query>    full-text search across all past sessions
  /sessions          list recent sessions
  /cost              tokens used this session
  /help              this list
  /exit              quit (Ctrl-D also works)
Anything else is sent to the agent."""


def _print_event(kind: str, text: str) -> None:
    if kind == "tool":
        print(f"{DIM}  · {text}{RESET}", flush=True)
    elif kind == "thinking":
        print(f"{DIM}  {text}{RESET}", flush=True)


def _handle_command(agent: Agent, line: str) -> bool:
    """Returns True if the input was a command. Raises SystemExit on /exit."""
    if not line.startswith("/"):
        return False
    command, _, argument = line[1:].partition(" ")

    if command in {"exit", "quit"}:
        raise SystemExit(0)
    if command == "help":
        print(HELP)
    elif command == "memory":
        print(agent.memory.snapshot())
    elif command == "skills":
        print(agent.registry.call("skill_manage", {"action": "list"})[0])
    elif command == "search":
        if not argument.strip():
            print("usage: /search <query>")
        else:
            print(agent.registry.call("session_search", {"query": argument.strip()})[0])
    elif command == "sessions":
        for row in agent.store.recent_sessions():
            print(f"  {row['id']}  {row['title'] or '(untitled)'}")
    elif command == "cost":
        budget = agent.budget
        print(
            f"  {budget.tokens:,} tokens over {budget.iterations} model calls "
            f"(ceiling {budget.max_iterations} calls / {budget.token_budget:,} tokens)"
        )
    else:
        print(f"Unknown command: /{command}. Try /help")
    return True


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    argv = list(sys.argv[1:] if argv is None else argv)

    try:
        agent = Agent(Config.load())
    except RuntimeError as exc:
        print(f"{exc}", file=sys.stderr)
        return 1

    # One-shot mode: simple-agent "summarize the repo"
    if argv:
        turn = agent.run(" ".join(argv), on_event=_print_event)
        print(turn.text)
        return 0

    try:
        import readline  # noqa: F401  (enables line editing and history)
    except ImportError:
        pass

    print(f"{BOLD}simple-agent{RESET} {DIM}· {agent.config.model} · session {agent.session_id}{RESET}")
    print(f"{DIM}/help for commands, /exit to quit{RESET}\n")

    while True:
        try:
            line = input(f"{CYAN}› {RESET}").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue
        try:
            if _handle_command(agent, line):
                continue
        except SystemExit:
            break

        try:
            turn = agent.run(line, on_event=_print_event)
        except KeyboardInterrupt:
            print(f"\n{DIM}[interrupted]{RESET}")
            continue
        except RuntimeError as exc:
            print(f"{DIM}[error] {exc}{RESET}")
            continue
        print(f"\n{turn.text}\n")

    return 0
