"""Email as a Source: parse raw messages, poll a mailbox over IMAP."""

from .imap import ImapSource
from .ledger import Ledger, PostgresLedger, SqliteLedger, open_ledger
from .parse import parse_message

__all__ = [
    "ImapSource",
    "Ledger",
    "PostgresLedger",
    "SqliteLedger",
    "open_ledger",
    "parse_message",
]
