"""Retries on transient API errors, and AWS credentials from the task role."""

from __future__ import annotations

import io
import json
import time
import urllib.error

import pytest

from simple_agent.providers import http
from simple_agent.providers.sigv4 import CredentialChain, resolve_credentials


def fake_urlopen(script):
    """Each call takes the next item: an exception to raise or a dict to return."""
    calls = []

    def urlopen(request, timeout):
        calls.append(dict(request.header_items()))
        item = script.pop(0)
        if isinstance(item, Exception):
            raise item
        return io.BytesIO(json.dumps(item).encode())

    return urlopen, calls


def http_error(code, retry_after=None):
    headers = {"retry-after": retry_after} if retry_after else {}
    return urllib.error.HTTPError("u", code, "x", headers, io.BytesIO(b'{"message":"busy"}'))


def test_retries_throttling_and_honors_retry_after(monkeypatch):
    urlopen, calls = fake_urlopen([http_error(429, "7"), http_error(503), {"ok": True}])
    monkeypatch.setattr(http.urllib.request, "urlopen", urlopen)
    slept = []
    signed = iter(["sig-1", "sig-2", "sig-3"])

    result = http.post_json("https://api.test/v1", b"{}", lambda: {"Authorization": next(signed)},
                            timeout=1, vendor="Bedrock", sleep=slept.append)

    assert result == {"ok": True}
    assert slept[0] == 7.0 and 0.5 <= slept[1] <= 2.0
    assert [c["Authorization"] for c in calls] == ["sig-1", "sig-2", "sig-3"]  # re-signed


def test_a_bad_request_is_not_retried(monkeypatch):
    urlopen, calls = fake_urlopen([http_error(400)])
    monkeypatch.setattr(http.urllib.request, "urlopen", urlopen)
    with pytest.raises(RuntimeError, match="Bedrock API error 400"):
        http.post_json("https://api.test/v1", b"{}", {}, timeout=1, vendor="Bedrock", sleep=lambda s: None)
    assert len(calls) == 1


def test_gives_up_after_the_last_attempt(monkeypatch):
    urlopen, calls = fake_urlopen([http_error(529)] * 3)
    monkeypatch.setattr(http.urllib.request, "urlopen", urlopen)
    with pytest.raises(RuntimeError, match="529"):
        http.post_json("https://api.test/v1", b"{}", {}, timeout=1, vendor="Anthropic", max_attempts=3, sleep=lambda s: None)
    assert len(calls) == 3


def test_connection_errors_are_retried_but_read_timeouts_are_not(monkeypatch):
    urlopen, _ = fake_urlopen([urllib.error.URLError(ConnectionRefusedError()), {"ok": 1}])
    monkeypatch.setattr(http.urllib.request, "urlopen", urlopen)
    assert http.post_json("https://api.test/v1", b"", {}, timeout=1, vendor="X", sleep=lambda s: None) == {"ok": 1}

    urlopen, calls = fake_urlopen([urllib.error.URLError(TimeoutError())])
    monkeypatch.setattr(http.urllib.request, "urlopen", urlopen)
    with pytest.raises(RuntimeError, match="unreachable"):
        http.post_json("https://api.test/v1", b"", {}, timeout=1, vendor="X", sleep=lambda s: None)
    assert len(calls) == 1


def test_task_role_credentials_and_refresh_before_expiry(monkeypatch):
    from simple_agent.providers import sigv4

    for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_PROFILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_CONTAINER_CREDENTIALS_RELATIVE_URI", "/v2/credentials/abc")
    issued = []

    def fake_http(method, url, headers, *, timeout):
        issued.append(url)
        expires = time.gmtime(time.time() + (60 if len(issued) == 1 else 3600))
        return json.dumps({
            "AccessKeyId": f"ASIA{len(issued)}", "SecretAccessKey": "s", "Token": "t",
            "Expiration": time.strftime("%Y-%m-%dT%H:%M:%SZ", expires),
        })

    monkeypatch.setattr(sigv4, "_http", fake_http)
    assert resolve_credentials().session_token == "t"
    assert issued == ["http://169.254.170.2/v2/credentials/abc"]

    issued.clear()
    chain = CredentialChain()
    first = chain.get()   # expires in 60s: inside the refresh margin
    second = chain.get()  # so this one is fetched again
    third = chain.get()   # valid for an hour: cached
    assert (first.access_key, second.access_key, third.access_key) == ("ASIA1", "ASIA2", "ASIA2")
    assert len(issued) == 2
