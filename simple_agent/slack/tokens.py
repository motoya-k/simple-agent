"""A Slack token that expires, kept alive with its refresh token.

An app with token rotation turned on — and it cannot be turned off again — is
issued access tokens that last twelve hours (``xoxe.xapp-…``, ``xoxe.xoxb-…``)
and a refresh token to trade for the next one.  A host that was handed such a
token in its environment works until the first reconnect after it expires, and
then never again: ``apps.connections.open`` answers ``token_expired`` forever.

So the token in the environment is only the *seed*.  The one in use lives in
the ``slack_integration`` table, beside the transcripts, and is traded for a
new one when it is about to expire.

Three things that decide the shape of this:

* **A refresh token is spent by using it.**  The answer carries the next one,
  and the old one stops working.  Two processes that both trade the same
  refresh token — two tasks during a deploy — would leave one of them holding
  a dead pair, so the trade happens inside the store's lock, and the second
  process finds the first one's result instead of making its own.
* **The seed is not trusted after the first trade.**  The environment still
  holds the token it was started with, long after that token is dead.  The
  stored row wins, unless the seed itself has changed — which is somebody
  putting a new token in the environment because the stored pair was lost.
* **A trade that fails costs nothing.**  The token in hand is returned as it
  is; it may still work, and if it does not, the connection's own retry asks
  again.
"""

from __future__ import annotations

import hashlib
import logging
import os
import sqlite3
import threading
import time
import urllib.parse
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Protocol

from ..providers.http import post_json
from .api import BASE_URL, TIMEOUT, SlackError

log = logging.getLogger(__name__)

#: Trade a token this long before it expires.  A token is only looked at when a
#: connection is opened, so this is the least life a token in use has left.
MARGIN_SECONDS = 15 * 60

#: Slack's answers that mean "this token is over", not "this call was wrong".
EXPIRED = frozenset({"token_expired", "invalid_auth", "token_revoked"})

_SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS slack_integration (
    app TEXT PRIMARY KEY,
    access_token TEXT NOT NULL,
    refresh_token TEXT NOT NULL,
    expires_at REAL NOT NULL,
    seed TEXT NOT NULL,
    updated_at REAL NOT NULL
);
"""

_POSTGRES_SCHEMA = """
CREATE TABLE IF NOT EXISTS slack_integration (
    app TEXT PRIMARY KEY,
    access_token TEXT NOT NULL,
    refresh_token TEXT NOT NULL,
    expires_at DOUBLE PRECISION NOT NULL,
    seed TEXT NOT NULL,
    updated_at DOUBLE PRECISION NOT NULL
);
"""

_SCHEMA_LOCK = 0x3A14
# The first half of the two-key advisory lock one app's trade is made under.
_TRADE_LOCK = 0x3A15


@dataclass(frozen=True)
class Grant:
    """One access token and the refresh token that replaces it."""

    access_token: str
    refresh_token: str
    #: Unix time; 0 when nobody knows (a seed says nothing about its age).
    expires_at: float = 0.0
    #: Fingerprint of the seed this grant descends from.
    seed: str = ""


Step = Callable[["Grant | None"], "Grant | None"]


class Integrations(Protocol):
    def exchange(self, app: str, step: Step) -> Grant | None:
        """Run ``step`` on the stored grant, alone, and store what it returns.

        ``step`` gets the stored grant (or ``None``) and returns the one to
        keep.  Nobody else's ``exchange`` for this app runs until it is done —
        in this process or another.
        """
        ...


def open_integrations(config: Any) -> Integrations:
    """The table that lives where this config keeps its transcripts."""
    if config.database_url:
        return PostgresIntegrations(config.database_url)
    return SqliteIntegrations(config.state_db)


class SqliteIntegrations:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        # isolation_level=None: transactions are opened by hand, so the write
        # lock is taken before the row is read rather than at the first write.
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.executescript(_SQLITE_SCHEMA)
        self._lock = threading.Lock()

    def exchange(self, app: str, step: Step) -> Grant | None:
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                row = self._db.execute(
                    "SELECT access_token, refresh_token, expires_at, seed "
                    "FROM slack_integration WHERE app = ?",
                    (app,),
                ).fetchone()
                stored = Grant(*row) if row else None
                kept = step(stored)
                if kept is not None and kept != stored:
                    self._db.execute(
                        "INSERT INTO slack_integration "
                        "(app, access_token, refresh_token, expires_at, seed, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (app) DO UPDATE SET "
                        "access_token = excluded.access_token, "
                        "refresh_token = excluded.refresh_token, "
                        "expires_at = excluded.expires_at, seed = excluded.seed, "
                        "updated_at = excluded.updated_at",
                        (app, kept.access_token, kept.refresh_token, kept.expires_at,
                         kept.seed, time.time()),
                    )
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
        return kept


class PostgresIntegrations:
    def __init__(self, url: str) -> None:
        import psycopg  # optional dependency: pip install 'simple-agent[postgres]'

        self._url = url
        self._psycopg = psycopg
        self._lock = threading.Lock()
        self._conn = self._connect()
        with self._lock, self._conn.transaction():
            self._conn.execute("SELECT pg_advisory_xact_lock(%s)", (_SCHEMA_LOCK,))
            self._conn.execute(_POSTGRES_SCHEMA)

    def _connect(self):
        return self._psycopg.connect(self._url, autocommit=True)

    def exchange(self, app: str, step: Step) -> Grant | None:
        with self._lock:
            if self._conn.closed:
                self._conn = self._connect()
            with self._conn.transaction():
                # An advisory lock and not FOR UPDATE: the first trade has no
                # row to lock yet, and that is the one two tasks race for.
                self._conn.execute(
                    "SELECT pg_advisory_xact_lock(%s, hashtext(%s))", (_TRADE_LOCK, app)
                )
                row = self._conn.execute(
                    "SELECT access_token, refresh_token, expires_at, seed "
                    "FROM slack_integration WHERE app = %s",
                    (app,),
                ).fetchone()
                stored = Grant(*row) if row else None
                kept = step(stored)
                if kept is not None and kept != stored:
                    self._conn.execute(
                        "INSERT INTO slack_integration "
                        "(app, access_token, refresh_token, expires_at, seed, updated_at) "
                        "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (app) DO UPDATE SET "
                        "access_token = EXCLUDED.access_token, "
                        "refresh_token = EXCLUDED.refresh_token, "
                        "expires_at = EXCLUDED.expires_at, seed = EXCLUDED.seed, "
                        "updated_at = EXCLUDED.updated_at",
                        (app, kept.access_token, kept.refresh_token, kept.expires_at,
                         kept.seed, time.time()),
                    )
        return kept


class RotatingToken:
    """An access token that is traded for the next one before it expires.

    ``access_token`` and ``refresh_token`` are the seed: what the app's settings
    page showed when the token was made.  ``name`` tells one app's tokens from
    another's in the table (an app has an app-level token and a bot token, and
    each has its own refresh token).
    """

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        access_token: str,
        refresh_token: str,
        store: Integrations,
        name: str = "app",
        margin: float = MARGIN_SECONDS,
        post=post_json,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.client_id = client_id
        self._client_secret = client_secret
        self._store = store
        self._key = f"{client_id}/{name}"
        self._margin = margin
        self._post = post
        self._clock = clock
        fingerprint = hashlib.sha256(refresh_token.encode("utf-8")).hexdigest()[:16]
        self._seed = Grant(access_token, refresh_token, 0.0, fingerprint)
        self._held: Grant | None = None
        self._lock = threading.Lock()

    def current(self) -> str:
        """A token with at least ``margin`` of life left, as far as anyone knows."""
        with self._lock:
            if self._held is None or not self._fresh(self._held):
                self._held = self._store.exchange(self._key, self._step)
            return (self._held or self._seed).access_token

    def renew(self, rejected: str) -> str:
        """Slack refused ``rejected``. A token that is not that one."""
        with self._lock:
            self._held = self._store.exchange(
                self._key, lambda stored: self._step(stored, rejected=rejected)
            )
            return (self._held or self._seed).access_token

    def _fresh(self, grant: Grant) -> bool:
        return grant.expires_at - self._clock() > self._margin

    def _step(self, stored: Grant | None, *, rejected: str = "") -> Grant:
        # A new seed in the environment replaces whatever was stored.
        grant = stored if stored and stored.seed == self._seed.seed else self._seed
        if grant.access_token != rejected and self._fresh(grant):
            return grant  # somebody else already traded it
        try:
            return self._trade(grant)
        except Exception as exc:  # network, a spent refresh token, wrong secret
            log.warning("slack token %s could not be refreshed (%s)", self._key, exc)
            return grant

    def _trade(self, grant: Grant) -> Grant:
        body = urllib.parse.urlencode({
            "client_id": self.client_id,
            "client_secret": self._client_secret,
            "grant_type": "refresh_token",
            "refresh_token": grant.refresh_token,
        }).encode("utf-8")
        answer = self._post(
            BASE_URL + "oauth.v2.access",
            body,
            {"Content-Type": "application/x-www-form-urlencoded"},
            timeout=TIMEOUT,
            vendor="slack",
        )
        if not answer.get("ok"):
            raise SlackError("oauth.v2.access", str(answer.get("error", "unknown")))
        access, refresh = answer.get("access_token"), answer.get("refresh_token")
        if not (access and refresh):
            raise SlackError("oauth.v2.access", "answer has no token pair")
        expires_at = self._clock() + float(answer.get("expires_in") or 0)
        log.info("slack token %s refreshed, good for %.1fh",
                 self._key, float(answer.get("expires_in") or 0) / 3600)
        return replace(grant, access_token=str(access), refresh_token=str(refresh),
                       expires_at=expires_at)


def open_app_token(config: Any, app_token: str) -> "str | RotatingToken":
    """``app_token`` as it is, or kept alive if the environment says how.

    Rotation needs all three of ``SIMPLE_AGENT_SLACK_APP_REFRESH_TOKEN``,
    ``SIMPLE_AGENT_SLACK_CLIENT_ID`` and ``SIMPLE_AGENT_SLACK_CLIENT_SECRET``.
    With none of them the token is used as given, which is right for an app
    without token rotation.
    """
    refresh = os.environ.get("SIMPLE_AGENT_SLACK_APP_REFRESH_TOKEN", "")
    client_id = os.environ.get("SIMPLE_AGENT_SLACK_CLIENT_ID", "")
    client_secret = os.environ.get("SIMPLE_AGENT_SLACK_CLIENT_SECRET", "")
    if not (refresh and client_id and client_secret):
        if refresh or app_token.startswith("xoxe."):
            log.warning(
                "slack: the app-level token expires, and it will not be refreshed: "
                "set SIMPLE_AGENT_SLACK_APP_REFRESH_TOKEN, SIMPLE_AGENT_SLACK_CLIENT_ID "
                "and SIMPLE_AGENT_SLACK_CLIENT_SECRET"
            )
        return app_token
    return RotatingToken(
        client_id=client_id,
        client_secret=client_secret,
        access_token=app_token,
        refresh_token=refresh,
        store=open_integrations(config),
    )


__all__ = [
    "EXPIRED",
    "Grant",
    "Integrations",
    "PostgresIntegrations",
    "RotatingToken",
    "SqliteIntegrations",
    "open_app_token",
    "open_integrations",
]
