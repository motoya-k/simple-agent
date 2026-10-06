# Examples

Each one is a whole deployment, assembled from the same parts: a `Source`, a
`Router`, a `Sink`, a `Profile` per route, and somewhere to keep what outlives a
turn. They differ in what sits in the middle and what sends the messages.

| | What it shows |
| --- | --- |
| [`claude-code-host`](claude-code-host) | **Another harness as the engine.** `claude -p` answers; this repo keeps the routes (Slack and an inbox), the profiles translated into Claude Code's allowlist, the conversations in Postgres, and Hindsight as long-term memory over MCP — alongside Notion, Slack and Google Workspace as tools. |
| [`pr-watch`](pr-watch) | **Time as a Source.** A scheduled sweep over GitHub pull requests that reports only what needs a person, and says nothing when nothing does. One conversation per day, read-only, teaches nothing. |

Neither is imported by `simple_agent`; both are read by `tests/`, so they
break loudly rather than quietly rotting.
