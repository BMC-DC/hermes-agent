"""Client for social-post-portal's ``/api/newsletter-review`` routes — same
bearer-token contract and sync-``urllib`` style as
``event_post_pipeline.review_client``, kept as a separate module per that
module's own convention (this plugin's route is a deliberately independent
listener, not an extension of the event-post one).
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
    create_secret: str  # NEWSLETTER_REVIEW_CREATE_SECRET, shared with the portal


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
    issue_month: str,
    bhante_advice_text: str,
    bhante_advice_quote: Optional[str],
    recap_summary: str,
    recap_image: Optional[dict[str, Any]],
    recap_link_url: Optional[str] = None,
    featured_announcement_text: str,
    featured_cta_label: str,
    featured_cta_url: str,
    programs_list: list[dict[str, str]],
    bonus_callout: Optional[dict[str, Any]],
    images: list[dict[str, Any]],
    draft: dict[str, Any],
    submitter_name: Optional[str] = None,
    submitter_phone: Optional[str] = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> str:
    """Creates the newsletter review page; returns its full URL."""
    payload: dict[str, Any] = {
        "taskId": task_id,
        "issueMonth": issue_month,
        "bhanteAdviceText": bhante_advice_text,
        "bhanteAdviceQuote": bhante_advice_quote,
        "recapSummary": recap_summary,
        "recapImage": recap_image,
        "recapLinkUrl": recap_link_url,
        "featuredAnnouncementText": featured_announcement_text,
        "featuredCtaLabel": featured_cta_label,
        "featuredCtaUrl": featured_cta_url,
        "programsList": programs_list,
        "bonusCallout": bonus_callout,
        "images": images,
        "draft": draft,
    }
    if submitter_name and submitter_phone:
        payload["submitterName"] = submitter_name
        payload["submitterPhone"] = submitter_phone
    result = _post_json(f"{config.base_url}/api/newsletter-review", payload, config.create_secret, timeout=timeout)
    url = result.get("url")
    if not isinstance(url, str) or not url:
        raise ReviewClientError(f"newsletter review-page create response missing 'url': {result!r}")
    return url


def update_review(
    config: ReviewClientConfig, *, slug: str, draft: dict[str, Any], timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> None:
    """Refreshes the issue's draft in place (a new round), resetting it to pending on the same page/URL."""
    _post_json(f"{config.base_url}/api/newsletter-review/{slug}/update", draft, config.create_secret, timeout=timeout)


def record_send(
    config: ReviewClientConfig, *, slug: str, brevo_campaign_id: Optional[str], timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> None:
    """Records a completed Brevo send against this issue — called right after
    a successful ``brevo_client.create_and_send_campaign`` call in
    ``pipeline.py``'s approve path."""
    _post_json(
        f"{config.base_url}/api/newsletter-review/{slug}/send-log",
        {"brevoCampaignId": brevo_campaign_id},
        config.create_secret,
        timeout=timeout,
    )


def slug_from_review_url(url: str) -> str:
    """Extracts the slug from a recorded review URL (``.../review/newsletter/<slug>``)."""
    slug = url.rstrip("/").rsplit("/", 1)[-1]
    if not slug:
        raise ReviewClientError(f"could not extract a slug from review URL: {url!r}")
    return slug
