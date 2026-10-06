# simple-agent

**A minimal, fully swappable AI agent — bring your own model, inputs, outputs, and memory.**

[English](README.md) · [日本語](README.ja.md)

Most agent projects ship as one integrated product: the loop, the tools, the chat
platforms, the memory, and the model all come together, and changing one means
forking the rest. simple-agent takes the opposite bet. The core is a few
thousand lines of standard-library Python, and every layer around it sits behind
a small interface you can replace from config.

New here? [INTRODUCTION.md](INTRODUCTION.md) walks through the whole agent — the
loop, the tools, the three memory layers, and what production needs — assuming
you have never read an agent implementation before.

## How it compares

|                        | simple-agent      | [Hermes Agent](https://github.com/NousResearch/hermes-agent) | [OpenClaw](https://github.com/openclaw/openclaw) |
| ---------------------- | ----------------- | ------------------ | ------------------ |
| Language               | Python            | Python             | TypeScript         |
| Runtime dependencies   | **0**             | 45                 | 66                 |
| Lines of code¹         | **~6.4k**         | ~887k              | ~4.4M              |
| License                | MIT               | MIT                | MIT                |

¹ Non-test source lines: simple-agent on 2026-10-06, the other two on
2026-09-29, each on its default branch.

Hermes and OpenClaw are full products and do far more out of the box — dozens
of chat platforms, apps, sandboxes. simple-agent is for when you would rather
read the whole agent in an afternoon and replace the parts yourself.

## Quick start

```bash
git clone https://github.com/motoya-k/simple-agent.git && cd simple-agent
pip install -e .
cp .env.example .env        # set ANTHROPIC_API_KEY (or another provider's key)

simple-agent                # interactive
simple-agent "summarize this repo"   # one shot
```

Python 3.10+. No other dependencies.

## Swap anything

| Layer | Options | Set with |
| --- | --- | --- |
| Model | `anthropic`, `bedrock` (Converse; Bedrock API key or IAM via `AWS_PROFILE`), `gemini`, `openai` (Responses) | `SIMPLE_AGENT_PROVIDER`, `SIMPLE_AGENT_MODEL` |
| Who the agent is | a profile: instructions, tools, learning, namespace | `SIMPLE_AGENT_PROFILE`, `~/.simple-agent/profiles/<name>.md` |
| Inputs / outputs | `Source` → `Router` → `Sink`; email (IMAP), Slack (Socket Mode) and the clock (cron) are included, and compose in one process (the terminal REPL is its own host) | code: `simple_agent/seams.py` |
| Tools | any stdio MCP server, plus the built-ins | `~/.simple-agent/mcp.json` |
| Tool policy | mods: refuse, rewrite or redact a tool call | `~/.simple-agent/mods/<name>.py` |
| Storage | files under `~/.simple-agent` (default) or Postgres — transcripts, memory and skills together | `SIMPLE_AGENT_DATABASE_URL` |

Settings come from environment variables or `~/.simple-agent/config.yaml`
(same keys, lower case); the environment wins. `.env.example` lists them all.

Adding a provider is one new file plus one line in a registry — never a
change to the loop.

### One place for everything it writes

Three things outlive a turn: the transcripts, long-term memory (what the team
knows), and skills (procedures the agent wrote for itself). They follow a
single setting, so a deployment cannot be half-moved:

- **unset** — files under `~/.simple-agent`: SQLite for transcripts, one JSONL
  per namespace for memory, a directory per skill. All of it readable, and
  correctable, in an editor.
- **`SIMPLE_AGENT_DATABASE_URL=postgresql://...`** — all three in Postgres, for
  a container whose disk does not outlive it. `pip install ".[postgres]"`.

`SIMPLE_AGENT_MEMORY_NAMESPACE` is the team the knowledge and the conversations
belong to; two teams on one deployment share neither.

A hosted memory service — mem0, Hindsight, a vector store of your own — is not
a backend here. It is an MCP server (see below), so it is declared once and
every harness reaches it, this one included.

## Profiles: who the agent is on a route

The same core answers a person at a terminal and a stranger who wrote to an
inbox — but not in the same way. A profile is that difference, named and in one
file: the instructions in the system prompt, the toolset, whether the
conversation may write to long-term memory and skills, and whose memory it
reads.

Three are built in. `terminal` has every tool and learns; `email` and `slack`
have read-only tools and learn nothing, because a route anyone can write into
must not be able to teach the conversations you trust. Those two halves are one
setting each, in one place, so they cannot drift apart.

Amend either, or add your own, with a file — the same shape as a skill:

```markdown
---
tools: skill_view, google__*_list
learning: false
namespace: support
imap_host: imap.gmail.com
imap_user: support@example.com
email_allow: "@example.com"
---

You answer support mail. Look things up before answering; never promise a refund.
```

```bash
simple-agent --profile support            # at the terminal
simple-agent --email --profile support    # or as the mail host
```

`tools: '*'` means every tool. Environment variables still win, so a container
needs no file: `SIMPLE_AGENT_SUPPORT_TOOLS`, `_LEARNING`, `_NAMESPACE` for the
fields and `SIMPLE_AGENT_IMAP_HOST`, `SIMPLE_AGENT_EMAIL_ALLOW` for the
settings. A password is read from the environment only, never from the file, so
a profile is safe to commit.

## Mods: a say in every tool call

Every other seam here is something you implement — a provider translates, a
Source receives, a tool acts. A mod is the one thing none of them can be: a
decision about a tool call that neither the model nor the tool should be
trusted to make. *Never `rm -rf` on this route. Strip the token out of
whatever the shell prints. Rewrite that path so it cannot leave the project.*

A mod is a file at `~/.simple-agent/mods/<name>.py` with either hook, or both:

```python
def before_tool(name, arguments):
    if name == "terminal" and "rm -rf" in arguments.get("command", ""):
        return Deny("rm -rf is not allowed here")
    return None                                  # no opinion

def after_tool(name, arguments, output, is_error):
    return output.replace(TOKEN, "[redacted]")   # what the model will read
```

`before_tool` returns `None`, a `Deny` (already in scope; importing it works
too), or replacement `arguments`. A refusal reaches the model as an ordinary
error result, so it reads why and carries on. Mods run in the order they are
named, each seeing what the last one decided.

A profile names the mods for its route and `SIMPLE_AGENT_MODS` names the ones
that run everywhere, which a profile cannot drop:

```markdown
---
tools: '*'
mods: no-rm, redact-secrets
---
```

`ToolRegistry.call` is the only path from a tool call to an action — the loop,
the background reviewer, the REPL's slash commands and `--mcp` all go through
it — so a rule written once holds whoever asked, including a harness borrowing
the toolset over MCP.

**The two hooks fail in opposite directions, deliberately.** A `before_tool`
that raises *denies* the call, and a mod a profile names but that is missing
stops the agent at startup: a policy that crashed has approved nothing. An
`after_tool` that raises is ignored and the original output stands, because it
only shapes what the model reads and a lost redaction pass should not cost the
turn.

Two things to know before writing one. A mod is Python in this process, not an
MCP server in another — it is trusted code with the agent's own powers, so a
mod is something you wrote or read, never something you installed. And
read-only tools fan out across threads, so a hook must be safe to call from
several at once.

## Use from another harness

The loop is this repo's own. To work from Claude Code, Codex, Goose, or any
other MCP-capable harness instead, register `simple-agent --mcp` there: it
serves memory, skills, and session search over MCP on stdio, so that harness
reads and writes the same memory as this agent, whichever backend is
configured. `--tools` narrows what it serves:

```json
{"command": "simple-agent", "args": ["--mcp", "--tools", "memory_search,memory_save"]}
```

`--profile` lends that harness a profile instead — its toolset and its memory
namespace — and `--tools` narrows what is left, never widens it.

Or let the other harness take the loop entirely.
[`examples/claude-code-host`](examples/claude-code-host) is a working
deployment where `claude -p` answers the messages and this repo keeps the rest:
Slack and an inbox as the routes, a profile per route (translated into Claude
Code's allowlist, both halves enforced), the conversations in Postgres, and
Hindsight as long-term memory over MCP. The other shape is in this repo
already: a schedule as a Source, sweeping GitHub pull requests on a cron and
staying quiet unless one needs a person.

A PM, sales, or marketing agent is the same loop given different tools (MCP
servers) and skills.

## Connect MCP servers

Put servers in `~/.simple-agent/mcp.json`, in the same format Claude Desktop,
Cursor, and Claude Code use:

```json
{"mcpServers": {"google": {"command": "uvx", "args": ["some-google-workspace-mcp"], "env": {"...": "..."}}}}
```

Their tools join the agent as `<server>__<tool>` (e.g. `google__calendar_list`),
and other harnesses receive them through `simple-agent --mcp`, so a server is
declared once. Allowlists accept patterns, so a route can take
a server's read tools only:

```bash
SIMPLE_AGENT_EMAIL_TOOLS='skill_view,google__*_list,google__*_get' simple-agent --email
```

Stdio servers only. A server that fails to start is logged and skipped.

This is also where a hosted memory service goes. mem0, Hindsight and the rest
publish MCP servers; adding one here gives the agent their tools alongside its
own, without this repo carrying an adapter for each.

## Email

```bash
SIMPLE_AGENT_IMAP_HOST=imap.gmail.com \
SIMPLE_AGENT_IMAP_USER=agent@example.com \
SIMPLE_AGENT_IMAP_PASSWORD=... \
SIMPLE_AGENT_EMAIL_ALLOW=@example.com \
simple-agent --email
```

Polls the mailbox, runs one conversation per sender and thread, and answers
nobody: whatever the agent should do, it does through tools. The mailbox is
never modified (read-only, `BODY.PEEK`); handled mail is tracked in the agent's
own database, and existing mail is treated as backlog and skipped.

**Mail is untrusted input.** Anyone can write to an inbox, and a sender address
is easy to forge, so the allowlist only saves money. The real boundary is the
`email` profile: read-only tools and `learning: false`, so a message cannot
write memory or skills that your trusted sessions later load. Widen it
knowingly, and widen both halves consciously — they are two lines of the same
file.

## Slack

```bash
SIMPLE_AGENT_SLACK_APP_TOKEN=xapp-...   # app-level token, connections:write
SIMPLE_AGENT_SLACK_BOT_TOKEN=xoxb-...   # bot token
SIMPLE_AGENT_SLACK_ALLOW='#ops,dm' \
simple-agent --slack
```

Socket Mode, so the connection is outbound: no public URL, no load balancer, no
request signatures — the same container that runs the mail host runs this one.
In the Slack app: enable Socket Mode, subscribe to `message.channels` and
`message.im`, and give the bot `chat:write`, `reactions:write` and
`channels:history` / `im:history`.

What it does with a message: in a channel it answers only when mentioned, in a
thread off the question, so the question, the answer and every follow-up are one
conversation. A direct message needs no mention. While a turn runs the
triggering message carries a 👀 — progress belongs on the message, not as a
commentary in a channel everyone is reading. Markdown is converted to Slack's
own markup on the way out, and an answer too long for one message is split
without breaking a code block.

Each event is written down before it is acknowledged (Socket Mode gives you
three seconds, which is not an answer), so a crash mid-turn replays the question
instead of losing it, and a re-delivery after a reconnect does not answer twice.

A message that is nothing but **`stop`** — or `cancel`, `やめて`, `中止`, and a
few more — means "do not post what you are writing". A thread is answered in
order, so it is the one message that cannot wait its turn: it is read as it
arrives rather than after the lock, and the answer in flight is dropped instead
of delivered. Only whole messages count, so *"stop the deploy and tell me what
broke"* is a question. The work itself still finishes — this cancels the answer,
not the turn — and `stop_words` in the profile replaces the list, or `none`
turns it off.

Like mail, a workspace is **untrusted input**: anyone in it can type. The `slack`
profile is read-only with `learning: false`, and `slack_allow` is a cost filter,
not a boundary.

## Schedules

A job is a file in `~/.simple-agent/schedules/`, the same shape as a skill or a
profile — frontmatter, then the prompt:

```markdown
---
schedule: 0 9 * * 1-5        # also @daily, @hourly, @every 10m
tz: Asia/Tokyo
to: slack:C0123ABC           # where the answer goes; omit to tell nobody
catch_up: true               # make up a firing a deploy spanned
---

Summarize yesterday's failed deploys and anything still red.
```

```bash
simple-agent --cron            # schedules only
simple-agent --slack --cron    # one process: questions and schedules
```

The clock is a Source like any other, so a scheduled run is a message with the
time filled in by whoever wrote the job — which makes it *more* trusted than an
inbox, not less. A job keeps the host's own profile unless it names another, and
each firing is a fresh conversation unless it asks for `history: true`.

A firing is claimed before the turn and never replayed: if the host dies at
09:00:30, the digest arrives tomorrow, not four minutes later. Mail and Slack
replay because somebody is waiting; a schedule has next time. The claim is also
what keeps two tasks on one database from sending everything twice.

## Deploy (AWS ECS Fargate)

The image holds the agent only. Production defaults are baked in: a non-root
user, JSON logs, no `terminal` tool, a health check (`simple-agent --health`),
and `simple-agent --email` as the command — change it to `--slack --cron`, or
any combination, and it is still one task. What is yours — MCP servers and their
configuration — goes in an image built from it:

```dockerfile
FROM ghcr.io/you/simple-agent:latest          # built from this repo's Dockerfile
RUN pip install --user workspace-mcp==1.30.0  # pin what you run
COPY --chown=agent:agent mcp.json /home/agent/.simple-agent/mcp.json
COPY --chown=agent:agent support.md /home/agent/.simple-agent/profiles/support.md
```

Run one task with:

- **State in Postgres** (RDS/Aurora), so the container keeps nothing: one
  `SIMPLE_AGENT_DATABASE_URL` moves the transcripts, the memory and the skills.
- **A task role** with `bedrock:InvokeModel` on the inference profile and the
  models it routes to. Credentials come from the role and are refreshed
  automatically; no keys in the task.
- **Secrets from Secrets Manager** as environment variables
  (`SIMPLE_AGENT_IMAP_PASSWORD`, the database URL, MCP servers' keys — MCP
  servers inherit the environment).
- `stopTimeout: 120` (turns in flight get 90s to finish after SIGTERM),
  `initProcessEnabled: true` (reaps MCP server processes), `desiredCount: 1`.

`deploy/terraform` sets all of this up in an existing VPC — ECR, the cluster
and service, task and execution roles (Bedrock limited to the two configured
inference profiles), Secrets Manager entries, logs, and an RDS Postgres:

```bash
cd deploy/terraform && cp terraform.tfvars.example terraform.tfvars  # fill in
terraform init -backend-config="bucket=..." -backend-config="key=simple-agent.tfstate" \
  -backend-config="region=ap-northeast-1" -backend-config="encrypt=true"
terraform apply
```

Model calls retry 429/5xx with backoff; up to `SIMPLE_AGENT_MAX_CONCURRENT_TURNS`
(default 4) conversations run at once, each one in order.

## Development

```bash
pip install -e ".[dev]"
pytest
```

## Acknowledgements

The design started as a rewrite of [Hermes Agent](https://github.com/NousResearch/hermes-agent)
by Nous Research. `simple_agent/session.py` and the review prompts in
`simple_agent/review.py` are adapted from it under the MIT License; see
[LICENSE](LICENSE).

## License

[MIT](LICENSE)
