#!/usr/bin/env python3
"""The wiring: the repo's own transports, with Claude Code in the middle.

    python examples/claude-code-host/host.py

``simple-agent --slack --email --cron`` is this process with this repo's loop
answering the messages.  This file is that same composition with exactly one
thing replaced — the engine — which is the whole point of the example: Slack,
IMAP, the schedules, the routers, the profiles and the transcripts are the
library's, and ``claude -p`` only has to answer.

* **In** — Slack (Socket Mode), one mailbox over IMAP, and any job in
  ``~/.simple-agent/schedules/``. Whichever of the three is configured.
* **Middle** — :class:`~claude_code.ClaudeCodeAgent`, under the route's profile,
  with the MCP servers rendered from [mcp.json](mcp.json).
* **Out** — Slack. Mail answers in Slack too; see ``slack_ops_channel`` below.
* **Kept** — Postgres: the transcripts, the skills, which mail has been handled,
  which firings have run. Long-term memory is Hindsight's, over MCP.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))  # a directory of scripts, not an installed package

from claude_code import ClaudeCodeAgent, allowed_tools, write_mcp_config  # noqa: E402
from simple_agent.config import Config, load_dotenv  # noqa: E402
from simple_agent.cron import CronRouter, CronSource, load_jobs, open_cursor  # noqa: E402
from simple_agent.host import AllowlistRouter, FirstMatch, Host  # noqa: E402
from simple_agent.logs import configure  # noqa: E402
from simple_agent.mail import ImapSource, open_ledger  # noqa: E402
from simple_agent.profile import Profile, load_profile  # noqa: E402
from simple_agent.seams import Destination  # noqa: E402
from simple_agent.slack import SlackRouter, SlackSink, SlackSource, open_inbox  # noqa: E402
from simple_agent.state import open_store  # noqa: E402

log = logging.getLogger("claude-code-host")


def build_host(config: Config) -> Host:
    store = open_store(config)  # one store for every conversation; see registry.py
    template = Path(os.environ.get("SIMPLE_AGENT_MCP_TEMPLATE", HERE / "mcp.json"))
    rendered: dict[str, tuple[Path, tuple[str, ...]]] = {}

    def mcp_for(namespace: str) -> tuple[Path, tuple[str, ...]]:
        """One rendered config per namespace — the Hindsight bank is in the URL."""
        if namespace not in rendered:
            rendered[namespace] = write_mcp_config(
                template,
                config.home / "claude" / f"mcp-{namespace}.json",
                namespace=namespace,
                # Where this repo keeps things is the config's answer, not the
                # environment's — it can come from config.yaml. The server
                # started by that entry has to open the *same* store as this
                # host, or the agent searches sessions nobody is writing to.
                env={
                    **os.environ,
                    "SIMPLE_AGENT_HOME": str(config.home),
                    "SIMPLE_AGENT_DATABASE_URL": config.database_url,
                },
            )
        return rendered[namespace]

    def agent_factory(cfg, *, session_key, source, profile):
        mcp_config, servers = mcp_for(profile.namespace or cfg.memory_namespace)
        return ClaudeCodeAgent(
            cfg,
            session_key=session_key,
            source=source,
            profile=profile,
            mcp_config=mcp_config,
            servers=servers,
            store=store,
        )

    sources, sinks, routers = [], [], []
    profiles: list[Profile] = []

    # -- Slack ----------------------------------------------------------
    app_token = os.environ.get("SIMPLE_AGENT_SLACK_APP_TOKEN", "")
    bot_token = os.environ.get("SIMPLE_AGENT_SLACK_BOT_TOKEN", "")
    if app_token and bot_token:
        slack = load_profile(config, "slack")
        allow = slack.setting_list("slack_allow")
        if not allow:
            raise SystemExit("slack needs slack_allow (e.g. `#ops, dm`) in profiles/slack.md")
        sources.append(
            SlackSource(app_token=app_token, bot_token=bot_token, inbox=open_inbox(config))
        )
        sinks.append(SlackSink(bot_token=bot_token))
        routers.append(SlackRouter(allow=allow, profile=slack))
        profiles.append(slack)

    # -- mail -----------------------------------------------------------
    email = load_profile(config, "email")
    imap_host = email.setting("imap_host")
    imap_password = os.environ.get("SIMPLE_AGENT_IMAP_PASSWORD", "")
    if imap_host and imap_password:
        # Mail answers in Slack, and that is a decision rather than a missing
        # piece: a sender address is trivially forged, so a host that replies to
        # everything it receives is a way to have the team's agent write to
        # strangers in the team's name. What the mail should *cause*, the agent
        # does with tools; the answer lands where a person will see it.
        ops = email.setting("slack_ops_channel")
        if not ops:
            log.warning("mail has no slack_ops_channel: it will answer nobody")
        sources.append(
            ImapSource(
                host=imap_host,
                user=email.setting("imap_user"),
                password=imap_password,
                mailbox=email.setting("imap_mailbox", "INBOX"),
                ledger=open_ledger(config),
            )
        )
        routers.append(
            AllowlistRouter(
                allow=email.setting_list("email_allow"),
                profile=email,
                to=(Destination("slack", ops),) if ops else (),
            )
        )
        profiles.append(email)

    # -- schedules ------------------------------------------------------
    # Anything in ~/.simple-agent/schedules/ runs here too, on the same agent
    # registry and the same concurrency limits — examples/pr-watch is a job
    # file, and this is one host that can run it.
    jobs = load_jobs(config)
    if jobs:
        sources.append(CronSource(jobs, cursor=open_cursor(config)))
        routers.append(CronRouter(jobs))
        profiles += [job.profile or load_profile(config) for job in jobs]

    if not sources:
        raise SystemExit(
            "nothing to listen to: set the Slack tokens, or imap_host and "
            "SIMPLE_AGENT_IMAP_PASSWORD, or put a job in ~/.simple-agent/schedules/"
        )

    # Every profile in play is translated now, so an unreadable mcp.json or a
    # profile that allows a tool its `learning: false` forbids stops the deploy
    # instead of failing on the first message that arrives.
    for profile in profiles:
        _, servers = mcp_for(profile.namespace or config.memory_namespace)
        tools = allowed_tools(profile, servers)
        log.info(
            "profile %s: learning=%s, namespace=%s, tools: %s",
            profile.name,
            profile.learning,
            profile.namespace or config.memory_namespace,
            ", ".join(tools) or "none",
        )

    return Host(
        config,
        sources=sources,
        router=FirstMatch(tuple(routers)),
        sinks=sinks,
        agent_factory=agent_factory,
    )


def main() -> int:
    import asyncio

    load_dotenv()
    configure()
    config = Config.load()
    if not config.database_url:
        log.warning(
            "no SIMPLE_AGENT_DATABASE_URL: conversations are going to SQLite under %s, "
            "which a container does not keep",
            config.home,
        )
    host = build_host(config)
    try:
        asyncio.run(host.serve())
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
