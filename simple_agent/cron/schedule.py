"""When a scheduled job fires — a cron expression, parsed once and asked forwards.

Five fields, the ones crontab has had for forty years, because a schedule
somebody has to read at 3am should look like the schedules they already know::

    minute  hour  day-of-month  month  day-of-week
    0       9     *             *      1-5          # weekdays at 09:00
    */15    *     *             *      *            # every quarter hour
    0       0     1             *      *            # first of the month

Also the usual shorthands (``@hourly``, ``@daily``, ``@weekly``, ``@monthly``,
``@yearly``, ``@midnight``) and one addition, ``@every 10m``, for the jobs that
are really a poll and have no business being pinned to the clock.

Two details that are easy to get wrong and are written down rather than assumed:

* **Day-of-month and day-of-week are an OR**, not an AND, when both are given —
  ``0 0 13 * 5`` is the 13th *and* every Friday.  That is what cron does, and a
  schedule that quietly means something else than the same line in crontab is
  worse than no schedule.
* **A time is a wall-clock time in the job's zone.**  The search walks absolute
  time and reads the wall clock at each step, so a daily 09:00 stays 09:00
  across a DST change instead of drifting with it.  A wall-clock time that a
  DST jump deletes simply does not happen that day.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo

#: How far ahead a search will look before calling the expression unsatisfiable
#: (``0 0 30 2 *`` — the 30th of February — is a typo, not a schedule).
LOOKAHEAD_DAYS = 400

MINUTE = timedelta(minutes=1)

SHORTHAND = {
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
    "@monthly": "0 0 1 * *",
    "@weekly": "0 0 * * 0",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@hourly": "0 * * * *",
}

_MONTHS = {
    name: number
    for number, name in enumerate(
        "jan feb mar apr may jun jul aug sep oct nov dec".split(), start=1
    )
}
#: Cron counts weekdays from Sunday, and accepts 7 for it as well as 0.
_WEEKDAYS = {name: number for number, name in enumerate("sun mon tue wed thu fri sat".split())}

_EVERY = re.compile(r"^@every\s+(\d+)\s*([smh])$", re.IGNORECASE)
_UNITS = {"s": 1, "m": 60, "h": 3600}


class ScheduleError(ValueError):
    """An expression that cannot mean anything. Raised at load time, not at 3am."""


@dataclass(frozen=True)
class CronSchedule:
    expression: str
    zone: tzinfo
    minutes: frozenset[int] = frozenset()
    hours: frozenset[int] = frozenset()
    days: frozenset[int] = frozenset()
    months: frozenset[int] = frozenset()
    weekdays: frozenset[int] = frozenset()
    day_given: bool = False
    weekday_given: bool = False
    #: Set for ``@every N`` instead of the five fields: fires this many seconds
    #: after the last firing, with no reference to the clock at all.
    interval: float = 0.0

    # -- building -------------------------------------------------------
    @classmethod
    def parse(cls, expression: str, *, tz: str = "") -> "CronSchedule":
        text = " ".join(expression.split())
        if not text:
            raise ScheduleError("empty schedule")
        zone = _zone(tz)

        every = _EVERY.match(text)
        if every:
            seconds = int(every.group(1)) * _UNITS[every.group(2).lower()]
            if seconds <= 0:
                raise ScheduleError(f"{expression!r}: an interval of zero never ends")
            return cls(expression=text, zone=zone, interval=float(seconds))

        text = SHORTHAND.get(text.lower(), text)
        fields = text.split(" ")
        if len(fields) != 5:
            raise ScheduleError(
                f"{expression!r}: expected 5 fields "
                "(minute hour day-of-month month day-of-week), got "
                f"{len(fields)}"
            )
        minute, hour, day, month, weekday = fields
        return cls(
            expression=text,
            zone=zone,
            minutes=_field(minute, 0, 59),
            hours=_field(hour, 0, 23),
            days=_field(day, 1, 31),
            months=_field(month, 1, 12, names=_MONTHS),
            weekdays=frozenset(value % 7 for value in _field(weekday, 0, 7, names=_WEEKDAYS)),
            day_given=day.strip() not in {"*", "?"},
            weekday_given=weekday.strip() not in {"*", "?"},
        )

    # -- asking ---------------------------------------------------------
    def matches(self, moment: datetime) -> bool:
        """Does this wall-clock minute, in this schedule's zone, fire?"""
        local = moment.astimezone(self.zone)
        if local.minute not in self.minutes or local.hour not in self.hours:
            return False
        if local.month not in self.months:
            return False
        day = local.day in self.days
        weekday = (local.isoweekday() % 7) in self.weekdays
        if self.day_given and self.weekday_given:
            return day or weekday
        return day and weekday

    def next_after(self, moment: datetime) -> datetime:
        """The first firing strictly after ``moment``, as an aware UTC datetime."""
        if self.interval:
            return (moment + timedelta(seconds=self.interval)).astimezone(timezone.utc)
        cursor = _next_minute(moment)
        limit = cursor + timedelta(days=LOOKAHEAD_DAYS)
        while cursor <= limit:
            if self.matches(cursor):
                return cursor
            cursor += MINUTE
        raise ScheduleError(
            f"{self.expression!r} does not fire in the next {LOOKAHEAD_DAYS} days"
        )

    def previous_at_or_before(self, moment: datetime) -> datetime | None:
        """The most recent firing up to ``moment`` — what a missed run was.

        ``None`` when there is none within the lookahead, and never anything for
        an interval schedule: "every ten minutes" has no missed slot to make up,
        the next one is ten minutes away.
        """
        if self.interval:
            return None
        cursor = moment.astimezone(timezone.utc).replace(second=0, microsecond=0)
        floor = cursor - timedelta(days=LOOKAHEAD_DAYS)
        while cursor >= floor:
            if self.matches(cursor):
                return cursor
            cursor -= MINUTE
        return None


def _next_minute(moment: datetime) -> datetime:
    return moment.astimezone(timezone.utc).replace(second=0, microsecond=0) + MINUTE


def _zone(name: str) -> tzinfo:
    """The job's zone, or the host's.

    A container's local zone is usually UTC, which is exactly the wrong thing to
    guess at for "every morning at nine", so a job that cares says so.
    """
    if not name:
        return datetime.now().astimezone().tzinfo or timezone.utc
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception as exc:  # no tzdata in the image, or a typo in the name
        raise ScheduleError(f"unknown time zone {name!r}: {exc}") from exc


def _field(
    spec: str, low: int, high: int, names: dict[str, int] | None = None
) -> frozenset[int]:
    """One cron field as the set of values it allows."""
    spec = spec.strip()
    if spec in {"*", "?"}:
        return frozenset(range(low, high + 1))
    values: set[int] = set()
    for part in spec.split(","):
        body, _, step_text = part.partition("/")
        try:
            step = int(step_text) if step_text else 1
        except ValueError:
            raise ScheduleError(f"{part!r}: step must be a number") from None
        if step < 1:
            raise ScheduleError(f"{part!r}: step must be positive")
        if body.strip() in {"*", ""}:
            start, end = low, high
        else:
            start_text, dash, end_text = body.partition("-")
            start = _value(start_text, low, high, names)
            end = _value(end_text, low, high, names) if dash else start
            if dash and end < start:
                # Cron has no wrapping range; "fri-mon" is a typo for two parts.
                raise ScheduleError(f"{part!r}: range runs backwards")
        values.update(range(start, end + 1, step))
    if not values:
        raise ScheduleError(f"{spec!r}: matches nothing")
    return frozenset(values)


def _value(text: str, low: int, high: int, names: dict[str, int] | None) -> int:
    text = text.strip().lower()
    if names and text[:3] in names:
        return names[text[:3]]
    try:
        number = int(text)
    except ValueError:
        raise ScheduleError(f"{text!r}: not a number in {low}-{high}") from None
    if not low <= number <= high:
        raise ScheduleError(f"{number}: outside {low}-{high}")
    return number
