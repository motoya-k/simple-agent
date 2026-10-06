# Claude Code as the engine

The loop is not always the interesting part. This example throws this repo's
loop away and keeps everything around it: Claude Code answers the messages, and
the Sources, the Sinks, the routers, the profiles, the session keys and the
transcript store are still simple-agent's.

`simple-agent --slack --email --cron` is this same process with this repo's own
loop in the middle. Everything below is that command with one thing replaced.

```
Slack  ──┐                                                   ┌─→ the same thread
mail   ──┤─→ Source ─→ Router ─→ claude -p ──────→ Sink ──────┤
clock  ──┘            profile      │                         └─→ #ops (mail, schedules)
                                   │
                         ┌─────────┴──────────┐
                         │ notion             │  tools
                         │ slack              │  tools
                         │ google workspace   │  tools
                         │ hindsight          │  long-term memory, one bank per namespace
                         │ simple-agent --mcp │  skills, session search
                         └────────────────────┘

Postgres: the transcripts · the skills · which mail has been handled
```

| File | What it owns |
| --- | --- |
| [`claude_code.py`](claude_code.py) | **The example.** `claude -p` as an agent the host can run, and the profile → allowlist translation. |
| [`host.py`](host.py) | The assembly: which transports, which profile per route, and that factory. Nothing else. |
| [`mcp.json`](mcp.json) | The MCP servers, as a template: `${VARS}` from the environment, `${namespace}` from the profile. |
| [`profiles/`](profiles) | Who the agent is on each route. Copy to `~/.simple-agent/profiles/`. |

Slack (Socket Mode), IMAP, the schedules and the routers come from the library —
[`simple_agent/slack`](../../simple_agent/slack),
[`simple_agent/mail`](../../simple_agent/mail),
[`simple_agent/cron`](../../simple_agent/cron) — not from here.

## Run it

```bash
pip install -e ".[postgres]"                      # from the repo root
cp examples/claude-code-host/profiles/*.md ~/.simple-agent/profiles/
$EDITOR ~/.simple-agent/profiles/slack.md         # slack_allow, namespace
$EDITOR ~/.simple-agent/profiles/email.md         # mailbox, allowlist, #ops channel

export SIMPLE_AGENT_DATABASE_URL=postgresql://...     # transcripts and skills
export ANTHROPIC_API_KEY=sk-ant-...                   # or CLAUDE_CODE_USE_BEDROCK=1
export SIMPLE_AGENT_SLACK_APP_TOKEN=xapp-...          # Socket Mode
export SIMPLE_AGENT_SLACK_BOT_TOKEN=xoxb-...
export SIMPLE_AGENT_IMAP_PASSWORD=...
export HINDSIGHT_URL=https://api.hindsight.vectorize.io HINDSIGHT_API_KEY=...
export NOTION_TOKEN=ntn_...
export GOOGLE_OAUTH_CLIENT_ID=... GOOGLE_OAUTH_CLIENT_SECRET=... USER_GOOGLE_EMAIL=...

python examples/claude-code-host/host.py
```

Slack app: Socket Mode enabled, an app-level token with `connections:write`,
and a bot token with `app_mentions:read`, `chat:write`, `reactions:write` and
the message scopes for what it listens to. Invite it to each channel in
`slack_allow`; it answers an @-mention, in a thread under the question.

Any job in `~/.simple-agent/schedules/` runs in the same process — so
[`../pr-watch`](../pr-watch)'s schedule, answered by Claude Code, is that file
copied in plus GitHub's server added to `mcp.json`. A profile that names a
server the config does not declare refuses to start, so you find that out now
rather than from a model saying it cannot see any pull requests.

Every `${VAR}` in `mcp.json` must be set — a missing one stops the start
rather than sending Notion the literal string `${NOTION_TOKEN}`. A server you
do not run is a server you delete from the template.

### Where the settings come from

Four places, and which one a value belongs in is not a matter of taste:

| | What goes there |
| --- | --- |
| the environment, or `.env` in the working directory | every secret, and anything a container overrides. [`.env.example`](.env.example) lists all of them; `cp examples/claude-code-host/.env.example .env` and run from the repo root. `load_dotenv` uses `setdefault`, so a real environment variable always wins over the file. |
| `~/.simple-agent/profiles/<name>.md` | who the agent is on a route, and that route's transport: channel ids, the mailbox, the allowlist, the namespace. Never a password — which is what makes a profile safe to commit. |
| `~/.simple-agent/config.yaml` | the deployment's own settings, in the same keys as `SIMPLE_AGENT_*` in lower case: `database_url`, `memory_namespace`, `max_concurrent_turns`. |
| [`mcp.json`](mcp.json) | the servers. `${VAR}` is substituted from the environment at startup, so this file holds no secret either. |

Every profile field also has an environment variable, so a container needs no
files at all: `SIMPLE_AGENT_SLACK_TOOLS`, `_LEARNING`, `_NAMESPACE` for the
fields, and `SIMPLE_AGENT_SLACK_CHANNELS`, `SIMPLE_AGENT_IMAP_HOST`,
`SIMPLE_AGENT_EMAIL_ALLOW`, `SIMPLE_AGENT_SLACK_OPS_CHANNEL` for the settings.

In production these arrive as the task definition's `secrets` (Secrets Manager)
and `environment`; `deploy/terraform` in the repo root already wires that up.
The MCP servers inherit this process's environment *and* get the `env` block
`mcp.json` gives them, which is why that file names what each server needs
instead of relying on what happens to be exported.

### Check the wiring

A server that fails to start is logged and skipped, by Claude Code as by this
repo, which looks exactly like a tool that does not exist. The first start
renders one config per namespace, so ask the engine what it ended up with:

```bash
claude -p --strict-mcp-config --mcp-config ~/.simple-agent/claude/mcp-ops.json \
  --tools "" --permission-prompts none "List your tools exactly, comma separated."
```

That is also how to find the exact tool names a server exposes, which is what
a profile's `tools:` needs to name them one at a time.

## Decisions worth knowing

**The conversation is in Postgres, not in the engine.** `claude -p` runs with
`--no-session-persistence` and gets the transcript handed to it, out of the
store, on every turn. The other option is `--resume <id>`, which is cheaper
and puts the conversation on one container's disk. A service that is replaced
mid-thread, or runs as more than one task, needs this one. See
`ClaudeCodeAgent._prompt`.

**The profile is still the boundary, and still both halves of it.** A profile's
`tools:` becomes `--allowedTools`, and with `--permission-prompts none`
anything outside it is denied rather than queued for a human who is not there.
Two translations to know: `notion__*` becomes the whole Notion server, writes
included, because Claude Code allowlists a server or an exact tool and nothing
in between; and `learning: false` refuses to start next to a tool that writes
long-term memory. That is the half people forget — a route anyone can write to
must not be able to leave a note that a trusted conversation reads back as the
team's own knowledge.

So the mail route here holds no Notion, Slack or Google tools at all. It can
recall from memory and read its own skills; anything that touches the world
goes through the Slack route, where the workspace decided who may speak.

**Mail does not answer mail.** There is no SMTP sink, on purpose: a sender
address is trivially forged, and a host that replies to everything it receives
is a way to have the team's agent write to strangers in the team's name. The
answer goes to the channel the team watches, where a person sees it. What the
mail should *cause*, the agent does with tools.

**One long-term memory, named once.** Hindsight keeps a bank per URL, so the
namespace that decides whose memory this is ends up in the URL, and two teams
on one deployment share no bank. `simple-agent --mcp` therefore serves skills
and session search only — not `memory_search`, not `memory_save`. Two memory
systems in one agent is not a fallback, it is two half-truths.

**No shell, no files.** `--tools ""` removes Claude Code's built-in toolset;
the agent acts through MCP servers only. There is nothing in this container
worth reading, and a chat message is not a reason to run a command.

**What this example does not use:** `simple_agent.loop`, `providers/`,
`tools/`, and the background reviewer. Learning here is the model calling
Hindsight's `retain` when something is settled, not a review pass after every
turn — which also means the agent only learns what the profile lets it, and
nothing at all on the mail route.

## Caveats

- **The MCP servers are examples, not recommendations.** Pin whatever you
  actually run; `@modelcontextprotocol/server-slack` is archived, and the
  Notion and Google servers want credentials a container cannot obtain
  interactively. The Slack server is only for *acting* in Slack beyond
  answering — delete it and the I/O above still works.
- **`simple-agent` has to be on `PATH`** of the process that runs `host.py`: it
  is how the engine reaches the skills and the session search. A server that
  fails to start is logged and skipped, which looks exactly like a tool that
  does not exist — hence the check above.
- **No tool-by-tool progress.** `claude -p` reports once, at the end, so the
  Slack sink's reaction is all anyone sees while a turn runs. Streaming it would
  mean `--output-format stream-json` and reading events as they arrive.
- **The engine's own retries are the engine's.** Rate limits, overload and
  context are Claude Code's problem here, not `providers/`'s, so
  `SIMPLE_AGENT_PROVIDER`, `SIMPLE_AGENT_MODEL` and the background reviewer do
  nothing on these routes.

## Deploy

The root [README](../../README.md#deploy-aws-ecs-fargate) covers the rest:
Postgres, secrets from Secrets Manager, `stopTimeout: 120`, a task role for
Bedrock. The image this host needs is the repo's own image plus the engine, the
runtimes its MCP servers are written in, and this directory:

```dockerfile
FROM ghcr.io/you/simple-agent:latest           # built from this repo's Dockerfile
USER root
RUN apt-get update && apt-get install -y --no-install-recommends nodejs npm \
 && npm install -g @anthropic-ai/claude-code@2.1 \
 && rm -rf /var/lib/apt/lists/*
USER agent
COPY --chown=agent:agent examples/claude-code-host /home/agent/host
COPY --chown=agent:agent examples/claude-code-host/profiles \
     /home/agent/.simple-agent/profiles
CMD ["python", "/home/agent/host/host.py"]
```

The base image's health check still applies: this host is
`simple_agent.host.Host`, so it touches the same heartbeat that
`simple-agent --health` reads.
