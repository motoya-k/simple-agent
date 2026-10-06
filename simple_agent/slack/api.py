"""Slack Web API: one method, one POST, through the providers' retrying client.

Slack answers an application error with HTTP 200 and ``{"ok": false, "error":
"…"}``, so a caller that only checks the status code sees every failure as a
success.  That check lives here, once, and nowhere else.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Iterable

from ..providers.http import post_json

log = logging.getLogger(__name__)

BASE_URL = "https://slack.com/api/"
TIMEOUT = 30.0


class SlackError(RuntimeError):
    """An ``ok: false`` answer. ``error`` is Slack's own code, worth matching on."""

    def __init__(self, method: str, error: str, detail: str = "") -> None:
        super().__init__(f"slack {method} failed: {error}{f' ({detail})' if detail else ''}")
        self.method = method
        self.error = error


class SlackApi:
    """One token's worth of API access.

    A Socket Mode host holds two: the app-level token (``xapp-``) opens the
    connection, the bot token (``xoxb-``) does everything else.  They are
    separate objects rather than one with two tokens, so a call cannot
    accidentally reach for the wrong one.

    Rate limits, 5xx and dropped connections are handled by
    :func:`simple_agent.providers.http.post_json`, which honours
    ``Retry-After``.
    """

    def __init__(self, token: str, *, timeout: float = TIMEOUT, post=post_json) -> None:
        self.token = token
        self.timeout = timeout
        self._post = post

    def call(
        self,
        method: str,
        payload: dict[str, Any] | None = None,
        *,
        form: bool = False,
        tolerate: Iterable[str] = (),
    ) -> dict[str, Any]:
        """Call ``method``; raise :class:`SlackError` unless ``ok``.

        ``tolerate`` names errors the caller treats as a non-event — adding a
        reaction that is already there, removing one that is gone.  They are
        returned instead of raised.

        ``form`` sends an empty urlencoded body, which is what the few methods
        that take no arguments at all expect (``apps.connections.open``).
        """
        if form:
            body = b""
            content_type = "application/x-www-form-urlencoded"
        else:
            body = json.dumps(payload or {}).encode("utf-8")
            content_type = "application/json; charset=utf-8"
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": content_type,
        }
        answer = self._post(
            BASE_URL + method, body, headers, timeout=self.timeout, vendor="slack"
        )
        if not answer.get("ok"):
            error = str(answer.get("error", "unknown"))
            if error in tuple(tolerate):
                log.debug("slack %s: %s (tolerated)", method, error)
                return answer
            raise SlackError(method, error, str(answer.get("needed", "")))
        return answer
