"""Client for the host-side site service (``bmcsitegraph serve``, 127.0.0.1:8650): closed link list + context pack before a
blog draft, link verification after. Task 07 / W03.

**Fail open, always.** Every public method returns a ``SiteResult`` and never raises. Any problem (flag off, service down,
timeout, redirect, non-200, bad JSON, schema mismatch, oversize reply) is ``available=False`` with a short ``reason``; the caller
carries on with "no links, no brief" exactly as before this client existed. A failed *verification* is reported as
``passed=None`` ("links NOT verified"), never as ``True``.

**Flag-gated, OFF by default.** ``platforms.event_post_pipeline.extra.site_service.enabled`` must be true; with it unset or false
no socket is ever opened. This module is not imported by ``pipeline.py`` yet.

Safety: stdlib ``urllib`` only (matches ``review_client.py``), loopback URLs only (submission text is sensitive; a non-loopback
``url`` disables the client), no proxies, redirects are not followed, replies are size-capped, request bodies are never logged
and errors carry only the exception class. Sync: call from the plugin's worker thread, never the gateway event loop.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

SCHEMA = 1
DEFAULT_URL = "http://127.0.0.1:8650"
DEFAULT_TIMEOUT_S = 3.0
HEALTH_TIMEOUT_S = 1.0
HEALTH_CACHE_S = 60.0
MAX_RESPONSE_BYTES = 256 * 1024          # the service caps itself at 64 KB; this is the client-side belt
MAX_REQUEST_BYTES = 60 * 1024            # service limit is 64 KB; refuse locally instead of sending a doomed request
DEFAULT_STALE_AFTER_DAYS = 14


@dataclass(frozen=True)
class SiteClientConfig:
    enabled: bool = False
    base_url: str = DEFAULT_URL
    timeout_s: float = DEFAULT_TIMEOUT_S
    stale_after_days: int = DEFAULT_STALE_AFTER_DAYS

    @classmethod
    def from_extra(cls, extra: Optional[dict[str, Any]]) -> "SiteClientConfig":
        """From ``platforms.event_post_pipeline.extra`` (``load_pipeline_extra()``). Missing/invalid values fall back to the safe
        defaults; ``enabled`` must be the literal boolean ``True``."""
        raw = (extra or {}).get("site_service")
        if not isinstance(raw, dict):
            return cls()
        timeout = raw.get("timeout_s", DEFAULT_TIMEOUT_S)
        stale = raw.get("stale_after_days", DEFAULT_STALE_AFTER_DAYS)
        url = raw.get("url", DEFAULT_URL)
        return cls(
            enabled=raw.get("enabled") is True,
            base_url=url.rstrip("/") if isinstance(url, str) and url else DEFAULT_URL,
            timeout_s=float(timeout) if isinstance(timeout, (int, float)) and not isinstance(timeout, bool) and 0 < timeout <= 10 else DEFAULT_TIMEOUT_S,
            stale_after_days=int(stale) if isinstance(stale, int) and not isinstance(stale, bool) and stale > 0 else DEFAULT_STALE_AFTER_DAYS,
        )


@dataclass
class SiteResult:
    """``available``: the service answered correctly. ``passed``: for verification, True/False when available, None when the
    check could not run (treat as "links NOT verified")."""

    available: bool
    reason: str = ""                      # why it is unavailable ("disabled", "timeout", "http 500", ...) or the 400 message
    text: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    passed: Optional[bool] = None
    corpus_hash: str = ""
    stale: bool = False                   # index older than stale_after_days (health only)
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def ready_to_draft(self) -> bool:
        """Context-pack gate. Unavailable => True (fail open: never block a post because the service is down)."""
        if not self.available:
            return True
        return bool(((self.data or {}).get("missing_info") or {}).get("ready_to_draft", True))

    @property
    def candidate_ids(self) -> list[str]:
        return [c["page_id"] for c in (self.data or {}).get("candidates", []) if isinstance(c, dict) and c.get("page_id")]


def _unavailable(reason: str) -> SiteResult:
    return SiteResult(available=False, reason=reason)


def _is_loopback(url: str) -> bool:
    try:
        parts = urlsplit(url)
        if parts.scheme != "http" or not parts.hostname:
            return False
        host = parts.hostname
        return host == "localhost" or ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


class SiteClient:
    def __init__(self, config: Optional[SiteClientConfig] = None):
        self.config = config or SiteClientConfig()
        self._health: Optional[tuple[float, SiteResult]] = None

    @classmethod
    def from_extra(cls, extra: Optional[dict[str, Any]]) -> "SiteClient":
        return cls(SiteClientConfig.from_extra(extra))

    # ------------------------------------------------------------------ transport
    def _gate(self) -> Optional[SiteResult]:
        if not self.config.enabled:
            return _unavailable("disabled")
        if not _is_loopback(self.config.base_url):
            return _unavailable("url is not a loopback http URL")
        return None

    def _request(self, method: str, path: str, body: Optional[dict[str, Any]], timeout: float) -> tuple[Optional[dict[str, Any]], str]:
        """(parsed JSON object, "") on a valid reply, else (None, short reason). A 400 returns (body, "http 400") so the message survives."""
        data = None
        headers = {"Accept": "application/json"}
        if body is not None:
            try:
                data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            except (TypeError, ValueError):
                return None, "request not serializable"
            if len(data) > MAX_REQUEST_BYTES:
                return None, "request too large"
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.config.base_url + path, data=data, headers=headers, method=method)
        status = 0
        raw = b""
        try:
            with _OPENER.open(req, timeout=timeout) as resp:
                status = resp.status
                raw = resp.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            status = exc.code
            try:
                raw = exc.read(MAX_RESPONSE_BYTES + 1)
            except Exception:  # noqa: BLE001
                raw = b""
        except Exception as exc:  # noqa: BLE001 - URLError, timeout, refused, reset, ...
            name = type(exc).__name__
            if "timed out" in str(exc).lower() or isinstance(exc, TimeoutError):
                name = "timeout"
            return None, name
        if len(raw) > MAX_RESPONSE_BYTES:
            return None, "response too large"
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None, f"http {status}, invalid json" if status != 200 else "invalid json"
        if not isinstance(parsed, dict):
            return None, "unexpected json shape"
        if status != 200:
            return parsed, f"http {status}"
        if parsed.get("schema") != SCHEMA:
            return None, "schema mismatch"
        return parsed, ""

    def _call(self, method: str, path: str, body: Optional[dict[str, Any]], timeout: Optional[float] = None) -> tuple[Optional[dict[str, Any]], str]:
        gate = self._gate()
        if gate is not None:
            return None, gate.reason
        parsed, reason = self._request(method, path, body, timeout if timeout is not None else self.config.timeout_s)
        if reason:
            # only the route and a short reason are logged; never the request body
            logger.info("site service %s unavailable: %s", path, reason)
            if parsed is not None and isinstance(parsed.get("error"), str):
                return None, f"{reason}: {parsed['error'][:200]}"
        return (parsed if not reason else None), reason

    # ------------------------------------------------------------------ calls
    def health(self, *, force: bool = False) -> SiteResult:
        """Cached for 60 s (a down service is cached too, so a burst of intakes does not each pay a timeout)."""
        gate = self._gate()
        if gate is not None:
            return gate
        now = time.monotonic()
        if not force and self._health is not None and now - self._health[0] < HEALTH_CACHE_S:
            return self._health[1]
        parsed, reason = self._call("GET", "/v1/health", None, timeout=min(HEALTH_TIMEOUT_S, self.config.timeout_s))
        if parsed is None or parsed.get("status") != "ok":
            res = _unavailable(reason or "health not ok")
        else:
            built = parsed.get("index_built_at") or (parsed.get("corpus") or {}).get("built_at") or ""
            res = SiteResult(available=True, data=parsed, corpus_hash=str((parsed.get("corpus") or {}).get("content_hash", "")),
                             stale=self._is_stale(built), meta={"index_built_at": built})
        self._health = (now, res)
        return res

    def _is_stale(self, built_at: str) -> bool:
        try:
            when = datetime.strptime(built_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            return False
        return (datetime.now(timezone.utc) - when).days > self.config.stale_after_days

    def context_pack(self, pack: dict[str, Any], *, exclude: Optional[list[str]] = None, as_of: Optional[str] = None,
                     max_tokens: Optional[int] = None, include_weak: bool = False) -> SiteResult:
        """Closed link list + passages + glossary + ``missing_info`` for a submission. ``pack``: ``{date, description, quote, intake?}``."""
        body: dict[str, Any] = {"schema": SCHEMA, "pack": pack}
        if exclude:
            body["exclude"] = list(exclude)
        if as_of:
            body["as_of"] = as_of
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        if include_weak:
            body["include_weak"] = True
        parsed, reason = self._call("POST", "/v1/context-pack", body)
        if parsed is None:
            return _unavailable(reason)
        data = parsed.get("data")
        if not isinstance(data, dict) or not isinstance(parsed.get("text"), str):
            return _unavailable("malformed reply")
        return SiteResult(available=True, text=parsed["text"], data=data, corpus_hash=str(parsed.get("corpus_hash", "")), meta={"request_id": parsed.get("request_id", "")})

    def verify_links(self, draft: Any, *, candidates: Optional[list[str]] = None) -> SiteResult:
        """``draft``: markdown string or the Stylus metadata dict. ``candidates``: page ids/URLs from the pre-draft context pack
        (an empty list means "no link allowed"; ``None`` skips the closed-list check)."""
        body: dict[str, Any] = {"schema": SCHEMA, "draft": draft}
        if candidates is not None:
            body["candidates"] = list(candidates)
        parsed, reason = self._call("POST", "/v1/verify-links", body)
        if parsed is None:
            return _unavailable(reason)
        data = parsed.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("ok"), bool) or not isinstance(parsed.get("text"), str):
            return _unavailable("malformed reply")
        return SiteResult(available=True, text=parsed["text"], data=data, passed=data["ok"], corpus_hash=str(parsed.get("corpus_hash", "")),
                          meta={"request_id": parsed.get("request_id", "")})
