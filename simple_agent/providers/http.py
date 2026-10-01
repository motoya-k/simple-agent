"""One JSON POST with retries — the only network call the providers make.

Model APIs fail transiently all the time: throttling (429), overload (529),
a 5xx from a busy region, a dropped connection.  An unattended agent that
gives up on the first one drops a whole conversation turn, so every provider
goes through here.

Retried: 408, 409, 425, 429, 500, 502, 503, 504, 529, and connection errors.
Not retried: other 4xx (a bad request stays bad) and read timeouts (the call
may have run for minutes; repeating it doubles the cost and rarely helps).
Backoff is exponential with jitter, and a ``Retry-After`` header wins.

``headers`` may be a callable so a signed request (SigV4 carries a timestamp)
is signed again on every attempt.
"""

from __future__ import annotations

import json
import logging
import os
import random
import time
import urllib.error
import urllib.request
from typing import Any, Callable

log = logging.getLogger(__name__)

RETRY_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504, 529})
MAX_DELAY = 30.0
MAX_RETRY_AFTER = 120.0


def attempts() -> int:
    try:
        return max(1, int(os.environ.get("SIMPLE_AGENT_HTTP_ATTEMPTS", "5")))
    except ValueError:
        return 5


def post_json(
    url: str,
    payload: bytes,
    headers: dict[str, str] | Callable[[], dict[str, str]],
    *,
    timeout: float,
    vendor: str,
    max_attempts: int | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    total = max_attempts or attempts()
    for attempt in range(1, total + 1):
        request = urllib.request.Request(
            url,
            data=payload,
            headers=headers() if callable(headers) else headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:2000]
            if exc.code not in RETRY_STATUS or attempt == total:
                raise RuntimeError(f"{vendor} API error {exc.code}: {detail}") from exc
            delay = _retry_after(exc.headers.get("retry-after")) or _backoff(attempt)
            log.warning("%s API %s, retry %d/%d in %.1fs", vendor, exc.code, attempt, total - 1, delay)
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, TimeoutError) or attempt == total:
                raise RuntimeError(f"{vendor} API unreachable: {exc.reason}") from exc
            delay = _backoff(attempt)
            log.warning("%s API unreachable (%s), retry %d/%d in %.1fs",
                        vendor, exc.reason, attempt, total - 1, delay)
        except ConnectionError as exc:
            if attempt == total:
                raise RuntimeError(f"{vendor} API connection failed: {exc}") from exc
            delay = _backoff(attempt)
            log.warning("%s API connection failed (%s), retry %d/%d in %.1fs",
                        vendor, exc, attempt, total - 1, delay)
        sleep(delay)
    raise AssertionError("unreachable")  # pragma: no cover


def _backoff(attempt: int) -> float:
    return min(MAX_DELAY, 2.0 ** (attempt - 1)) * random.uniform(0.5, 1.0)


def _retry_after(value: str | None) -> float | None:
    try:
        return min(MAX_RETRY_AFTER, max(0.0, float(value))) if value else None
    except ValueError:
        return None  # an HTTP date; the computed backoff is close enough
