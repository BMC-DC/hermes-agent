"""Payload shapes this plugin receives, and the light validation each gets.

Deliberately structural, not exhaustive: the intake pack's detailed field
validation already happened in social-post-portal
(``app/api/submit-newsletter/route.ts``) before it reached here. This layer
only confirms the shape needed to route and act on it safely.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional

SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
REVIEW_ACTIONS = ("approved", "rejected", "refine", "edited")

# A reviewer's manual edit: the plain-text copy Stylus normally authors (its
# ``kanban_complete`` metadata keys — see pipeline.DRAFT_METADATA_FIELDS), re-rendered
# here by the deterministic template. Caps match the portal's (lib/manual-edit.ts);
# this is the independent second check, since what passes here is what gets emailed to
# every subscriber. Single-line fields must not contain line breaks.
EDITED_COPY_SINGLE_LINE_MAX = {"subject_line": 250, "preview_text": 400, "eyebrow_label": 120, "hero_headline": 250}
EDITED_COPY_PARAGRAPH_MAX = 6_000
EDITED_COPY_FIELDS = (
    "subject_line", "preview_text", "eyebrow_label", "hero_headline",
    "bhante_advice_paragraph", "recap_paragraph", "featured_announcement_paragraph",
)
_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class ReviewActionPayload:
    task_id: str
    action: str
    comment: str
    # Only for action == "edited": the reviewer's edited copy (all seven fields).
    copy: Optional[dict] = None
    # Only sent with "approved" for a manually-edited draft: SHA-256 hex of the exact
    # assembled HTML the reviewer approved. pipeline.handle_review_action refuses to
    # approve/send if its own stored draft doesn't hash to this.
    draft_sha256: Optional[str] = None


def parse_edited_copy(value: Any) -> Optional[dict]:
    """The seven copy fields, unmodified, or ``None`` if any is missing/empty/oversized
    (or a single-line field contains a line break). Never normalizes — what is
    validated is exactly what gets rendered."""
    if not isinstance(value, dict):
        return None
    out: dict[str, str] = {}
    for key in EDITED_COPY_FIELDS:
        item = value.get(key)
        if not isinstance(item, str) or not item.strip():
            return None
        single_line_max = EDITED_COPY_SINGLE_LINE_MAX.get(key)
        if single_line_max is not None:
            if len(item) > single_line_max or "\n" in item or "\r" in item:
                return None
        elif len(item) > EDITED_COPY_PARAGRAPH_MAX:
            return None
        out[key] = item
    return out


def parse_review_action(payload: Any) -> Optional[ReviewActionPayload]:
    """``None`` on any shape/value mismatch; comment is always a string (may be empty)."""
    if not isinstance(payload, dict):
        return None
    task_id = payload.get("taskId")
    action = payload.get("action")
    comment = payload.get("comment", "")
    if not isinstance(task_id, str) or not SAFE_ID_RE.match(task_id):
        return None
    if action not in REVIEW_ACTIONS:
        return None
    if not isinstance(comment, str):
        return None
    if action == "edited":
        copy = parse_edited_copy(payload.get("copy"))
        if copy is None:
            return None
        return ReviewActionPayload(task_id=task_id, action=action, comment=comment, copy=copy)
    draft_sha256 = payload.get("draftSha256")
    if draft_sha256 is not None:
        if action != "approved" or not isinstance(draft_sha256, str) or not _SHA256_HEX_RE.match(draft_sha256):
            return None  # a malformed integrity hash must never be silently ignored
    return ReviewActionPayload(task_id=task_id, action=action, comment=comment, draft_sha256=draft_sha256)


def parse_submission_id(payload: Any) -> Optional[str]:
    if not isinstance(payload, dict):
        return None
    submission_id = payload.get("submissionId")
    return submission_id if isinstance(submission_id, str) and SAFE_ID_RE.match(submission_id) else None
