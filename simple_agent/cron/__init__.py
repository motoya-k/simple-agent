"""Scheduled runs as a Source — the clock is the thing that writes in.

What a host needs from here::

    jobs = load_jobs(config)                       # ~/.simple-agent/schedules/*.md
    source = CronSource(jobs, cursor=open_cursor(config))
    router = CronRouter(jobs)                      # each job names where its answer goes

:mod:`.schedule` parses the cron expression, :mod:`.cursor` remembers which
firings have happened, and :mod:`.source` is the Source, the Router and the
loader.
"""

from .cursor import Cursor, MemoryCursor, PostgresCursor, SqliteCursor, open_cursor
from .schedule import CronSchedule, ScheduleError
from .source import (
    PLATFORM,
    CronRouter,
    CronSource,
    Job,
    load_jobs,
    parse_destinations,
)

__all__ = [
    "PLATFORM",
    "CronRouter",
    "CronSchedule",
    "CronSource",
    "Cursor",
    "Job",
    "MemoryCursor",
    "PostgresCursor",
    "ScheduleError",
    "SqliteCursor",
    "load_jobs",
    "open_cursor",
    "parse_destinations",
]
