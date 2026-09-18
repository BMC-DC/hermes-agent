"""Core deterministic-glue logic — the code replacement for
``event-post-pipeline/SKILL.md`` steps 2, 3, 4 (partial), 5, 6. Stylus's
drafting (step 2's target, and every refine round) is untouched: this module
only creates its handoff tasks and reacts to their completion.

Every function here is plain, synchronous, testable Python — no LLM call,
no agent turn. The one deliberate exception this plugin depends on is that
Stylus itself must keep producing the structured ``kanban_complete(metadata=
{"event_title", "fb", "ig", "blog": {...}})`` contract verified against the
live Kanban DB during design (see
``extra/plans/socialpost/event-post-pipeline-deterministic-glue-plan.md``).
``event_title`` may be absent until that SOUL.md addition ships — handled
by falling back to a mechanical truncation of the description, never by
crashing or fabricating a review page from partial data.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Optional

from plugins.platforms.event_post_pipeline.review_client import (
    ReviewClientConfig, ReviewClientError, create_review, slug_from_review_url, update_review,
)
from plugins.platforms.event_post_pipeline.store import EventPostPipelineStore

logger = logging.getLogger("plugins.platforms.event_post_pipeline")

TENANT = "event-post-pipeline-v2"
CREATED_BY = "event-post-pipeline-plugin"
_REFINE_IDEMPOTENCY_RE = re.compile(r"^(?P<root>.+)-(?P<platform>fb|ig|blog)-r(?P<round>\d+)$")
_TITLE_MAX_CHARS = 80
_REVIEW_URL_COMMENT_RE = re.compile(r"Review page:\s*(\S+)")


class PipelineError(RuntimeError):
    """Raised for conditions that should stop this event and leave it for a human,
    never for something safe to silently retry or guess past."""


@dataclass(frozen=True)
class KanbanOps:
    """The subset of ``hermes_cli.kanban_db`` this module calls, injected so
    tests can run against a real throwaway SQLite board with zero mocking."""

    kb: Any  # the hermes_cli.kanban_db module itself

    def create_task(self, conn, **kwargs) -> str:
        return self.kb.create_task(conn, **kwargs)

    def get_task(self, conn, task_id: str):
        return self.kb.get_task(conn, task_id)

    def latest_run(self, conn, task_id: str):
        return self.kb.latest_run(conn, task_id)

    def add_comment(self, conn, task_id: str, author: str, body: str) -> int:
        return self.kb.add_comment(conn, task_id, author, body)

    def list_comments(self, conn, task_id: str):
        return self.kb.list_comments(conn, task_id)

    def list_tasks(self, conn, **kwargs):
        return self.kb.list_tasks(conn, **kwargs)


def _short_title(description: str) -> str:
    """Mechanical fallback only — prefer Stylus's ``event_title`` metadata field."""
    first_sentence = re.split(r"(?<=[.!?])\s", description.strip(), maxsplit=1)[0]
    text = first_sentence if len(first_sentence) <= _TITLE_MAX_CHARS else description.strip()
    return (text[: _TITLE_MAX_CHARS - 1] + "…") if len(text) > _TITLE_MAX_CHARS else text


def _wrap_untrusted(event_pack: dict[str, Any]) -> str:
    """Same defensive framing the skill file's webhook prompt used: the fields inside
    came from a public form and must never be read as instructions by anything that
    later reads this task body (Stylus is itself an LLM)."""
    lines = [
        "<untrusted_submission>",
        f"Date: {event_pack.get('date', '')}",
        f"Description: {event_pack.get('description', '')}",
        f"Quote: {event_pack.get('quote', '')}",
        f"First photo: {(event_pack.get('first') or {}).get('finalUrl', '')}",
        f"Last photo: {(event_pack.get('last') or {}).get('finalUrl', '')}",
        f"Rest: {[r.get('finalUrl') for r in (event_pack.get('rest') or [])]}",
        "</untrusted_submission>",
    ]
    return "\n".join(lines)


def _image_list(event_pack: dict[str, Any]) -> list[str]:
    first = (event_pack.get("first") or {}).get("finalUrl")
    rest = [r.get("finalUrl") for r in (event_pack.get("rest") or []) if r.get("finalUrl")]
    last = (event_pack.get("last") or {}).get("finalUrl")
    return [u for u in ([first] + rest + [last]) if u]


def handle_intake(conn, ops: KanbanOps, store: EventPostPipelineStore, *, submission_id: str, event_pack: dict[str, Any]) -> str:
    """Step 2: create the Stylus handoff. No LLM call. Returns the new task id."""
    description = str(event_pack.get("description") or "")
    task_id = ops.create_task(
        conn, title=f"Draft social copy: {_short_title(description)}", body=_wrap_untrusted(event_pack),
        assignee="stylus", idempotency_key=submission_id, tenant=TENANT, created_by=CREATED_BY,
    )
    store.upsert_submission(
        submission_id,
        {
            "root_task_id": task_id, "date": event_pack.get("date"), "description": description,
            "quote": event_pack.get("quote"), "images": _image_list(event_pack),
            # Carried through to create_review() at first-round completion so
            # post_submissions.submitted_by can be resolved (see plan doc's
            # "Social Media Curator" ledger rationale) — same fields adapter.py's
            # intake notification already reads off the raw event_pack.
            "submitter_name": event_pack.get("submitterName"), "submitter_phone": event_pack.get("submitterPhone"),
        },
    )
    logger.info("event_post_pipeline: intake submission=%s -> task=%s", submission_id, task_id)
    return task_id


def _record_for_task(conn, ops: KanbanOps, store: EventPostPipelineStore, task) -> Optional[dict[str, Any]]:
    """The submission record owning ``task`` — from the store, or reconstructed from
    Kanban's own comment history when the store is empty/stale (never the reverse)."""
    refine = _REFINE_IDEMPOTENCY_RE.match(task.idempotency_key or "")
    root_task_id = refine.group("root") if refine else task.id
    record = store.find_by_task_id(root_task_id) or store.get_submission(root_task_id)
    if record is not None:
        return record
    # Store miss: reconstruct the minimum needed (review slug) from the root task's own
    # comment history, exactly as the skill file this replaces already relies on.
    root_task = ops.get_task(conn, root_task_id)
    if root_task is None:
        return None
    for comment in ops.list_comments(conn, root_task_id):
        if match := _REVIEW_URL_COMMENT_RE.search(comment.body):
            return {"root_task_id": root_task_id, "review_url": match.group(1)}
    return None


def _extract_stylus_metadata(run) -> dict[str, Any]:
    if run is None or run.outcome != "completed" or not isinstance(run.metadata, dict):
        raise PipelineError(f"Stylus run for is missing or did not complete cleanly (outcome={getattr(run, 'outcome', None)!r})")
    metadata = run.metadata
    blog = metadata.get("blog")
    if not isinstance(blog, dict) or not blog.get("body"):
        raise PipelineError(f"Stylus completion metadata missing a well-formed 'blog' object: {metadata!r}")
    for key in ("fb", "ig"):
        if not isinstance(metadata.get(key), str) or not metadata[key].strip():
            raise PipelineError(f"Stylus completion metadata missing required string field {key!r}: {metadata!r}")
    return metadata


def _blog_seo_payload(blog: dict[str, Any]) -> dict[str, Any]:
    return {
        "seoTitle": blog.get("seo_title", ""), "urlSlug": blog.get("url_slug", ""),
        "focusKeyword": blog.get("focus_keyword", ""), "supportingKeywords": blog.get("supporting_keywords") or [],
        "metaDescription": blog.get("meta_description", ""),
    }


def handle_stylus_completion(
    conn, ops: KanbanOps, store: EventPostPipelineStore, review_config: ReviewClientConfig, task_id: str,
) -> Optional[dict[str, Any]]:
    """The ``kanban_task_completed`` hook callback's core logic (transport/async
    concerns live in ``adapter.py``). Returns a dict describing what happened (for
    logging/tests), or ``None`` when this completion isn't ours to react to.

    Raises :class:`PipelineError` for anything that should stop and leave the task
    visible for a human rather than silently guess past it.
    """
    task = ops.get_task(conn, task_id)
    if task is None or task.tenant != TENANT or task.assignee != "stylus":
        return None  # not ours
    run = ops.latest_run(conn, task_id)
    metadata = _extract_stylus_metadata(run)
    refine = _REFINE_IDEMPOTENCY_RE.match(task.idempotency_key or "")
    if refine:
        return _handle_refine_round_completion(conn, ops, store, review_config, task, metadata, refine.group("platform"))
    return _handle_first_round_completion(conn, ops, store, review_config, task, metadata)


def _handle_first_round_completion(conn, ops, store, review_config, task, metadata: dict[str, Any]) -> dict[str, Any]:
    record = store.get_submission(task.idempotency_key) or {}
    blog = metadata["blog"]
    event_title = str(metadata.get("event_title") or _short_title(record.get("description") or task.title))
    try:
        review_url = create_review(
            review_config, task_id=task.id, event_title=event_title, date=record.get("date", ""),
            images=record.get("images", []), fb=metadata["fb"], ig=metadata["ig"], blog_body=blog["body"],
            blog_seo=_blog_seo_payload(blog),
            submitter_name=record.get("submitter_name"), submitter_phone=record.get("submitter_phone"),
        )
    except ReviewClientError as exc:
        raise PipelineError(str(exc)) from exc
    ops.add_comment(conn, task.id, CREATED_BY, f"Review page: {review_url}")
    store.upsert_submission(task.idempotency_key, {"review_url": review_url})
    return {"action": "created_review_page", "task_id": task.id, "review_url": review_url}


def _handle_refine_round_completion(conn, ops, store, review_config, task, metadata: dict[str, Any], platform: str) -> dict[str, Any]:
    record = _record_for_task(conn, ops, store, task)
    review_url = (record or {}).get("review_url")
    if not review_url:
        raise PipelineError(f"could not recover the review-page URL for refine task {task.id} (platform={platform})")
    slug = slug_from_review_url(review_url)
    blog = metadata.get("blog") if platform == "blog" else None
    text = blog["body"] if platform == "blog" else metadata[platform]
    seo = _blog_seo_payload(blog) if platform == "blog" else None
    try:
        update_review(review_config, slug=slug, platform=platform, text=text, seo=seo)
    except ReviewClientError as exc:
        raise PipelineError(str(exc)) from exc
    root_task_id = (record or {}).get("root_task_id") or task.idempotency_key.rsplit("-", 2)[0]
    store.upsert_submission(root_task_id, {"rounds": {platform: {"task_id": task.id, "status": "pending"}}})
    return {"action": "updated_review_page", "task_id": task.id, "platform": platform, "review_url": review_url}


# --- Step 5/6: reacting to a review-page action -----------------------------------------

def _platform_round_tasks(conn, ops: KanbanOps, root_task_id: str, platform: str) -> list:
    """Every redraft round already created for ``platform`` under ``root_task_id``, in
    creation order — queried against Kanban directly (never the local store) so the next
    round number is always correct even after a store loss or restart."""
    prefix = f"{root_task_id}-{platform}-r"
    tasks = ops.list_tasks(conn, tenant=TENANT, assignee="stylus", include_archived=True)
    matches = [t for t in tasks if (t.idempotency_key or "").startswith(prefix)]
    return sorted(matches, key=lambda t: int((t.idempotency_key or "").rsplit("r", 1)[-1]))


def _current_platform_task(conn, ops: KanbanOps, root_task_id: str, platform: str):
    """The most recent task that actually drafted ``platform``: the latest redraft round,
    or the original root task if there has never been one."""
    rounds = _platform_round_tasks(conn, ops, root_task_id, platform)
    return rounds[-1] if rounds else ops.get_task(conn, root_task_id)


def handle_review_action(
    conn, ops: KanbanOps, store: EventPostPipelineStore, *, task_id: str, platform: str, action: str, comment: str,
) -> dict[str, Any]:
    """Step 5/6: a review-page button click. ``task_id`` here is the review page's
    recorded ``taskId`` — always the *root* Stylus task, per the skill's own contract
    (``lib/event-review.ts``'s ``EventReview.taskId`` is set once at page creation).
    """
    root_task = ops.get_task(conn, task_id)
    if root_task is None:
        raise PipelineError(f"review action referenced unknown task {task_id!r}")
    current_task = _current_platform_task(conn, ops, task_id, platform)
    if current_task is None:
        raise PipelineError(f"no drafting task found for platform={platform!r} under root task {task_id!r}")

    if action in ("approved", "rejected"):
        outcome_line = f"{platform.upper()} {action.upper()} via review page: {comment}" if comment else f"{platform.upper()} {action.upper()} via review page"
        ops.add_comment(conn, current_task.id, CREATED_BY, outcome_line)
        return {"action": action, "task_id": current_task.id, "platform": platform}

    if action == "refine":
        record = store.get_submission(task_id) or {}
        previous_draft = _previous_draft_text(ops.latest_run(conn, current_task.id), platform)
        next_round = len(_platform_round_tasks(conn, ops, task_id, platform)) + 1
        idempotency_key = f"{task_id}-{platform}-r{next_round}"
        body = _refine_task_body(record=record, platform=platform, previous_draft=previous_draft, comment=comment)
        new_task_id = ops.create_task(
            conn, title=f"Redraft {platform.upper()} (round {next_round}): {root_task.title.removeprefix('Draft social copy: ')}",
            body=body, assignee="stylus", idempotency_key=idempotency_key, tenant=TENANT, created_by=CREATED_BY,
        )
        store.upsert_submission(task_id, {"rounds": {platform: {"task_id": new_task_id, "round": next_round, "status": "in_progress"}}})
        return {"action": "refine_dispatched", "task_id": new_task_id, "platform": platform, "round": next_round}

    raise PipelineError(f"unknown review action {action!r}")


def _previous_draft_text(run, platform: str) -> str:
    if run is None or not isinstance(run.metadata, dict):
        return ""
    value = run.metadata.get(platform)
    if platform == "blog" and isinstance(value, dict):
        return str(value.get("body") or "")
    return str(value or "")


def _refine_task_body(*, record: dict[str, Any], platform: str, previous_draft: str, comment: str) -> str:
    return "\n\n".join(
        part for part in (
            "<untrusted_submission>",
            f"Date: {record.get('date', '')}",
            f"Description: {record.get('description', '')}",
            f"Quote: {record.get('quote', '')}",
            "</untrusted_submission>",
            f"Previous {platform.upper()} draft:\n{previous_draft}",
            f"Requested change (reviewer's own words):\n{comment}",
            f"Only the {platform.upper()} content needs to be redrafted — the other platforms are "
            "unaffected and already handled separately.",
        ) if part
    )
