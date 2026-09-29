"""The mail ledger contract, on every backend, and through ImapSource.

Postgres runs when ``SIMPLE_AGENT_TEST_DATABASE_URL`` is set; see
test_state_contract.py for how to start one.
"""

from __future__ import annotations

import asyncio
import os

import pytest

from simple_agent.mail import ImapSource, SqliteLedger

PG_URL = os.environ.get("SIMPLE_AGENT_TEST_DATABASE_URL", "")
BIG = 4_000_000_000  # a real UIDVALIDITY: above Postgres's INTEGER range


@pytest.fixture(params=["sqlite", "postgres"])
def open_ledger(request, tmp_path):
    """A factory, so a test can open the ledger twice — as a restart would."""
    if request.param == "sqlite":
        return lambda: SqliteLedger(tmp_path / "state.db")
    if not PG_URL:
        pytest.skip("SIMPLE_AGENT_TEST_DATABASE_URL not set")
    psycopg = pytest.importorskip("psycopg")
    with psycopg.connect(PG_URL, autocommit=True) as conn:
        conn.execute("DROP TABLE IF EXISTS mail_cursor, mail_seen")
    from simple_agent.mail import PostgresLedger

    return lambda: PostgresLedger(PG_URL)


def test_first_baseline_wins(open_ledger):
    ledger = open_ledger()
    assert ledger.baseline("inbox", BIG, 10) == 10
    assert ledger.baseline("inbox", BIG, 99) == 10  # a later poll cannot move it
    assert ledger.baseline("inbox", BIG + 1, 3) == 3  # renumbered folder: new baseline


def test_marks_survive_a_restart(open_ledger):
    ledger = open_ledger()
    ledger.mark("inbox", BIG, BIG)
    ledger.mark("inbox", BIG, BIG)  # idempotent
    again = open_ledger()
    assert again.seen("inbox", BIG, BIG)
    assert not again.seen("inbox", BIG, BIG - 1)
    assert not again.seen("other", BIG, BIG)


class FakeImap:
    """Just enough of imaplib.IMAP4 for ImapSource.poll."""

    def __init__(self, mailbox: dict[int, bytes]) -> None:
        self.mailbox = mailbox

    def __call__(self, host, port):
        return self

    def login(self, user, password):
        return "OK", []

    def select(self, mailbox, readonly=False):
        return "OK", []

    def response(self, name):
        return name, [b"7"]

    def uid(self, command, *args):
        if command == "SEARCH":
            return "OK", [" ".join(str(u) for u in sorted(self.mailbox)).encode()]
        uid = int(args[0])
        return "OK", [(b"1 (BODY[] {n}", self.mailbox[uid])]

    def logout(self):
        return "BYE", []


def _mail(n: int) -> bytes:
    return (
        f"From: a@example.com\r\nMessage-ID: <m{n}@example.com>\r\n"
        f"Subject: s{n}\r\n\r\nbody {n}\r\n"
    ).encode()


def test_imap_source_skips_backlog_and_handled_mail(open_ledger):
    server = FakeImap({1: _mail(1)})

    def source():
        return ImapSource(host="h", user="u", password="p", ledger=open_ledger(), connect=server)

    first = source()
    assert first.poll() == []  # existing mail is backlog

    server.mailbox[2] = _mail(2)
    [message] = first.poll()
    asyncio.run(first.ack(message))

    server.mailbox[3] = _mail(3)
    restarted = source()
    assert [m.message_id for m in restarted.poll()] == ["<m3@example.com>"]
