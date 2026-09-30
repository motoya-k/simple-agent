"""AWS Signature Version 4 and credential lookup — standard library only.

Enough of the AWS SDK's behaviour to call one JSON API with IAM credentials:
static keys from the environment or a shared-credentials profile, optionally
with a session token.  SSO and assume-role profiles are out of scope; refresh
those with the AWS CLI and export the resulting keys, or use a Bedrock API key.

https://docs.aws.amazon.com/IAM/latest/UserGuide/reference_sigv-create-signed-request.html
"""

from __future__ import annotations

import configparser
import datetime as _dt
import hashlib
import hmac
import os
import urllib.parse
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Credentials:
    access_key: str
    secret_key: str
    session_token: str = ""


def load_credentials(profile: str | None = None) -> Credentials | None:
    """Environment first, then the shared credentials file — the SDK's order."""
    access = os.environ.get("AWS_ACCESS_KEY_ID", "")
    secret = os.environ.get("AWS_SECRET_ACCESS_KEY", "")
    if access and secret:
        return Credentials(access, secret, os.environ.get("AWS_SESSION_TOKEN", ""))

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
