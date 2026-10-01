"""Payload shapes this plugin receives, and the light validation each gets.

Deliberately structural, not exhaustive: the event pack's own detailed field
validation already happened in ``social-post-portal``
(``app/api/submit-event-post/route.ts``) before it reached here. This layer
only confirms the shape needed to route and act on it safely — it never
trusts the *content* of free-text fields (date/description/quote) as
anything but data, matching the skill file it replaces.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional

SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
REVIEW_PLATFORMS = ("fb", "ig", "blog")
REVIEW_ACTIONS = ("approved", "rejected", "refine", "edited")

# Generous-but-bounded caps for a reviewer's manual edit (the portal enforces the
# same numbers; this is the independent second check — Hermes writes these into a
# Kanban comment and any later redraft's task body).
EDITED_TEXT_MAX_CHARS = 20_000
_EDITED_SEO_KEYS = ("seoTitle", "urlSlug", "focusKeyword", "metaDescription")
_EDITED_SEO_MAX_CHARS = 1_000
_EDITED_SEO_KEYWORDS_MAX = 30


@dataclass(frozen=True)
class ReviewActionPayload:
    task_id: str
    platform: str
    action: str
    comment: str
    # Only for action == "edited": the reviewer's hand-written draft (and, for the blog,
    # its SEO fields in the portal's camelCase shape). None for every other action.
    edited_text: Optional[str] = None
    edited_seo: Optional[dict] = None


def _parse_edited_seo(value: Any) -> Optional[dict]:
    if not isinstance(value, dict):
        return None
    out: dict[str, Any] = {}
    for key in _EDITED_SEO_KEYS:
        item = value.get(key)
        if not isinstance(item, str) or len(item) > _EDITED_SEO_MAX_CHARS:
            return None
        out[key] = item
    keywords = value.get("supportingKeywords")
    if (
        not isinstance(keywords, list) or len(keywords) > _EDITED_SEO_KEYWORDS_MAX
        or not all(isinstance(k, str) and len(k) <= _EDITED_SEO_MAX_CHARS for k in keywords)
    ):
        return None
    out["supportingKeywords"] = list(keywords)
    return out


def parse_review_action(payload: Any) -> Optional[ReviewActionPayload]:
    """``None`` on any shape/value mismatch; comment is always a string (may be empty)."""
    if not isinstance(payload, dict):
        return None
    task_id = payload.get("taskId")
    platform = payload.get("platform")
    action = payload.get("action")
    comment = payload.get("comment", "")
    if not isinstance(task_id, str) or not SAFE_ID_RE.match(task_id):
        return None
    if platform not in REVIEW_PLATFORMS or action not in REVIEW_ACTIONS:
        return None
    if not isinstance(comment, str):
        return None
    if action == "edited":
        edited_text = payload.get("editedText")
        if not isinstance(edited_text, str) or not edited_text.strip() or len(edited_text) > EDITED_TEXT_MAX_CHARS:
            return None
        edited_seo = None
        if platform == "blog":
            edited_seo = _parse_edited_seo(payload.get("editedSeo"))
            if edited_seo is None:
                return None
        return ReviewActionPayload(
            task_id=task_id, platform=platform, action=action, comment=comment,
            edited_text=edited_text, edited_seo=edited_seo,
        )
    return ReviewActionPayload(task_id=task_id, platform=platform, action=action, comment=comment)


def parse_submission_id(payload: Any) -> Optional[str]:
    if not isinstance(payload, dict):
        return None
    submission_id = payload.get("submissionId")
    return submission_id if isinstance(submission_id, str) and SAFE_ID_RE.match(submission_id) else None
