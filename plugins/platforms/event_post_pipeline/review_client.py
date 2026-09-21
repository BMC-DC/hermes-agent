"""Client for the review-page API (``social-post-portal``'s
``/api/event-review`` routes) — same bearer-token contract the skill file's
hand-written scripts already use, and the same endpoints, unchanged. Sync
(``urllib``) to match the pattern already proven in production and to avoid
a new dependency; called from this plugin's own worker thread, never the
gateway event loop.
"""

from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass
from typing import Any, Optional

DEFAULT_TIMEOUT_SECONDS = 30


class ReviewClientError(RuntimeError):
    """Raised when the review-page API rejects a request or is unreachable."""


@dataclass(frozen=True)
class ReviewClientConfig:
    base_url: str  # e.g. "https://spp.buddhameditationdc.org"
    create_secret: str  # EVENT_REVIEW_CREATE_SECRET, shared with the portal


def _post_json(url: str, payload: dict[str, Any], secret: str, *, timeout: float) -> dict[str, Any]:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {secret}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception as exc:
        raise ReviewClientError(f"review-page API request to {url} failed: {exc}") from exc


def create_review(
    config: ReviewClientConfig,
    *,
    task_id: str,
    event_title: str,
    date: str,
    images: list[str],
    fb: str,
    ig: str,
    blog_body: str,
    blog_seo: dict[str, Any],
    submitter_name: Optional[str] = None,
    submitter_phone: Optional[str] = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> str:
    """Creates the review page; returns its full URL.

    ``submitter_name``/``submitter_phone`` are optional and additive to the
    existing contract — the portal resolves ``post_submissions.submitted_by``
    from them when present, and leaves it NULL otherwise (older callers, or a
    submission the shared-login field didn't capture, are unaffected).
    """
    payload: dict[str, Any] = {
        "taskId": task_id, "eventTitle": event_title, "date": date, "images": images,
        "fb": fb, "ig": ig, "blogBody": blog_body, "blogSeo": blog_seo,
    }
    if submitter_name and submitter_phone:
        payload["submitterName"] = submitter_name
        payload["submitterPhone"] = submitter_phone
    result = _post_json(f"{config.base_url}/api/event-review", payload, config.create_secret, timeout=timeout)
    url = result.get("url")
    if not isinstance(url, str) or not url:
        raise ReviewClientError(f"review-page create response missing 'url': {result!r}")
    return url


def update_review(
    config: ReviewClientConfig,
    *,
    slug: str,
    platform: str,
    text: str,
    seo: Optional[dict[str, Any]] = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> None:
    """Refreshes one platform's draft in place, resetting it to pending on the same page/URL."""
    payload: dict[str, Any] = {"platform": platform, "text": text}
    if platform == "blog" and seo:
        payload["seo"] = seo
    _post_json(f"{config.base_url}/api/event-review/{slug}/update", payload, config.create_secret, timeout=timeout)


def slug_from_review_url(url: str) -> str:
    """Extracts the slug from a recorded review URL (``.../review/<slug>``)."""
    slug = url.rstrip("/").rsplit("/", 1)[-1]
    if not slug:
        raise ReviewClientError(f"could not extract a slug from review URL: {url!r}")
    return slug
