"""A terminal REPL — one of several possible hosts for the same Agent core."""

from __future__ import annotations

import os
import sys

from .agent import Agent
from .config import Config, load_dotenv
from .profile import load_profile
from .session import SessionSource

DIM = "\033[2m"
BOLD = "\033[1m"
CYAN = "\033[36m"
RESET = "\033[0m"

HELP = """Commands:
  /memory <query>    search long-term (team) memory
  /skills            list skills with status and use count
  /search <query>    full-text search across all past sessions
  /sessions          list recent sessions
  /cost              tokens used this session
  /new               start a fresh conversation in this directory
  /help              this list
  /exit              quit (Ctrl-D also works)
Anything else is sent to the agent."""


def _print_event(kind: str, text: str) -> None:
    if kind == "tool":
        print(f"{DIM}  · {text}{RESET}", flush=True)
    elif kind == "thinking":
        print(f"{DIM}  {text}{RESET}", flush=True)


def _handle_command(agent: Agent, line: str) -> Agent | bool:
    """Handle a slash command.

    Returns ``False`` when the input was not a command, ``True`` when it was
    handled, or a replacement Agent when the command started a new session.
    Raises ``SystemExit`` on /exit.
    """
    if not line.startswith("/"):
        return False
    command, _, argument = line[1:].partition(" ")

    if command in {"exit", "quit"}:
        raise SystemExit(0)
    if command == "help":
        print(HELP)
    elif command == "memory":
        if not argument.strip():
            print(f"usage: /memory <query>   (namespace: {agent.memory.namespace})")
        else:
            print(agent.registry.call("memory_search", {"query": argument.strip()})[0])
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
        print(f"  {len(agent.messages)} messages in context")
    elif command == "new":
        fresh = Agent(
            agent.config, cwd=agent.cwd, source=agent.source, resume=False,
            profile=agent.profile,
        )
        print(f"{DIM}started session {fresh.session_id}{RESET}")
        return fresh
    else:
        print(f"Unknown command: /{command}. Try /help")
    return True


def health(config: Config) -> int:
    """Exit 0 while a host's heartbeat is fresh — for a container health check."""
    import time

    from .host import HEARTBEAT_SECONDS

    try:
        age = time.time() - float((config.home / "heartbeat").read_text())
    except (OSError, ValueError):
        print("unhealthy: no heartbeat", file=sys.stderr)
        return 1
    if age > 3 * HEARTBEAT_SECONDS:
        print(f"unhealthy: last heartbeat {age:.0f}s ago", file=sys.stderr)
        return 1
    print(f"healthy: last heartbeat {age:.0f}s ago")
    return 0


def serve_email(config: Config, profile_name: str = "") -> int:
    """Poll a mailbox and run one agent per sender and thread. Answers nobody.

    The mailbox, the allowlist, the toolset and the instructions all come from
    the profile — ``email`` unless another is named. The password does not: a
    secret is read from the environment only, so a profile file stays safe to
    commit.
    """
    import asyncio

    from .host import AllowlistRouter, Host
    from .mail import ImapSource, open_ledger

    profile = load_profile(config, profile_name or "email")
    password = os.environ.get("SIMPLE_AGENT_IMAP_PASSWORD", "")
    host_name = profile.setting("imap_host")
    user = profile.setting("imap_user")
    mailbox = profile.setting("imap_mailbox", "INBOX")
    allow = profile.setting_list("email_allow")
    missing = [
        name
        for name, value in (
            ("imap_host", host_name),
            ("imap_user", user),
            ("SIMPLE_AGENT_IMAP_PASSWORD", password),
            ("email_allow", allow),
        )
        if not value
    ]
    if missing:
        print(
            f"--email needs: {', '.join(missing)} "
            f"(in {config.profiles_dir / f'{profile.name}.md'} or the environment)",
            file=sys.stderr,
        )
        return 1

    from .logs import configure

    configure()
    source = ImapSource(
        host=host_name,
        user=user,
        password=password,
        mailbox=mailbox,
        ledger=open_ledger(config),
    )
    host = Host(config, sources=[source], router=AllowlistRouter(allow=allow, profile=profile))
    tools = "*" if profile.tools is None else ", ".join(profile.tools) or "none"
    print(
        f"{DIM}watching {user} {mailbox} · profile {profile.name} "
        f"· tools: {tools}{RESET}"
    )
    try:
        asyncio.run(host.serve())
    except KeyboardInterrupt:
        pass
    return 0


def _take_profile(argv: list[str]) -> str:
    """Pull ``--profile NAME`` out of the arguments, wherever it appears."""
    if "--profile" not in argv:
        return ""
    index = argv.index("--profile")
    name = argv[index + 1] if len(argv) > index + 1 else ""
    del argv[index : index + 2]
    return name


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    argv = list(sys.argv[1:] if argv is None else argv)

    config = Config.load()
    if argv[:1] == ["--mcp"]:
        from . import mcp

        return mcp.main(argv[1:], config)
    profile_name = _take_profile(argv)
    if argv[:1] == ["--email"]:
        return serve_email(config, profile_name)
    if argv[:1] == ["--health"]:
        return health(config)
    try:
        # One conversation per working directory, resumed on the next launch.
        agent = Agent(
            config,
            source=SessionSource.local(os.getcwd()),
            profile=load_profile(config, profile_name),
        )
    except (RuntimeError, ValueError) as exc:
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

    resumed = f" · resumed {len(agent.messages)} messages" if agent.messages else ""
    print(
        f"{BOLD}simple-agent{RESET} {DIM}· {agent.config.model} "
        f"· profile {agent.profile.name} · session {agent.session_id}{resumed}{RESET}"
    )
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
            handled = _handle_command(agent, line)
        except SystemExit:
            break
        if handled is not False:
            if isinstance(handled, Agent):
                agent = handled
            continue

        try:
            turn = agent.run(line, on_event=_print_event)
        except KeyboardInterrupt:
            # The turn's own `finally` has already written everything that ran.
            # Whatever it left dangling is repaired at the start of the next
            # turn, so the session stays usable instead of ending here.
            agent.interrupt()
            print(f"\n{DIM}[interrupted]{RESET}")
            continue
        except RuntimeError as exc:
            print(f"{DIM}[error] {exc}{RESET}")
            continue
        print(f"\n{turn.text}\n")

    return 0
