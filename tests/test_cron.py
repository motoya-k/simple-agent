"""The clock as a Source: expressions, firings, and what reaches the agent."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from simple_agent.config import Config
from simple_agent.cron import (
    CronRouter,
    CronSchedule,
    CronSource,
    Job,
    MemoryCursor,
    ScheduleError,
    load_jobs,
    parse_destinations,
)
from simple_agent.cron.cursor import SqliteCursor
from simple_agent.providers.base import Provider, Response
from simple_agent.seams import Destination

TOKYO = "Asia/Tokyo"


def utc(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc)


# -- expressions ----------------------------------------------------------


def test_weekdays_at_nine_in_the_zone_that_was_asked_for():
    schedule = CronSchedule.parse("0 9 * * 1-5", tz=TOKYO)

    # Saturday 10:00 in Tokyo -> Monday 09:00 in Tokyo, which is Sunday in UTC.
    assert schedule.next_after(utc("2026-10-03T01:00")) == utc("2026-10-05T00:00")


def test_the_same_line_means_what_crontab_means():
    quarter = CronSchedule.parse("*/15 * * * *", tz="UTC")
    assert quarter.next_after(utc("2026-10-03T01:00")) == utc("2026-10-03T01:15")

    # Day-of-month and day-of-week together are an OR: the 13th *or* any Friday.
    both = CronSchedule.parse("0 0 13 * 5", tz="UTC")
    assert both.next_after(utc("2026-10-03T01:00")) == utc("2026-10-09T00:00")  # a Friday
    assert both.next_after(utc("2026-10-10T00:00")) == utc("2026-10-13T00:00")  # the 13th

    named = CronSchedule.parse("30 6 * jan mon", tz="UTC")
    assert named.next_after(utc("2026-10-03T01:00")) == utc("2027-01-04T06:30")


def test_shorthands_and_intervals():
    assert CronSchedule.parse("@daily", tz="UTC").next_after(utc("2026-10-03T01:00")) == utc(
        "2026-10-04T00:00"
    )
    every = CronSchedule.parse("@every 10m")
    assert every.next_after(utc("2026-10-03T01:00")) == utc("2026-10-03T01:10")
    # An interval has no slot it could have missed.
    assert every.previous_at_or_before(utc("2026-10-03T01:00")) is None


def test_a_schedule_that_cannot_mean_anything_is_refused_at_load_time():
    for broken in ("", "0 9 * *", "0 9 * * xyz", "99 * * * *", "0 fri-mon * * *", "*/0 * * * *"):
        with pytest.raises(ScheduleError):
            CronSchedule.parse(broken)
    with pytest.raises(ScheduleError):
        CronSchedule.parse("0 9 * * *", tz="Mars/Olympus")
    with pytest.raises(ScheduleError):
        CronSchedule.parse("0 0 30 2 *").next_after(utc("2026-10-03T01:00"))  # 30 February


def test_a_daily_time_stays_at_that_time_across_a_clock_change():
    """New York loses an hour on 8 March 2026; 09:00 is still 09:00."""
    schedule = CronSchedule.parse("0 9 * * *", tz="America/New_York")

    before = schedule.next_after(utc("2026-03-07T06:00"))
    after = schedule.next_after(utc("2026-03-08T06:00"))

    assert before == utc("2026-03-07T14:00")  # 09:00 EST
    assert after == utc("2026-03-08T13:00")  # 09:00 EDT: an hour earlier in UTC,
    # same hour on the wall. A schedule that drifted with the offset would have
    # fired at 14:00 UTC, which is 10:00 to the person reading it.


# -- firing ---------------------------------------------------------------


class Clock:
    """A clock the test moves, so a day of schedule runs in a millisecond."""

    def __init__(self, start: datetime) -> None:
        self.moment = start
        self.slept = 0.0

    def now(self) -> datetime:
        return self.moment

    async def sleep(self, seconds: float) -> None:
        self.slept += seconds
        self.moment += timedelta(seconds=seconds)
        await asyncio.sleep(0)  # let the loop breathe, so a test can time out


def job(name="digest", expression="0 9 * * *", **kwargs) -> Job:
    return Job(
        name=name,
        schedule=CronSchedule.parse(expression, tz="UTC"),
        prompt=kwargs.pop("prompt", "Summarize yesterday."),
        **kwargs,
    )


def collect(source: CronSource, count: int, timeout: float = 5.0):
    async def run():
        found = []

        async def drain():
            async for message in source.messages():
                found.append(message)
                if len(found) >= count:
                    return

        await asyncio.wait_for(drain(), timeout)
        return found

    return asyncio.run(run())


def test_a_job_fires_at_its_time_and_says_what_it_is():
    clock = Clock(utc("2026-10-06T08:58"))
    source = CronSource([job()], now=clock.now, sleep=clock.sleep)

    [message] = collect(source, 1)

    assert message.platform == "cron"
    assert message.chat_name == "digest"
    assert message.text.startswith("Scheduled run: digest (0 9 * * *) at 2026-10-06 09:00")
    assert message.text.endswith("Summarize yesterday.")
    assert clock.moment == utc("2026-10-06T09:00")  # it waited, it did not spin


def test_the_next_job_due_is_the_one_that_fires():
    clock = Clock(utc("2026-10-06T08:00"))
    source = CronSource(
        [job("nightly", "0 23 * * *"), job("standup", "30 9 * * *"), job("digest", "0 9 * * *")],
        now=clock.now,
        sleep=clock.sleep,
    )

    found = collect(source, 3)

    assert [m.chat_name for m in found] == ["digest", "standup", "nightly"]


def test_every_firing_is_its_own_conversation_unless_the_job_keeps_one():
    clock = Clock(utc("2026-10-06T08:59"))
    fresh = collect(CronSource([job("d", "@every 1h")], now=clock.now, sleep=clock.sleep), 2)

    clock = Clock(utc("2026-10-06T08:59"))
    kept = collect(
        CronSource([job("d", "@every 1h", history=True)], now=clock.now, sleep=clock.sleep), 2
    )

    assert fresh[0].session_key() != fresh[1].session_key()
    assert kept[0].session_key() == kept[1].session_key()


def test_a_firing_runs_once_even_with_two_hosts_on_one_database(tmp_path):
    cursor = SqliteCursor(tmp_path / "state.db")
    first_clock = Clock(utc("2026-10-06T08:59"))
    one = CronSource([job()], cursor=cursor, now=first_clock.now, sleep=first_clock.sleep)
    second_clock = Clock(utc("2026-10-06T08:59"))
    two = CronSource([job()], cursor=cursor, now=second_clock.now, sleep=second_clock.sleep)

    first = collect(one, 1)
    # The second host reaches 09:00, finds it claimed, and waits for tomorrow.
    second = collect(two, 1)

    assert first[0].message_id == "digest@2026-10-06T09:00:00+00:00"
    assert second[0].message_id == "digest@2026-10-07T09:00:00+00:00"


def test_a_firing_the_host_slept_through_is_skipped_by_default():
    clock = Clock(utc("2026-10-06T08:59"))
    source = CronSource([job()], now=clock.now, sleep=clock.sleep, late_grace=60.0)

    async def run():
        found = []
        async for message in source.messages():
            found.append(message)
            if len(found) >= 1:
                return found
            clock.moment += timedelta(hours=10)  # the container was frozen
        return found

    # Nothing from today's 09:00 slot; the first message is tomorrow's.
    clock.moment = utc("2026-10-06T09:30")  # already late when it starts
    [message] = asyncio.run(asyncio.wait_for(run(), 5))

    assert message.message_id == "digest@2026-10-07T09:00:00+00:00"


def test_a_job_that_would_rather_be_late_than_skipped_catches_up():
    clock = Clock(utc("2026-10-06T09:30"))  # a deploy spanned 09:00
    source = CronSource([job(catch_up=True)], now=clock.now, sleep=clock.sleep)

    [message] = collect(source, 1)

    assert message.message_id == "digest@2026-10-06T09:00:00+00:00"
    assert clock.slept == 0.0  # made up immediately, not waited for


def test_a_missed_firing_is_made_up_once_not_once_per_slot(tmp_path):
    cursor = SqliteCursor(tmp_path / "state.db")
    clock = Clock(utc("2026-10-06T09:30"))
    source = CronSource([job(catch_up=True)], cursor=cursor, now=clock.now, sleep=clock.sleep)
    collect(source, 1)

    # A restart ten minutes later does not make up the same 09:00 again: the
    # claim is what the next process reads.
    assert cursor.claim("digest", utc("2026-10-06T09:00").timestamp()) is False


def test_a_cursor_in_memory_forgets_when_the_process_does():
    cursor = MemoryCursor()

    assert cursor.claim("digest", 1.0) is True
    assert cursor.claim("digest", 1.0) is False
    assert MemoryCursor().claim("digest", 1.0) is True


# -- routing --------------------------------------------------------------


def test_a_job_carries_its_own_destination_and_profile():
    from simple_agent.profile import BUILT_IN

    report = job(to=(Destination("slack", "C_OPS"),), profile=BUILT_IN["slack"])
    quiet = job("quiet", "0 1 * * *")
    router = CronRouter(jobs=(report, quiet))
    clock = Clock(utc("2026-10-06T08:59"))
    [message] = collect(CronSource([report], now=clock.now, sleep=clock.sleep), 1)

    route = router.route(message)

    assert route is not None
    assert route.to == (Destination("slack", "C_OPS"),)
    assert route.profile is not None and route.profile.name == "slack"


def test_a_job_with_nowhere_to_answer_still_runs():
    """It does whatever it should do with tools; nobody is waiting for prose."""
    clock = Clock(utc("2026-10-06T08:59"))
    [message] = collect(CronSource([job()], now=clock.now, sleep=clock.sleep), 1)

    route = CronRouter(jobs=(job(),)).route(message)

    assert route is not None and route.to == ()
    assert route.profile is None  # the host's own profile: a job is trusted


def test_the_router_ignores_other_platforms_and_unknown_jobs():
    from simple_agent.slack.parse import PLATFORM as SLACK
    from simple_agent.seams import InboundMessage

    router = CronRouter(jobs=(job(),))

    assert router.route(InboundMessage(platform=SLACK, chat_id="C", user_id="U", text="hi")) is None
    assert router.route(
        InboundMessage(platform="cron", chat_id="x", user_id="cron:x", text="hi", chat_name="gone")
    ) is None


# -- jobs on disk ---------------------------------------------------------


@pytest.fixture
def config(tmp_path):
    cfg = Config(home=tmp_path)
    for directory in (cfg.memories_dir, cfg.skills_dir, cfg.shell_state_dir, cfg.schedules_dir,
                      cfg.profiles_dir):
        directory.mkdir(parents=True, exist_ok=True)
    return cfg


def write_job(config, name: str, text: str) -> None:
    (config.schedules_dir / f"{name}.md").write_text(text, encoding="utf-8")


def test_a_job_file_is_frontmatter_and_a_prompt(config):
    write_job(config, "deploys", """---
schedule: 0 9 * * 1-5
tz: Asia/Tokyo
to: slack:C0123, slack:C0999:1712345678.000100
catch_up: true
profile: slack
---
Summarize yesterday's failed deploys.
""")

    [loaded] = load_jobs(config)

    assert loaded.name == "deploys"
    assert loaded.prompt == "Summarize yesterday's failed deploys."
    assert loaded.catch_up is True and loaded.history is False
    assert loaded.profile is not None and loaded.profile.name == "slack"
    assert loaded.to == (
        Destination("slack", "C0123"),
        Destination("slack", "C0999", "1712345678.000100"),
    )
    assert loaded.schedule.next_after(utc("2026-10-03T01:00")) == utc("2026-10-05T00:00")


def test_a_broken_job_file_is_a_startup_error_not_a_silent_skip(config):
    write_job(config, "typo", "---\nschedule: 0 9 * * funday\n---\nGo.\n")

    with pytest.raises(ScheduleError):
        load_jobs(config)

    write_job(config, "typo", "---\nschedule: 0 9 * * *\n---\n")
    with pytest.raises(ScheduleError):  # a job with no prompt asks for nothing
        load_jobs(config)


def test_destinations_must_say_where():
    assert parse_destinations("") == ()
    with pytest.raises(ValueError):
        parse_destinations("slack")


# -- end to end, without a model ------------------------------------------


class Scripted(Provider):
    name = "scripted"

    def __init__(self, text="done"):
        self.text = text
        self.tools_seen = []

    def complete(self, *, system, messages, tools, max_tokens, model):
        self.tools_seen.append(sorted(tool["name"] for tool in tools))
        return Response(text=self.text, raw_content=[{"type": "text", "text": self.text}])


def test_a_schedule_becomes_an_answer_posted_where_the_job_said(config):
    from simple_agent.agent import Agent
    from simple_agent.host import Host
    from simple_agent.seams import Sink

    sent = []

    class Recorder(Sink):
        platform = "slack"

        async def send(self, to, text):
            sent.append((to, text))

    report = job(to=(Destination("slack", "C_OPS"),))
    clock = Clock(utc("2026-10-06T08:59"))
    [message] = collect(CronSource([report], now=clock.now, sleep=clock.sleep), 1)

    provider = Scripted("three deploys failed")
    host = Host(
        config,
        sources=[],
        router=CronRouter(jobs=(report,)),
        sinks=[Recorder()],
        agent_factory=lambda cfg, *, session_key, source, profile: Agent(
            cfg, source=source, session_key=session_key, profile=profile, provider=provider
        ),
    )

    assert asyncio.run(host.handle(message)) == "three deploys failed"
    assert sent == [(Destination("slack", "C_OPS"), "three deploys failed")]
    # A job is the operator's own instruction, so it keeps the host's toolset.
    assert "terminal" in provider.tools_seen[0]
