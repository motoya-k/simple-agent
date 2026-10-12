"""A Slack token that expires, and the refresh token that replaces it.

What these hold: a refresh token is spent exactly once however many processes
want a new token, the stored pair outlives the environment it was seeded from,
and a trade that fails never leaves the host with less than it had.
"""

from __future__ import annotations

import urllib.parse

from simple_agent.slack import SlackSource
from simple_agent.slack.api import SlackError
from simple_agent.slack.inbox import SqliteInbox
from simple_agent.slack.tokens import RotatingToken, SqliteIntegrations, open_app_token


class Slack:
    """oauth.v2.access: each refresh token works once and names the next."""

    def __init__(self, first="r0", expires_in=43200, fail=""):
        self.valid, self.expires_in, self.fail = first, expires_in, fail
        self.trades: list[dict] = []

    def __call__(self, url, body, headers, *, timeout, vendor):
        form = dict(urllib.parse.parse_qsl(body.decode()))
        self.trades.append(form)
        if self.fail:
            return {"ok": False, "error": self.fail}
        if form["refresh_token"] != self.valid:
            return {"ok": False, "error": "invalid_refresh_token"}
        n = len(self.trades)
        self.valid = f"r{n}"
        return {"ok": True, "access_token": f"a{n}", "refresh_token": self.valid,
                "expires_in": self.expires_in, "token_type": "app_level"}


class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now


def token(tmp_path, slack, clock, *, access="a0", refresh="r0"):
    return RotatingToken(
        client_id="cid", client_secret="secret", access_token=access, refresh_token=refresh,
        store=SqliteIntegrations(tmp_path / "state.db"), post=slack, clock=clock,
    )


def test_a_seed_is_traded_at_once_and_the_result_is_kept(tmp_path):
    slack, clock = Slack(), Clock()
    rotating = token(tmp_path, slack, clock)

    assert rotating.current() == "a1"
    assert rotating.current() == "a1"  # still good: no second trade
    assert slack.trades == [{
        "client_id": "cid", "client_secret": "secret",
        "grant_type": "refresh_token", "refresh_token": "r0",
    }]


def test_a_token_is_traded_again_shortly_before_it_expires(tmp_path):
    slack, clock = Slack(), Clock()
    rotating = token(tmp_path, slack, clock)
    rotating.current()

    clock.now += 43200 - 20 * 60
    assert rotating.current() == "a1"
    clock.now += 10 * 60  # inside the margin
    assert rotating.current() == "a2"
    assert [t["refresh_token"] for t in slack.trades] == ["r0", "r1"]


def test_a_restart_uses_the_stored_pair_not_the_dead_seed(tmp_path):
    slack, clock = Slack(), Clock()
    token(tmp_path, slack, clock).current()

    clock.now += 43200
    restarted = token(tmp_path, slack, clock)  # same environment as before

    assert restarted.current() == "a2"
    assert slack.trades[-1]["refresh_token"] == "r1"


def test_two_processes_make_one_trade(tmp_path):
    slack, clock = Slack(), Clock()
    one, two = token(tmp_path, slack, clock), token(tmp_path, slack, clock)

    assert one.current() == two.current() == "a1"
    assert len(slack.trades) == 1


def test_a_new_seed_replaces_what_was_stored(tmp_path):
    slack, clock = Slack(), Clock()
    token(tmp_path, slack, clock).current()

    slack.valid = "fresh"  # somebody made a new token in the app's settings
    reseeded = token(tmp_path, slack, clock, access="b0", refresh="fresh")

    reseeded.current()
    assert slack.trades[-1]["refresh_token"] == "fresh"


def test_a_trade_that_fails_keeps_the_token_in_hand(tmp_path):
    slack, clock = Slack(fail="invalid_client_id"), Clock()
    rotating = token(tmp_path, slack, clock)

    assert rotating.current() == "a0"

    slack.fail = ""  # and the next look tries again, with the same refresh token
    assert rotating.current() == "a2"  # the fake counts trades, failed ones too
    assert [t["refresh_token"] for t in slack.trades] == ["r0", "r0"]


def test_a_refused_token_is_traded_but_not_twice(tmp_path):
    slack, clock = Slack(), Clock()
    one, two = token(tmp_path, slack, clock), token(tmp_path, slack, clock)
    assert one.current() == two.current() == "a1"

    assert one.renew("a1") == "a2"  # revoked by hand, say
    assert two.renew("a1") == "a2"  # the other process finds the new one
    assert len(slack.trades) == 2


# -- the source -----------------------------------------------------------
class AppApi:
    """apps.connections.open, accepting only the token named ``good``."""

    def __init__(self, good):
        self.good, self.token, self.seen = good, "", []

    def call(self, method, payload=None, *, form=False, tolerate=()):
        self.seen.append(self.token)
        if self.token != self.good:
            raise SlackError(method, "token_expired")
        return {"ok": True, "url": "wss://x/link"}


def source(tmp_path, rotating, app_api):
    return SlackSource(
        app_token=rotating, bot_token="xoxb", inbox=SqliteInbox(tmp_path / "state.db"),
        app_api=app_api, connect=lambda url, **kw: url,
    )


def test_every_connect_asks_for_the_token_again(tmp_path):
    slack, clock = Slack(), Clock()
    app_api = AppApi("a1")
    slack_source = source(tmp_path, token(tmp_path, slack, clock), app_api)

    assert slack_source._open() == "wss://x/link"

    clock.now += 43200
    app_api.good = "a2"
    assert slack_source._open() == "wss://x/link"
    assert app_api.seen == ["a1", "a2"]


def test_a_token_that_expired_early_is_renewed_and_the_connect_retried(tmp_path):
    slack, clock = Slack(), Clock()
    app_api = AppApi("a2")  # a1 is already dead, whatever its row says
    slack_source = source(tmp_path, token(tmp_path, slack, clock), app_api)

    assert slack_source._open() == "wss://x/link"
    assert app_api.seen == ["a1", "a2"]


# -- the environment ------------------------------------------------------
class Cfg:
    database_url = ""

    def __init__(self, tmp_path):
        self.state_db = tmp_path / "state.db"


def test_without_a_refresh_token_the_token_is_used_as_given(tmp_path, monkeypatch):
    for name in ("APP_REFRESH_TOKEN", "CLIENT_ID", "CLIENT_SECRET"):
        monkeypatch.delenv(f"SIMPLE_AGENT_SLACK_{name}", raising=False)
    assert open_app_token(Cfg(tmp_path), "xapp-1") == "xapp-1"

    monkeypatch.setenv("SIMPLE_AGENT_SLACK_APP_REFRESH_TOKEN", "xoxe-1-r")
    assert open_app_token(Cfg(tmp_path), "xoxe.xapp-1") == "xoxe.xapp-1"  # half set

    monkeypatch.setenv("SIMPLE_AGENT_SLACK_CLIENT_ID", "1.2")
    monkeypatch.setenv("SIMPLE_AGENT_SLACK_CLIENT_SECRET", "s")
    assert isinstance(open_app_token(Cfg(tmp_path), "xoxe.xapp-1"), RotatingToken)


# -- postgres -------------------------------------------------------------
def test_postgres_keeps_the_pair_and_makes_one_trade():
    """Runs when ``SIMPLE_AGENT_TEST_DATABASE_URL`` is set; see test_state_contract."""
    import os
    import uuid

    import pytest

    url = os.environ.get("SIMPLE_AGENT_TEST_DATABASE_URL", "")
    if not url:
        pytest.skip("SIMPLE_AGENT_TEST_DATABASE_URL not set")
    from simple_agent.slack.tokens import PostgresIntegrations

    slack, clock, client_id = Slack(), Clock(), f"cid-{uuid.uuid4().hex}"

    def rotating():
        return RotatingToken(
            client_id=client_id, client_secret="secret", access_token="a0",
            refresh_token="r0", store=PostgresIntegrations(url), post=slack, clock=clock,
        )

    assert rotating().current() == rotating().current() == "a1"
    assert len(slack.trades) == 1
    clock.now += 43200
    assert rotating().current() == "a2"
