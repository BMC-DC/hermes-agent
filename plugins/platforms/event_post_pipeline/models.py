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
REVIEW_ACTIONS = ("approved", "rejected", "refine")


@dataclass(frozen=True)
class ReviewActionPayload:
    task_id: str
    platform: str
    action: str
    comment: str


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
    return ReviewActionPayload(task_id=task_id, platform=platform, action=action, comment=comment)


def parse_submission_id(payload: Any) -> Optional[str]:
    if not isinstance(payload, dict):
        return None
    submission_id = payload.get("submissionId")
    return submission_id if isinstance(submission_id, str) and SAFE_ID_RE.match(submission_id) else None
