"""Slack as a Source and a Sink, over Socket Mode — outbound only, no public URL.

What a host needs from here::

    source = SlackSource(app_token=…, bot_token=…, inbox=open_inbox(config))
    sink = SlackSink(bot_token=…)
    router = SlackRouter(allow=("#ops", "dm"))

The pieces behind them: :mod:`.ws` speaks WebSocket, :mod:`.api` speaks the Web
API, :mod:`.parse` decides whether an event was a request at all, and
:mod:`.inbox` remembers which events have been handled.
"""

from .api import SlackApi, SlackError
from .inbox import Inbox, PostgresInbox, SqliteInbox, open_inbox
from .parse import PLATFORM, parse_event
from .router import SlackRouter
from .sink import SlackSink, to_mrkdwn
from .socket import SlackSource

__all__ = [
    "PLATFORM",
    "Inbox",
    "PostgresInbox",
    "SlackApi",
    "SlackError",
    "SlackRouter",
    "SlackSink",
    "SlackSource",
    "SqliteInbox",
    "open_inbox",
    "parse_event",
    "to_mrkdwn",
]
