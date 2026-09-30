"""SigV4 against AWS's published test vector, and credential lookup order."""

from __future__ import annotations

import datetime as dt

from simple_agent.providers.sigv4 import Credentials, load_credentials, profile_region, sign

EXAMPLE = Credentials("AKIDEXAMPLE", "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY")
WHEN = dt.datetime(2015, 8, 30, 12, 36, 0, tzinfo=dt.timezone.utc)


def test_matches_the_aws_get_vanilla_vector():
    headers = sign(
        method="GET", url="https://example.amazonaws.com/", headers={}, body=b"",
        credentials=EXAMPLE, region="us-east-1", service="service", now=WHEN,
    )
    assert headers["authorization"] == (
        "AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/20150830/us-east-1/service/aws4_request, "
        "SignedHeaders=host;x-amz-date, "
        "Signature=5fa00fa31553b73ebf1942676e86291e8372ff2a2260956d9b8aae1d763fbf31"
    )


def test_session_token_is_sent_and_signed():
    headers = sign(
        method="POST", url="https://bedrock-runtime.ap-northeast-1.amazonaws.com/model/a%3Ab/converse",
        headers={"content-type": "application/json"}, body=b"{}",
        credentials=Credentials("AK", "SK", "TOKEN"), region="ap-northeast-1", service="bedrock", now=WHEN,
    )
    assert headers["x-amz-security-token"] == "TOKEN"
    assert "SignedHeaders=content-type;host;x-amz-date;x-amz-security-token," in headers["authorization"]


def test_environment_keys_win_over_the_profile(tmp_path, monkeypatch):
    (tmp_path / "credentials").write_text("[work]\naws_access_key_id = FILE\naws_secret_access_key = S\n")
    (tmp_path / "config").write_text("[profile work]\nregion = eu-west-1\n")
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "credentials"))
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "config"))
    monkeypatch.setenv("AWS_PROFILE", "work")
    monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("AWS_SECRET_ACCESS_KEY", raising=False)

    assert load_credentials().access_key == "FILE"
    assert profile_region() == "eu-west-1"

    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "ENV")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "S")
    assert load_credentials().access_key == "ENV"
