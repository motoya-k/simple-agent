---
name: pr-triage
description: Decide which open pull requests need a person now, and say why in one line each.
status: active
uses: 0
---

## When to use

A sweep over the open pull requests of one or more repositories, where the
output is for a human who will act on it — a scheduled report, a stand-up
preparation, a release check.

## Steps

1. Recall the team's conventions from long-term memory first: how long a review
   may wait, which checks are required, who owns which area, and what the team
   counts as a stale draft. These are facts about the team, not part of this
   procedure.
2. For each repository, list the open pull requests. For each one, establish the
   review state, the status of its checks, whether it merges cleanly, and when
   it last moved.
3. Keep only what needs a person now:
   - a required check is failing;
   - approved and mergeable, but still open;
   - waiting for its first review longer than the team allows;
   - conflicting with the base branch;
   - a draft older than the team's staleness window that is still getting commits.
4. Drop everything else without mentioning it, including anything already
   reported earlier in the same conversation.
5. Write one line per item: repository, number, title, what is wrong, who is
   expected to act. Worst first.
6. If nothing qualifies, say exactly what the caller asked you to say in that
   case, and nothing else.

## Notes

- Read-only. Name the action; never take it.
- A pull request can qualify twice (failing checks *and* stale). Report the
  reason that blocks it, not both.
- The thresholds are recalled, never hard-coded here: corrected once in
  long-term memory, every future run is right.
