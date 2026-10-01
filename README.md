# simple-agent

**A minimal, fully swappable AI agent — bring your own model, inputs, outputs, and memory.**

[English](README.md) · [日本語](README.ja.md)

Most agent projects ship as one integrated product: the loop, the tools, the chat
platforms, the memory, and the model all come together, and changing one means
forking the rest. simple-agent takes the opposite bet. The core is a few
thousand lines of standard-library Python, and every layer around it sits behind
a small interface you can replace from config.

## How it compares

|                        | simple-agent      | [Hermes Agent](https://github.com/NousResearch/hermes-agent) | [OpenClaw](https://github.com/openclaw/openclaw) |
| ---------------------- | ----------------- | ------------------ | ------------------ |
| Language               | Python            | Python             | TypeScript         |
| Runtime dependencies   | **0**             | 45                 | 66                 |
| Lines of code¹         | **~5.3k**         | ~887k              | ~4.4M              |
| License                | MIT               | MIT                | MIT                |

¹ Non-test source lines, measured 2026-09-29 on each default branch.

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
| Harness | `loop` (built in), or an external harness run as a subprocess: `pi`, `claude-code`, `goose`, `opencode` | `SIMPLE_AGENT_ENGINE`, `SIMPLE_AGENT_ENGINE_ARGS` |
| Inputs / outputs | `Source` → `Router` → `Sink`; an IMAP email source is included (the terminal REPL is its own host) | code: `simple_agent/seams.py` |
| Memory | `local`, `mem0`, `hindsight` | `SIMPLE_AGENT_MEMORY_BACKEND` |
| Transcripts | SQLite (default), Postgres | `SIMPLE_AGENT_DATABASE_URL` |

Settings come from environment variables or `~/.simple-agent/config.yaml`
(same keys, lower case); the environment wins. `.env.example` lists them all.

Adding a provider or an engine is one new file plus one line in a registry —
never a change to the loop.

### Mix and match

The three big choices are independent. The harness owns only the loop; the
model, memory, and tool permissions are chosen once and handed to it.

```yaml
# ~/.simple-agent/config.yaml
engine: pi              # who runs the loop
provider: bedrock       # which LLM — passed to pi as --provider amazon-bedrock
memory_backend: mem0    # which memory — pi reads and writes it through our tools
```

External harnesses reach memory, skills, and session search through
`simple-agent --mcp`, an MCP server on stdio that serves only the tools the
current route allows. pi has no MCP client, so a bundled pi extension bridges
to it. Any MCP-capable harness can use it directly, e.g.
`{"command": "simple-agent", "args": ["--mcp", "--tools", "memory_search,memory_save"]}`.
An impossible combination (an engine that cannot run the configured provider)
fails at start.

| Engine | Harness | Providers | How our tools reach it |
| --- | --- | --- | --- |
| `loop` | this repo | all | directly |
| `pi` | [pi](https://github.com/badlogic/pi-mono) | anthropic, bedrock, gemini, openai | pi extension → MCP |
| `claude-code` | [Claude Code](https://docs.claude.com/en/docs/claude-code) | anthropic, bedrock | MCP (`--mcp-config`) |
| `goose` | [Goose](https://github.com/aaif-goose/goose) | anthropic, bedrock, openai | MCP extension |
| `opencode` | [OpenCode](https://github.com/sst/opencode) | anthropic, bedrock, gemini, openai | MCP (inline config) |
| `hermes` | [Hermes Agent](https://github.com/NousResearch/hermes-agent) — self-improving; where this repo started | anthropic, bedrock | MCP (isolated `HERMES_HOME`) |
| `mini-swe` | [mini-swe-agent](https://github.com/SWE-agent/mini-swe-agent) — ~200-line loop, bash is the only tool | anthropic, bedrock, gemini, openai | none: context in the task; needs `terminal` |

The harness decides *how* the work is done, not *what* it is about: a PM,
sales, or marketing agent is the same harness given different tools (MCP
servers) and skills.

## Connect MCP servers

Put servers in `~/.simple-agent/mcp.json`, in the same format Claude Desktop,
Cursor, and Claude Code use:

```json
{"mcpServers": {"google": {"command": "uvx", "args": ["some-google-workspace-mcp"], "env": {"...": "..."}}}}
```

Their tools join the agent as `<server>__<tool>` (e.g. `google__calendar_list`),
on every engine: external harnesses receive them through `simple-agent --mcp`,
so a server is declared once. Allowlists accept patterns, so a route can take
a server's read tools only:

```bash
SIMPLE_AGENT_EMAIL_TOOLS='skill_view,google__*_list,google__*_get' simple-agent --email
```

Stdio servers only. A server that fails to start is logged and skipped.

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
toolset: mail runs with read-only tools by default (`SIMPLE_AGENT_EMAIL_TOOLS`),
and without the background review, so a message cannot write memory or skills
that your trusted sessions later load. Widen it knowingly.

## Deploy (AWS ECS Fargate)

The image holds the agent only. Production defaults are baked in: a non-root
user, JSON logs, no `terminal` tool, a health check (`simple-agent --health`),
and `simple-agent --email` as the command. What is yours — MCP servers and
their configuration — goes in an image built from it:

```dockerfile
FROM ghcr.io/you/simple-agent:latest          # built from this repo's Dockerfile
RUN pip install --user workspace-mcp==1.30.0  # pin what you run
COPY --chown=agent:agent mcp.json /home/agent/.simple-agent/mcp.json
```

Run one task with:

- **State in Postgres** (RDS/Aurora), so the container keeps nothing:
  `SIMPLE_AGENT_DATABASE_URL`, `SIMPLE_AGENT_MEMORY_BACKEND=postgres`,
  `SIMPLE_AGENT_SKILL_BACKEND=postgres`.
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
