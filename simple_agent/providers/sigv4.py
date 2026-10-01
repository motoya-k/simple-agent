"""AWS Signature Version 4 and credential lookup — standard library only.

Enough of the AWS SDK's behaviour to call one JSON API with IAM credentials,
looked up in the SDK's order:

1. ``AWS_ACCESS_KEY_ID`` / ``AWS_SECRET_ACCESS_KEY`` (+ ``AWS_SESSION_TOKEN``)
2. the ECS / Fargate task role (``AWS_CONTAINER_CREDENTIALS_RELATIVE_URI`` or
   ``..._FULL_URI``) — what a container in production uses
3. a profile in ``~/.aws/credentials`` (``AWS_PROFILE``)
4. the EC2 instance role, via IMDSv2

Temporary credentials (2 and 4) expire; :class:`CredentialChain` refreshes
them five minutes before they do.  SSO and assume-role profiles are out of
scope; use a role attached to the task or instance instead.

https://docs.aws.amazon.com/IAM/latest/UserGuide/reference_sigv-create-signed-request.html
"""

from __future__ import annotations

import configparser
import datetime as _dt
import hashlib
import hmac
import json
import os
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

ECS_HOST = "http://169.254.170.2"
IMDS_HOST = "http://169.254.169.254"
REFRESH_MARGIN = 300.0  # seconds before expiry


@dataclass(frozen=True)
class Credentials:
    access_key: str
    secret_key: str
    session_token: str = ""
    expires_at: float | None = None  # epoch seconds; None for static keys


class CredentialChain:
    """Resolve credentials once, and again whenever temporary ones near expiry."""

    def __init__(self, profile: str | None = None) -> None:
        self.profile = profile
        self._current: Credentials | None = None
        self._lock = threading.Lock()

    def get(self) -> Credentials | None:
        with self._lock:
            current = self._current
            if current is None or (
                current.expires_at is not None and current.expires_at - time.time() < REFRESH_MARGIN
            ):
                self._current = resolve_credentials(self.profile)
            return self._current


def resolve_credentials(profile: str | None = None) -> Credentials | None:
    return (
        _from_env()
        or _from_container()
        or load_credentials(profile)
        or _from_instance()
    )


def _from_env() -> Credentials | None:
    access = os.environ.get("AWS_ACCESS_KEY_ID", "")
    secret = os.environ.get("AWS_SECRET_ACCESS_KEY", "")
    if access and secret:
        return Credentials(access, secret, os.environ.get("AWS_SESSION_TOKEN", ""))
    return None


def _from_container() -> Credentials | None:
    relative = os.environ.get("AWS_CONTAINER_CREDENTIALS_RELATIVE_URI")
    full = os.environ.get("AWS_CONTAINER_CREDENTIALS_FULL_URI")
    if not (relative or full):
        return None
    url = ECS_HOST + relative if relative else full
    headers = {}
    token = os.environ.get("AWS_CONTAINER_AUTHORIZATION_TOKEN", "")
    token_file = os.environ.get("AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE", "")
    if token_file:
        token = Path(token_file).read_text().strip()
    if token:
        headers["Authorization"] = token
    return _parse_temporary(_http("GET", url, headers, timeout=5))


def _from_instance() -> Credentials | None:
    if os.environ.get("AWS_EC2_METADATA_DISABLED", "").lower() == "true":
        return None
    try:
        token = _http("PUT", f"{IMDS_HOST}/latest/api/token",
                      {"X-aws-ec2-metadata-token-ttl-seconds": "21600"}, timeout=1)
        auth = {"X-aws-ec2-metadata-token": token}
        base = f"{IMDS_HOST}/latest/meta-data/iam/security-credentials/"
        role = _http("GET", base, auth, timeout=1).splitlines()[0].strip()
        return _parse_temporary(_http("GET", base + role, auth, timeout=1))
    except (OSError, IndexError, ValueError):
        return None  # not on EC2, or no role attached


def _parse_temporary(body: str) -> Credentials:
    data = json.loads(body)
    expiration = data.get("Expiration")
    expires_at = (
        _dt.datetime.fromisoformat(expiration.replace("Z", "+00:00")).timestamp()
        if expiration else None
    )
    return Credentials(data["AccessKeyId"], data["SecretAccessKey"], data.get("Token", ""), expires_at)


def _http(method: str, url: str, headers: dict[str, str], *, timeout: float) -> str:
    request = urllib.request.Request(url, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=timeout) as resp:
        return resp.read().decode("utf-8")


def load_credentials(profile: str | None = None) -> Credentials | None:
    """Environment keys, else the shared credentials file. No network."""
    env = _from_env()
    if env:
        return env

    profile = profile or os.environ.get("AWS_PROFILE") or "default"
    section = _read_ini(
        os.environ.get("AWS_SHARED_CREDENTIALS_FILE") or Path.home() / ".aws" / "credentials",
        profile,
    )
    if section.get("aws_access_key_id") and section.get("aws_secret_access_key"):
        return Credentials(
            section["aws_access_key_id"],
            section["aws_secret_access_key"],
            section.get("aws_session_token", ""),
        )
    return None


def profile_region(profile: str | None = None) -> str:
    """The ``region`` a profile sets in ``~/.aws/config``, or ""."""
    profile = profile or os.environ.get("AWS_PROFILE") or "default"
    name = profile if profile == "default" else f"profile {profile}"
    path = os.environ.get("AWS_CONFIG_FILE") or Path.home() / ".aws" / "config"
    return _read_ini(path, name).get("region", "")


def sign(
    *,
    method: str,
    url: str,
    headers: dict[str, str],
    body: bytes,
    credentials: Credentials,
    region: str,
    service: str,
    now: _dt.datetime | None = None,
) -> dict[str, str]:
    """Return ``headers`` plus the SigV4 ``authorization`` and its companions."""
    now = now or _dt.datetime.now(_dt.timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    day = amz_date[:8]
    parts = urllib.parse.urlsplit(url)

    signed = {k.lower(): v.strip() for k, v in headers.items()}
    signed["host"] = parts.netloc
    signed["x-amz-date"] = amz_date
    if credentials.session_token:
        signed["x-amz-security-token"] = credentials.session_token
    names = sorted(signed)

    # Every service but S3 signs the path URI-encoded a second time, so a
    # model id quoted once in the URL (":" -> "%3A") is signed as "%253A".
    canonical_uri = urllib.parse.quote(parts.path or "/", safe="/-_.~")
    canonical_query = "&".join(
        f"{urllib.parse.quote(k, safe='-_.~')}={urllib.parse.quote(v, safe='-_.~')}"
        for k, v in sorted(urllib.parse.parse_qsl(parts.query, keep_blank_values=True))
    )
    canonical_request = "\n".join(
        [
            method,
            canonical_uri,
            canonical_query,
            "".join(f"{n}:{signed[n]}\n" for n in names),
            ";".join(names),
            hashlib.sha256(body).hexdigest(),
        ]
    )
    scope = f"{day}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join(
        ["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical_request.encode()).hexdigest()]
    )

    key = _hmac(f"AWS4{credentials.secret_key}".encode(), day)
    for part in (region, service, "aws4_request"):
        key = _hmac(key, part)
    signature = hmac.new(key, string_to_sign.encode(), hashlib.sha256).hexdigest()

    result = dict(headers)
    result["x-amz-date"] = amz_date
    if credentials.session_token:
        result["x-amz-security-token"] = credentials.session_token
    result["authorization"] = (
        f"AWS4-HMAC-SHA256 Credential={credentials.access_key}/{scope}, "
        f"SignedHeaders={';'.join(names)}, Signature={signature}"
    )
    return result


def _hmac(key: bytes, text: str) -> bytes:
    return hmac.new(key, text.encode(), hashlib.sha256).digest()


def _read_ini(path: str | Path, section: str) -> dict[str, str]:
    parser = configparser.RawConfigParser()
    try:
        parser.read(path)
    except configparser.Error:
        return {}
    return dict(parser[section]) if parser.has_section(section) else {}
