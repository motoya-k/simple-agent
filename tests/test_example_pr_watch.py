"""The ``examples/pr-watch`` deployment, which is four files and no code.

An example made of configuration rots exactly as fast as one made of code, and
more quietly: a schedule that no longer parses, a profile that quietly gained a
write tool, a skill with the team's own numbers baked into it. So the files are
loaded here with the same loaders the host uses.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from simple_agent.config import Config
from simple_agent.cron import CronRouter, CronSource, Job, MemoryCursor, load_jobs
from simple_agent.profile import load_profile
from simple_agent.skills import find_specifics, parse_frontmatter

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "pr-watch"


@pytest.fixture
def config(tmp_path):
    """A home with the example's files in it, as the README says to copy them."""
    for source in (EXAMPLE / "profiles").glob("*.md"):
        (tmp_path / "profiles").mkdir(exist_ok=True)
        (tmp_path / "profiles" / source.name).write_text(source.read_text("utf-8"))
    return Config(home=tmp_path)


@pytest.fixture
def job(config) -> Job:
    jobs = load_jobs(config, EXAMPLE / "schedules")
    assert len(jobs) == 1
    return jobs[0]


def test_the_schedule_is_working_hours_in_its_own_timezone(job):
    assert job.name == "pr-watch"
    tokyo = ZoneInfo("Asia/Tokyo")
    # Half past nine on a Monday morning in Tokyo, whatever the host thinks.
    monday = datetime(2026, 10, 5, 9, 15, tzinfo=tokyo)
    assert job.schedule.next_after(monday).astimezone(tokyo).hour == 9
    assert job.schedule.next_after(monday).astimezone(tokyo).minute == 30
    # Not at night, and not at the weekend.
    night = job.schedule.next_after(datetime(2026, 10, 5, 23, 0, tzinfo=tokyo))
    assert night.astimezone(tokyo).hour == 9
    saturday = job.schedule.next_after(datetime(2026, 10, 10, 10, 0, tzinfo=tokyo))
    assert saturday.astimezone(tokyo).weekday() == 0


def test_the_answer_has_somewhere_to_go_and_a_profile_to_run_under(job):
    assert [(place.platform, place.chat_id) for place in job.to] == [("slack", "C0123456789")]
    assert job.profile is not None and job.profile.name == "pr-watch"
    # Firings share a conversation, which is what stops it repeating itself.
    assert job.history is True


def test_the_prompt_names_the_skill_and_the_repositories(job):
    assert "pr-triage" in job.prompt
    assert job.prompt.count("/") >= 2  # owner/repo, at least twice
    assert "nothing at all" in job.prompt  # silence is the normal output


def test_the_route_a_firing_takes(job):
    source = CronSource([job], cursor=MemoryCursor())
    message = source._message(job, datetime(2026, 10, 5, 9, 30, tzinfo=ZoneInfo("Asia/Tokyo")))
    route = CronRouter((job,)).route(message)
    assert route is not None
    assert route.profile.name == "pr-watch"
    assert [place.chat_id for place in route.to] == ["C0123456789"]
    # The model is told when it is running, so it does not have to ask.
    assert "2026-10-05 09:30" in message.text


def test_the_profile_reads_and_writes_nothing(config):
    profile = load_profile(config, "pr-watch")
    assert profile.learning is False
    assert profile.tools is not None
    for writer in ("memory_save", "skill_manage"):
        assert writer not in profile.tools
    assert any(tool.startswith("github") for tool in profile.tools)
    assert "memory_search" in profile.tools  # it recalls the team's numbers


def test_the_skill_is_a_procedure_not_a_team_fact():
    meta, body = parse_frontmatter((EXAMPLE / "skills" / "pr-triage" / "SKILL.md").read_text())
    assert meta["name"] == "pr-triage" and meta["description"]
    # find_specifics is what the library warns a model with when a skill it
    # wrote has a URL, an address or an hour count baked into it.
    assert find_specifics(body) == []
    assert "long-term memory" in body


def test_the_github_server_is_read_only():
    import json

    servers = json.loads((EXAMPLE / "mcp.json").read_text())["mcpServers"]
    assert list(servers) == ["github"]
    github = servers["github"]
    assert github["env"]["GITHUB_READ_ONLY"] == "1"
    assert "pull_requests" in github["env"]["GITHUB_TOOLSETS"]
    # The token is passed by name, so it is never in the file.
    assert "GITHUB_PERSONAL_ACCESS_TOKEN" in github["args"]
    assert all("github_pat" not in str(value) for value in github["env"].values())
