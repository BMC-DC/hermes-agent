"""HMAC-SHA256-V2 request auth — a self-contained copy of
``plugins.platforms.event_post_pipeline.security``, same scheme
(hex(HMAC-SHA256(secret, "<unix-timestamp>." + raw_body)), replay-bound),
kept separate per that module's own convention: each pipeline plugin is a
deliberately independent listener with its own secret, not a shared import.
"""

from __future__ import annotations

import hmac
import os
import re
import time

REPLAY_WINDOW_SECONDS = 300
DEFAULT_REVIEW_CREATE_SECRET_PATH = "/opt/data/newsletter_review_create_secret.txt"

_ENV_VAR_PATTERN = re.compile(r"^\$\{([A-Za-z0-9_]+)\}$")


def resolve_env_secret(value: str) -> str:
    """Resolve a bare ``${VAR_NAME}`` config value from the process environment.

    A missing env var returns the literal placeholder (never a silent empty
    string) so a misconfigured secret fails loudly at signature-check time
    instead of accepting everything.
    """
    if not isinstance(value, str):
        return value
    m = _ENV_VAR_PATTERN.match(value.strip())
    if not m:
        return value
    return os.environ.get(m.group(1)) or value


def resolve_review_create_secret(extra: dict) -> str:
    """``extra.review_create_secret`` (``${VAR}``-resolved) if explicitly configured,
    else read straight from the well-known file — mirrors
    event_post_pipeline.security.resolve_review_create_secret."""
    explicit = extra.get("review_create_secret")
    if explicit:
        return resolve_env_secret(explicit)
    path = extra.get("review_create_secret_path", DEFAULT_REVIEW_CREATE_SECRET_PATH)
    try:
        return open(path, encoding="utf-8").read().strip()
    except OSError:
        return ""


def _hex_hmac(secret: str, data: bytes) -> str:
    import hashlib

    return hmac.new(secret.encode(), data, hashlib.sha256).hexdigest()


def _str_equal(provided: str, expected: str) -> bool:
    try:
        return hmac.compare_digest(provided.encode(), expected.encode())
    except Exception:
        return False


def verify_signature(*, secret: str, timestamp: str, signature: str, body: bytes) -> bool:
    """True when ``signature`` is a fresh, valid HMAC-V2 signature of ``body``."""
    if not secret or not timestamp or not signature:
        return False
    try:
        age = abs(int(time.time()) - int(timestamp))
    except (TypeError, ValueError):
        return False
    if age > REPLAY_WINDOW_SECONDS:
        return False
    expected = _hex_hmac(secret, timestamp.encode() + b"." + body)
    return _str_equal(signature, expected)
