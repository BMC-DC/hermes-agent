"""Brevo (Sendinblue) v3 API client for sending the approved newsletter.

Built now, wired later — per newsletter-pipeline-plan.md's explicit decision:
"Brevo send is real infrastructure, built now, wired later." This module is a
complete, working client against Brevo's documented v3 campaign API; nothing
in the rest of this plugin calls ``create_and_send_campaign`` yet (approving a
newsletter today stops at "approved" — see ``pipeline.py``). Wiring it in is
a small, deliberate follow-up once Amila has real Brevo API access and the
target list/segment id from the email team, not a rebuild.

Kept as its own module (not folded into ``review_client.py``) since it talks
to a completely different third-party API, not social-post-portal.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Optional

BREVO_API_BASE = "https://api.brevo.com/v3"
DEFAULT_TIMEOUT_SECONDS = 30


class BrevoClientError(RuntimeError):
    """Raised when Brevo's API rejects a request, is unreachable, or no API
    key is configured yet — this last case is the expected state until the
    email team provisions access (see this module's docstring)."""


@dataclass(frozen=True)
class BrevoConfig:
    api_key: str
    sender_name: str
    sender_email: str
    list_ids: tuple[int, ...] = ()

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.sender_email and self.list_ids)


def resolve_brevo_config(extra: dict) -> BrevoConfig:
    """Reads ``platforms.newsletter_pipeline.extra.brevo`` from config.yaml:

      platforms.newsletter_pipeline.extra:
        brevo:
          sender_name: "Buddha Meditation Center DC"
          sender_email: "hello@buddhameditationdc.org"
          list_ids: [3]   # Brevo contact list id(s) to send to

    ``api_key`` always comes from the ``BREVO_API_KEY`` env var (never
    config.yaml) via the same ``${VAR}``-resolution convention as this
    plugin's other secrets — see security.resolve_env_secret.
    """
    from plugins.platforms.newsletter_pipeline import security

    brevo_extra = dict((extra or {}).get("brevo") or {})
    api_key = security.resolve_env_secret(brevo_extra.get("api_key", "${BREVO_API_KEY}"))
    list_ids = tuple(int(i) for i in (brevo_extra.get("list_ids") or []))
    return BrevoConfig(
        api_key=api_key,
        sender_name=str(brevo_extra.get("sender_name") or ""),
        sender_email=str(brevo_extra.get("sender_email") or ""),
        list_ids=list_ids,
    )


def _request(method: str, path: str, config: BrevoConfig, body: Optional[dict[str, Any]] = None, *, timeout: float) -> dict[str, Any]:
    if not config.api_key:
        raise BrevoClientError(
            "Brevo is not configured yet — set BREVO_API_KEY and "
            "platforms.newsletter_pipeline.extra.brevo (sender_name/sender_email/list_ids) "
            "in config.yaml once the email team provisions API access"
        )
    req = urllib.request.Request(
        f"{BREVO_API_BASE}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json", "Accept": "application/json", "api-key": config.api_key},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        raise BrevoClientError(f"Brevo API {method} {path} failed: {exc.code} {detail}") from exc
    except Exception as exc:
        raise BrevoClientError(f"Brevo API {method} {path} failed: {exc}") from exc


def create_campaign(
    config: BrevoConfig, *, subject: str, html_content: str, campaign_name: str, timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> int:
    """Creates a draft email campaign targeting ``config.list_ids``. Returns the new campaign id.

    Raises :class:`BrevoClientError` (including the "not configured" case) —
    callers should let that surface as a clear operator-facing error, never
    silently no-op, per this plugin's ``PipelineError`` convention.
    """
    if not config.configured:
        raise BrevoClientError(
            "Brevo config incomplete — need api_key, sender_email, and at least one list_id"
        )
    payload = {
        "name": campaign_name,
        "subject": subject,
        "sender": {"name": config.sender_name, "email": config.sender_email},
        "type": "classic",
        "htmlContent": html_content,
        "recipients": {"listIds": list(config.list_ids)},
    }
    result = _request("POST", "/emailCampaigns", config, payload, timeout=timeout)
    campaign_id = result.get("id")
    if not isinstance(campaign_id, int):
        raise BrevoClientError(f"Brevo campaign create response missing 'id': {result!r}")
    return campaign_id


def send_campaign_now(config: BrevoConfig, campaign_id: int, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> None:
    """Sends a previously-created draft campaign immediately."""
    _request("POST", f"/emailCampaigns/{campaign_id}/sendNow", config, timeout=timeout)


def create_and_send_campaign(
    config: BrevoConfig, *, subject: str, html_content: str, campaign_name: str, timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> int:
    """Convenience wrapper: create, then send immediately. Not called from
    anywhere in this plugin yet — see this module's docstring."""
    campaign_id = create_campaign(config, subject=subject, html_content=html_content, campaign_name=campaign_name, timeout=timeout)
    send_campaign_now(config, campaign_id, timeout=timeout)
    return campaign_id
