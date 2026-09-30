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
| Harness | `loop` (built in), `pi` ([pi-mono](https://github.com/badlogic/pi-mono) as a subprocess) | `SIMPLE_AGENT_ENGINE`, `SIMPLE_AGENT_PI_ARGS` |
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
