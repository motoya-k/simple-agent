# A scheduled sweep: which pull requests need a person

```
                                        ┌─ nothing wrong → nothing posted
cron ─→ CronSource ─→ Agent ────────────┤
        (the clock       │              └─ something wrong → #ops in Slack
         writes in)      │
                 ┌───────┴────────┐
                 │ github (MCP)   │  read-only, pull requests only
                 │ pr-triage      │  the procedure (a skill)
                 │ long-term mem. │  the team's own numbers
                 └────────────────┘
```

There is no code in this example. A scheduled run is a transport like any
other — [`simple_agent/cron`](../../simple_agent/cron) is the Source, the
Router and the job loader — so a deployment is four files and
`simple-agent --cron`.

| File | Goes to | What it says |
| --- | --- | --- |
| [`schedules/pr-watch.md`](schedules/pr-watch.md) | `~/.simple-agent/schedules/` | **When**, **what** to ask, and **where** the answer goes. |
| [`profiles/pr-watch.md`](profiles/pr-watch.md) | `~/.simple-agent/profiles/` | **Who** it is: its instructions, what it may touch, whether it may learn. |
| [`skills/pr-triage/`](skills/pr-triage) | `~/.simple-agent/skills/` | **How** to triage — with no team-specific number in it. |
| [`mcp.json`](mcp.json) | `~/.simple-agent/mcp.json` | GitHub's MCP server, read-only, pull requests only. |

## Run it

```bash
pip install -e .                                  # from the repo root
cp examples/pr-watch/mcp.json ~/.simple-agent/mcp.json
cp examples/pr-watch/profiles/pr-watch.md ~/.simple-agent/profiles/
cp examples/pr-watch/schedules/pr-watch.md ~/.simple-agent/schedules/
cp -r examples/pr-watch/skills/pr-triage ~/.simple-agent/skills/
$EDITOR ~/.simple-agent/schedules/pr-watch.md     # the repositories, the channel

cp examples/pr-watch/.env.example .env            # the two tokens; see below
simple-agent --cron
```

That is the whole deployment: one process, waking for each firing, posting into
Slack when something needs a person. `--cron --slack` in one command adds
answering questions in the same workspace, on the same agent registry and the
same concurrency limits.

Before trusting a schedule, run the job by hand once — it is also how you find
out whether the team's conventions are in long-term memory yet:

```bash
simple-agent --profile pr-watch "Follow the pr-triage skill for owner/service-api."
```

Without those conventions the answer will say it could not tell what counts as
late, which is correct, and the fix is to say it once:

```bash
simple-agent "remember: a pull request waits at most one working day for a first
review, the required checks are build and e2e, and the web app is Ana's area"
```

### The environment

Secrets are environment variables only, never files, which is what makes the
four files above safe to commit ([`.env.example`](.env.example)):

| | |
| --- | --- |
| `GITHUB_PERSONAL_ACCESS_TOKEN` | a PAT with read access to the repositories. `mcp.json` passes it to the container with `docker run -e GITHUB_PERSONAL_ACCESS_TOKEN` and no value, so the token stays in the environment. |
| `SIMPLE_AGENT_SLACK_BOT_TOKEN` | needed by the sink the job's `to: slack:…` asks for. Without it the report goes to the dead-letter file. |
| `ANTHROPIC_API_KEY` | or another provider's; `SIMPLE_AGENT_PROVIDER=bedrock` and a task role works too. |
| `SIMPLE_AGENT_DATABASE_URL` | optional on one machine, required in a container: both the transcript and the cron cursor live there. |

## Decisions worth knowing

**The job is the operator's own words.** Nobody wrote in — the time arrived —
so the prompt in the job file was written months before it runs, by whoever
deployed it. That makes a schedule *more* trusted than an inbox, and it is why
the job names the repositories: they are part of the instruction, not a setting.

**`history: true` is why it does not repeat itself.** Firings share one
conversation, so the sweep knows what it already said. The cost is a transcript
that grows all week; compaction keeps it affordable. Drop the line and every
firing starts clean — and tells you about the same stuck pull request forty
times.

**At most once, never replayed.** A firing is claimed before the turn, so a host
that dies at 09:00:30 does not post the 09:00 digest at 09:04 on restart; it
posts tomorrow's. Mail and Slack replay because somebody is waiting. A schedule
has next time — `catch_up: true` for a daily report that must not be skipped by
a deploy.

**Read-only, twice.** The profile hands out no tool that writes, *and* the
GitHub server runs with `GITHUB_READ_ONLY=1`. An unattended job that concludes
something should be merged must not be able to merge it.

**It teaches nothing.** `learning: false`: other people's pull requests are not
where the team's long-term memory should come from. It reads memory and writes
none — the two halves of one profile, as in
[`profile.py`](../../simple_agent/profile.py).

**The procedure and the numbers are separate.** The skill says "longer than the
team allows"; long-term memory says what that is. Correct the number once and
every future run is right, including runs of every other skill that needs it. A
skill with `24 hours` written into it is a skill you have to find and edit.

## Caveats

- **Silence depends on the model.** The job asks for an empty answer when
  nothing qualifies, and the host does not post an empty answer — but a model
  determined to be helpful will write "nothing to report" anyway, and that will
  be posted. If it becomes a problem, the fix is a Sink that drops a sentinel,
  twelve lines wrapping `SlackSink`, and the host needs no change:

  ```python
  @dataclass
  class QuietSink(Sink):
      inner: Sink
      sentinel: str = "NOTHING_TO_REPORT"

      @property
      def platform(self) -> str:
          return self.inner.platform

      async def send(self, to, text):
          if text.strip().strip(".").upper() != self.sentinel:
              await self.inner.send(to, text)
  ```

- **Docker**, for the GitHub server. With a Copilot seat there is a remote one
  (`https://api.githubcopilot.com/mcp/x/pull_requests/readonly`), but this
  repo's MCP client speaks stdio only — a remote server belongs in
  [`../claude-code-host`](../claude-code-host), where Claude Code is the client.
- **A PAT is a person's access.** Scope it to the repositories you watch, or use
  a GitHub App's token.
- **Timezones belong in the job.** `tz: Asia/Tokyo` with `*/30 9-19 * * 1-5`
  means working hours there, not wherever the container happens to think it is.
