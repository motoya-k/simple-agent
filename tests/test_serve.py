"""The CLI's host wiring: which transports a flag adds, and what is missing."""

from __future__ import annotations

import pytest

from simple_agent.cli import serve
from simple_agent.config import Config
from simple_agent.host import FirstMatch


@pytest.fixture
def config(tmp_path, monkeypatch):
    cfg = Config(home=tmp_path)
    for directory in (cfg.profiles_dir, cfg.schedules_dir, cfg.memories_dir, cfg.skills_dir):
        directory.mkdir(parents=True, exist_ok=True)
    for key in ("SLACK_APP_TOKEN", "SLACK_BOT_TOKEN", "SLACK_ALLOW", "SLACK_REQUIRE_MENTION"):
        monkeypatch.delenv(f"SIMPLE_AGENT_{key}", raising=False)
    return cfg


@pytest.fixture
def host(monkeypatch):
    """Capture what the CLI hands to Host instead of running it."""
    built = {}

    class FakeHost:
        def __init__(self, cfg, *, sources, router, sinks=()):
            built.update(config=cfg, sources=list(sources), router=router, sinks=list(sinks))

        async def serve(self):
            return None

    monkeypatch.setattr("simple_agent.host.Host", FakeHost)
    return built


def test_slack_and_cron_are_one_process(config, host, monkeypatch):
    (config.schedules_dir / "digest.md").write_text(
        "---\nschedule: 0 9 * * 1-5\ntz: Asia/Tokyo\nto: slack:C_OPS\n---\nSummarize.\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SIMPLE_AGENT_SLACK_APP_TOKEN", "xapp-1")
    monkeypatch.setenv("SIMPLE_AGENT_SLACK_BOT_TOKEN", "xoxb-1")
    monkeypatch.setenv("SIMPLE_AGENT_SLACK_ALLOW", "#ops,dm")

    assert serve(config, ("slack", "cron")) == 0

    assert [source.platform for source in host["sources"]] == ["slack", "cron"]
    # One sink for Slack, however many transports asked for it.
    assert {sink.platform for sink in host["sinks"]} == {"slack"}
    assert isinstance(host["router"], FirstMatch)
    assert [type(r).__name__ for r in host["router"].routers] == ["SlackRouter", "CronRouter"]


def test_a_schedule_that_reports_into_slack_gets_the_slack_sink(config, host, monkeypatch):
    """Otherwise its answer lands in dead letters and the reason is a puzzle."""
    (config.schedules_dir / "digest.md").write_text(
        "---\nschedule: @daily\nto: slack:C_OPS\n---\nSummarize.\n", encoding="utf-8"
    )
    monkeypatch.setenv("SIMPLE_AGENT_SLACK_BOT_TOKEN", "xoxb-1")

    assert serve(config, ("cron",)) == 0

    assert [sink.platform for sink in host["sinks"]] == ["slack"]


def test_slack_without_its_tokens_says_so_and_starts_nothing(config, host, capsys):
    assert serve(config, ("slack",)) == 1

    message = capsys.readouterr().err
    assert "SIMPLE_AGENT_SLACK_APP_TOKEN" in message
    assert "SIMPLE_AGENT_SLACK_BOT_TOKEN" in message
    assert "slack_allow" in message
    assert host == {}


def test_cron_with_nothing_to_run_says_where_jobs_live(config, host, capsys):
    assert serve(config, ("cron",)) == 1

    assert str(config.schedules_dir) in capsys.readouterr().err


def test_a_broken_schedule_is_a_clean_error_not_a_traceback(config, host, capsys):
    (config.schedules_dir / "typo.md").write_text(
        "---\nschedule: 0 9 * * funday\n---\nGo.\n", encoding="utf-8"
    )

    assert serve(config, ("cron",)) == 1

    assert "funday" in capsys.readouterr().err
