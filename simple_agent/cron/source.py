"""The clock as a Source: nobody wrote in, the time arrived.

Everything else in :mod:`simple_agent.seams` assumes a message from a person.
A schedule is the same shape with one field filled in differently: the text is
written by whoever wrote the job, months before it runs.  That makes a job *more*
trusted than an inbox, not less — it is the operator's own words — so a job runs
under the host's own profile unless it names another.

Where the answer goes is the job's business too, because a scheduled run has no
"where it came from" to reply to.  A job with no ``to`` runs and tells nobody;
whatever it should do in the world, it does with tools.  A job with
``to: slack:C0123`` posts its answer in that channel.

**At most once, on purpose.**  A firing is claimed before the turn and never
replayed: if the host dies at 09:00:30, the 09:00 digest does not appear at
09:04 on restart, it appears tomorrow.  Mail and Slack replay because somebody
is waiting for an answer; a schedule has next time.  A job that would rather be
late than skipped says ``catch_up: true``.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator, Callable, Iterable

from ..profile import Profile, load_profile
from ..seams import Destination, InboundMessage, Route, Router, Source
from ..skills import parse_frontmatter
from .cursor import Cursor, MemoryCursor
from .schedule import CronSchedule, ScheduleError

log = logging.getLogger(__name__)

PLATFORM = "cron"

#: Never sleep longer than this in one step, so a suspended laptop, a clock
#: correction or a container frozen by its scheduler is noticed within a minute
#: instead of being slept straight through.
MAX_SLEEP = 60.0

#: A firing this far behind is stale: something stalled the host, and running it
#: now would be a surprise rather than a schedule.  ``catch_up`` overrides it.
LATE_GRACE = 300.0

#: How old a missed firing may be and still be made up at startup.
CATCH_UP_WINDOW = 24 * 3600.0


@dataclass(frozen=True)
class Job:
    """One scheduled instruction: when, what to say, and where the answer goes."""

    name: str
    schedule: CronSchedule
    prompt: str
    to: tuple[Destination, ...] = ()
    profile: Profile | None = None
    #: Make up the most recent missed firing at startup — once, not once per
    #: slot that went by.  For a daily report that must not be skipped by a
    #: deploy; never for ``@every``, which has nothing to make up.
    catch_up: bool = False
    #: Keep one growing conversation across firings instead of starting fresh.
    #: Off by default: a scheduled run that reads differently because of what
    #: happened last Tuesday is a scheduled run nobody can debug.
    history: bool = False


class CronSource(Source):
    platform = PLATFORM

    def __init__(
        self,
        jobs: Iterable[Job],
        *,
        cursor: Cursor | None = None,
        now: Callable[[], datetime] | None = None,
        sleep: Callable[[float], object] | None = None,
        max_sleep: float = MAX_SLEEP,
        late_grace: float = LATE_GRACE,
        catch_up_window: float = CATCH_UP_WINDOW,
    ) -> None:
        self.jobs = tuple(jobs)
        self.cursor = cursor or MemoryCursor()
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._sleep = sleep or asyncio.sleep
        self.max_sleep = max_sleep
        self.late_grace = late_grace
        self.catch_up_window = catch_up_window
        self._by_name = {job.name: job for job in self.jobs}

    async def messages(self) -> AsyncIterator[InboundMessage]:
        if not self.jobs:
            return
        await asyncio.to_thread(self.cursor.prune)

        async for message in self._missed():
            yield message

        pending: dict[str, datetime] = {}
        for job in self.jobs:
            pending[job.name] = job.schedule.next_after(self._now())
            log.info("cron: %s next at %s", job.name, pending[job.name].isoformat())

        while pending:
            name = min(pending, key=lambda key: pending[key])
            when = pending[name]
            job = self._by_name[name]
            late = (self._now() - when).total_seconds()
            if late < 0:
                # Short naps rather than one long one; the clock is re-read each
                # time, so a schedule cannot be slept past.
                await self._sleep(min(-late, self.max_sleep))
                continue

            following = self._advance(job, when)
            if following is None:
                del pending[name]
            else:
                pending[name] = following
            if late > self.late_grace and not job.catch_up:
                log.warning("cron: skipping %s due at %s (%.0fs late)", name, when, late)
                continue
            if await asyncio.to_thread(self.cursor.claim, name, when.timestamp()):
                yield self._message(job, when)
            else:
                log.info("cron: %s at %s already ran elsewhere", name, when.isoformat())

    async def _missed(self) -> AsyncIterator[InboundMessage]:
        for job in self.jobs:
            if not job.catch_up:
                continue
            when = job.schedule.previous_at_or_before(self._now())
            if when is None:
                continue
            if (self._now() - when).total_seconds() > self.catch_up_window:
                continue
            if await asyncio.to_thread(self.cursor.claim, job.name, when.timestamp()):
                log.info("cron: making up %s from %s", job.name, when.isoformat())
                yield self._message(job, when)

    def _advance(self, job: Job, when: datetime) -> datetime | None:
        """The firing after ``when``, or ``None`` when the job has no more.

        An interval counts from now rather than from the slot that just fired:
        a host stalled for an hour owes six "every ten minutes" runs, and is not
        going to send them.
        """
        base = self._now() if job.schedule.interval else when
        try:
            return job.schedule.next_after(base)
        except ScheduleError:  # a schedule with an end, e.g. "0 0 29 2 *"
            log.info("cron: %s has no further firing", job.name)
            return None

    def _message(self, job: Job, when: datetime) -> InboundMessage:
        local = when.astimezone(job.schedule.zone)
        stamp = local.strftime("%Y-%m-%d %H:%M %Z").strip()
        return InboundMessage(
            platform=PLATFORM,
            # A fresh conversation per firing unless the job asked to keep one.
            chat_id=job.name if job.history else f"{job.name}@{when.timestamp():.0f}",
            user_id=f"{PLATFORM}:{job.name}",
            # The time is in the text because the alternative is the model
            # guessing, or spending a tool call to find out what day it is.
            text=f"Scheduled run: {job.name} ({job.schedule.expression}) at {stamp}.\n\n{job.prompt}",
            chat_type="dm",
            user_name="schedule",
            chat_name=job.name,
            message_id=f"{job.name}@{when.isoformat()}",
        )


@dataclass(frozen=True)
class CronRouter(Router):
    """Route a firing by the job that produced it: its destinations, its profile.

    ``profile=None`` on a job means the host's own — a job is the operator's own
    instruction, so the default is the toolset the operator already has, not a
    narrowed one.  A job that should reach less says so with ``profile:``.
    """

    jobs: tuple[Job, ...] = ()

    def route(self, message: InboundMessage) -> Route | None:
        if message.platform != PLATFORM:
            return None
        for job in self.jobs:
            if job.name == message.chat_name:
                return Route(to=job.to, profile=job.profile)
        log.warning("cron: no job named %r", message.chat_name)
        return None


# -- jobs on disk ---------------------------------------------------------


def load_jobs(config, directory: Path | None = None) -> tuple[Job, ...]:
    """Every job in ``~/.simple-agent/schedules/``, one file each.

    Same shape as a skill or a profile — frontmatter, then the prompt as the
    body — so there is one file format to learn in this repo::

        ---
        schedule: 0 9 * * 1-5
        tz: Asia/Tokyo
        to: slack:C0123ABC
        catch_up: true
        ---
        Summarize yesterday's failed deploys and anything still red.

    A file with a broken schedule raises here, at startup, rather than being
    skipped quietly and noticed in a month.
    """
    directory = directory or Path(getattr(config, "schedules_dir", config.home / "schedules"))
    jobs = []
    for path in sorted(directory.glob("*.md")):
        jobs.append(_job_from_file(config, path))
    return tuple(jobs)


def _job_from_file(config, path: Path) -> Job:
    meta, body = parse_frontmatter(path.read_text("utf-8"))
    values = {key: value.strip().strip("\"'") for key, value in meta.items()}
    expression = values.get("schedule", "")
    if not expression:
        raise ScheduleError(f"{path}: no 'schedule:' line")
    if not body.strip():
        raise ScheduleError(f"{path}: no prompt — the body is what the agent is asked to do")
    profile_name = values.get("profile", "")
    return Job(
        name=path.stem,
        schedule=CronSchedule.parse(expression, tz=values.get("tz", "")),
        prompt=body.strip(),
        to=parse_destinations(values.get("to", "")),
        profile=load_profile(config, profile_name) if profile_name else None,
        catch_up=_flag(values.get("catch_up", "")),
        history=_flag(values.get("history", "")),
    )


def parse_destinations(text: str) -> tuple[Destination, ...]:
    """``slack:C0123, email:ops@example.com`` as destinations.

    A third part is a thread: ``slack:C0123:1712345678.9``.
    """
    places = []
    for entry in text.split(","):
        entry = entry.strip()
        if not entry:
            continue
        platform, _, rest = entry.partition(":")
        chat_id, _, thread = rest.partition(":")
        if not platform or not chat_id:
            raise ValueError(f"{entry!r}: expected platform:chat_id[:thread]")
        places.append(Destination(platform.strip(), chat_id.strip(), thread.strip()))
    return tuple(places)


def _flag(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}
