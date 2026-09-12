"""Tests for the event-post-pipeline plugin's HMAC-V2 request auth
(matches the scheme social-post-portal's clients already sign with)."""

from __future__ import annotations

import hashlib
import hmac
import time

from plugins.platforms.event_post_pipeline import security


def _sign(secret: str, timestamp: str, body: bytes) -> str:
    return hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()


def test_valid_signature_accepted():
    secret, body = "s3cret", b'{"hello": "world"}'
    timestamp = str(int(time.time()))
    assert security.verify_signature(secret=secret, timestamp=timestamp, signature=_sign(secret, timestamp, body), body=body)


def test_wrong_secret_rejected():
    body = b'{"hello": "world"}'
    timestamp = str(int(time.time()))
    assert not security.verify_signature(secret="s3cret", timestamp=timestamp, signature=_sign("wrong", timestamp, body), body=body)


def test_stale_timestamp_rejected():
    secret, body = "s3cret", b'{"hello": "world"}'
    stale_timestamp = str(int(time.time()) - security.REPLAY_WINDOW_SECONDS - 60)
    signature = _sign(secret, stale_timestamp, body)
    assert not security.verify_signature(secret=secret, timestamp=stale_timestamp, signature=signature, body=body)


def test_tampered_body_rejected():
    secret = "s3cret"
    timestamp = str(int(time.time()))
    signature = _sign(secret, timestamp, b'{"hello": "world"}')
    assert not security.verify_signature(secret=secret, timestamp=timestamp, signature=signature, body=b'{"hello": "mallory"}')


def test_missing_signature_rejected():
    assert not security.verify_signature(secret="s3cret", timestamp=str(int(time.time())), signature="", body=b"{}")


def test_resolve_env_secret(monkeypatch):
    monkeypatch.setenv("MY_TEST_SECRET", "the-real-secret")
    assert security.resolve_env_secret("${MY_TEST_SECRET}") == "the-real-secret"
    # Missing env var: literal placeholder, never a silent empty string.
    monkeypatch.delenv("SOME_UNSET_VAR", raising=False)
    assert security.resolve_env_secret("${SOME_UNSET_VAR}") == "${SOME_UNSET_VAR}"
    # Not a ${VAR} pattern at all: passed through unchanged.
    assert security.resolve_env_secret("plain-literal") == "plain-literal"


def test_resolve_review_create_secret_prefers_explicit_config(monkeypatch):
    monkeypatch.setenv("MY_REVIEW_SECRET", "from-env")
    assert security.resolve_review_create_secret({"review_create_secret": "${MY_REVIEW_SECRET}"}) == "from-env"


def test_resolve_review_create_secret_falls_back_to_the_shared_file(tmp_path):
    secret_file = tmp_path / "event_review_create_secret.txt"
    secret_file.write_text("from-shared-file\n", encoding="utf-8")
    assert security.resolve_review_create_secret({"review_create_secret_path": str(secret_file)}) == "from-shared-file"


def test_resolve_review_create_secret_missing_file_returns_empty(tmp_path):
    assert security.resolve_review_create_secret({"review_create_secret_path": str(tmp_path / "nope.txt")}) == ""
