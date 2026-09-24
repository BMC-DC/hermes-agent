"""Core deterministic-glue logic for the newsletter pipeline — mirrors
``event_post_pipeline.pipeline``, simplified to a single draft (not
per-platform): one Stylus task drafts every Section 10 slot for one issue,
and a refine round redrafts the whole issue rather than one platform at a
time (a newsletter has no independent "platforms" to refine separately).

Every function here is plain, synchronous, testable Python — no LLM call.
Stylus must produce the structured ``kanban_complete(metadata={
"subject_line", "preview_text", "bhante_advice_html", "recap_html",
"featured_announcement_html", "programs_html"})`` contract — see
``/home/bmc/.hermes/profiles/stylus/SOUL.md``'s newsletter section.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Optional

from plugins.platforms.newsletter_pipeline import brevo_client, db
from plugins.platforms.newsletter_pipeline.brevo_client import BrevoClientError, BrevoConfig
from plugins.platforms.newsletter_pipeline.review_client import (
    ReviewClientConfig, ReviewClientError, create_review, record_send, slug_from_review_url, update_review,
)
from plugins.platforms.newsletter_pipeline.store import NewsletterPipelineStore

logger = logging.getLogger("plugins.platforms.newsletter_pipeline")

TENANT = "newsletter-pipeline-v1"
CREATED_BY = "newsletter-pipeline-plugin"
_REFINE_IDEMPOTENCY_RE = re.compile(r"^(?P<root>.+)-r(?P<round>\d+)$")
_REVIEW_URL_COMMENT_RE = re.compile(r"Review page:\s*(\S+)")

DRAFT_METADATA_FIELDS = (
    "subject_line", "preview_text", "bhante_advice_html", "recap_html",
    "featured_announcement_html", "programs_html", "assembled_html",
)


class PipelineError(RuntimeError):
    """Raised for conditions that should stop this issue and leave it for a
    human, never for something safe to silently retry or guess past."""


@dataclass(frozen=True)
class KanbanOps:
    """The subset of ``hermes_cli.kanban_db`` this module calls — mirrors
    event_post_pipeline.pipeline.KanbanOps, injected so tests can run
    against a real throwaway SQLite board with zero mocking."""

    kb: Any

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


def _wrap_untrusted(intake: dict[str, Any]) -> str:
    """Same defensive framing as event_post_pipeline's task bodies: fields
    from a form submission must never be read as instructions by Stylus."""
    lines = [
        "<untrusted_submission>",
        f"Issue month: {intake.get('issueMonth', '')}",
        f"Bhante's advice text: {intake.get('bhanteAdviceText', '')}",
        f"Bhante's advice quote: {intake.get('bhanteAdviceQuote', '')}",
        f"Recap summary: {intake.get('recapSummary', '')}",
        f"Recap photo: {(intake.get('recapImage') or {}).get('url', '')}",
        f"Featured announcement text: {intake.get('featuredAnnouncementText', '')}",
        f"Featured CTA label: {intake.get('featuredCtaLabel', '')}",
        f"Featured CTA url: {intake.get('featuredCtaUrl', '')}",
        f"Programs summary: {intake.get('programsSummary', '')}",
        f"Curator-suggested subject line: {intake.get('subjectLine', '')}",
        f"Curator-suggested preview text: {intake.get('previewText', '')}",
        "</untrusted_submission>",
    ]
    return "\n".join(lines)


# --- Pipeline visualizer tracking ---------------------------------------------------------
#
# Best-effort only, mirrors event_post_pipeline.pipeline's own rule: a visualizer-tracking
# failure must never break the actual pipeline. Every helper below swallows its own errors
# (after logging) and is a silent no-op when no Postgres is configured for this profile.


def _resolve_visualizer_db_url(database_url: Optional[str]) -> str:
    return database_url if database_url is not None else db.resolve_database_url()


def _track_run(
    database_url: Optional[str], task_id: str, *, title: str, state: str, current_step: str,
    step: str, status: str, detail: Optional[str] = None, actor: Optional[str] = None,
) -> None:
    url = _resolve_visualizer_db_url(database_url)
    if not url:
        return
    try:
        conn = db.get_connection(url)
    except db.DatabaseError:
        logger.debug("newsletter_pipeline: visualizer tracking skipped (no Postgres) for task=%s step=%s", task_id, step)
        return
    try:
        db.upsert_pipeline_run(conn, task_id, title=title, state=state, current_step=current_step)
        db.record_pipeline_event(conn, task_id, step, status, detail=detail, actor=actor)
    except Exception:
        logger.exception("newsletter_pipeline: visualizer tracking failed for task=%s step=%s", task_id, step)
    finally:
        conn.close()


def _track_event_only(
    database_url: Optional[str], task_id: str, step: str, status: str, *,
    detail: Optional[str] = None, actor: Optional[str] = None,
) -> None:
    url = _resolve_visualizer_db_url(database_url)
    if not url:
        return
    try:
        conn = db.get_connection(url)
    except db.DatabaseError:
        logger.debug("newsletter_pipeline: visualizer tracking skipped (no Postgres) for task=%s step=%s", task_id, step)
        return
    try:
        db.record_pipeline_event(conn, task_id, step, status, detail=detail, actor=actor)
    except Exception:
        logger.exception("newsletter_pipeline: visualizer event tracking failed for task=%s step=%s", task_id, step)
    finally:
        conn.close()


def _link_issue_if_resolvable(database_url: Optional[str], task_id: str, review_url: str) -> None:
    """Best-effort backfill of ``pipeline_runs.newsletter_issue_id`` once
    social-post-portal's own row exists (resolved via the review-page slug)."""
    url = _resolve_visualizer_db_url(database_url)
    if not url:
        return
    try:
        slug = slug_from_review_url(review_url)
    except Exception:
        return
    try:
        conn = db.get_connection(url)
    except db.DatabaseError:
        return
    try:
        issue_id = db.get_newsletter_issue_id_by_slug(conn, slug)
        if issue_id is not None:
            db.link_newsletter_issue_id(conn, task_id, issue_id)
    except Exception:
        logger.exception("newsletter_pipeline: visualizer issue-id link failed for task=%s", task_id)
    finally:
        conn.close()


def root_task_id_for_idempotency_key(idempotency_key: Optional[str]) -> Optional[str]:
    """The root Stylus task id for a (possibly refine-round) idempotency key — public so
    ``hooks.py``'s ``kanban_task_blocked`` callback can resolve the ``pipeline_runs`` key
    for a blocked refine-round task."""
    match = _REFINE_IDEMPOTENCY_RE.match(idempotency_key or "")
    return match.group("root") if match else None


def track_stylus_blocked(database_url: Optional[str], task_id: str, *, title: str, reason: Optional[str]) -> None:
    _track_run(
        database_url, task_id, title=title, state="blocked", current_step="stylus_blocked",
        step="stylus_blocked", status="error", detail=reason,
    )


def track_notification(database_url: Optional[str], task_id: str, *, ok: bool, message: str) -> None:
    """``message`` is always the actual notification text (attempted or sent), never the
    exception — see event_post_pipeline.pipeline.track_notification's docstring for why
    that distinction matters (the visualizer's own "Retry notify" button reads it back)."""
    step = "notified" if ok else "notify_failed"
    status = "ok" if ok else "error"
    _track_event_only(database_url, task_id, step, status, detail=message)


def track_stylus_retry_requested(database_url: Optional[str], task_id: str, *, ok: bool, detail: Optional[str] = None) -> None:
    _track_event_only(database_url, task_id, "stylus_retry_requested", "ok" if ok else "error", detail=detail)


def handle_intake(
    conn, ops: KanbanOps, store: NewsletterPipelineStore, *, submission_id: str, intake: dict[str, Any],
    database_url: Optional[str] = None,
) -> str:
    """Creates the Stylus handoff for a new newsletter issue. Returns the new task id."""
    issue_month = str(intake.get("issueMonth") or submission_id)
    task_id = ops.create_task(
        conn, title=f"Draft newsletter: {issue_month}", body=_wrap_untrusted(intake),
        assignee="stylus", idempotency_key=submission_id, tenant=TENANT, created_by=CREATED_BY,
    )
    _track_run(
        database_url, task_id, title=issue_month, state="running", current_step="intake_received",
        step="intake_received", status="ok",
    )
    _track_run(
        database_url, task_id, title=issue_month, state="running", current_step="stylus_drafting",
        step="stylus_drafting", status="ok",
    )
    store.upsert_issue(
        submission_id,
        {
            "root_task_id": task_id,
            "issue_month": issue_month,
            "bhante_advice_text": intake.get("bhanteAdviceText"),
            "bhante_advice_quote": intake.get("bhanteAdviceQuote"),
            "recap_summary": intake.get("recapSummary"),
            "recap_image": intake.get("recapImage"),
            "featured_announcement_text": intake.get("featuredAnnouncementText"),
            "featured_cta_label": intake.get("featuredCtaLabel"),
            "featured_cta_url": intake.get("featuredCtaUrl"),
            "programs_summary": intake.get("programsSummary"),
            "subject_line": intake.get("subjectLine"),
            "preview_text": intake.get("previewText"),
            "images": intake.get("images") or [],
            "submitter_name": intake.get("submitterName"),
            "submitter_phone": intake.get("submitterPhone"),
        },
    )
    logger.info("newsletter_pipeline: intake submission=%s -> task=%s", submission_id, task_id)
    return task_id


def _record_for_task(conn, ops: KanbanOps, store: NewsletterPipelineStore, task) -> Optional[dict[str, Any]]:
    refine = _REFINE_IDEMPOTENCY_RE.match(task.idempotency_key or "")
    root_task_id = refine.group("root") if refine else task.id
    record = store.find_by_task_id(root_task_id) or store.get_issue(root_task_id)
    if record is not None:
        return record
    root_task = ops.get_task(conn, root_task_id)
    if root_task is None:
        return None
    for comment in ops.list_comments(conn, root_task_id):
        if match := _REVIEW_URL_COMMENT_RE.search(comment.body):
            return {"root_task_id": root_task_id, "review_url": match.group(1)}
    return None


def _extract_stylus_metadata(run) -> dict[str, Any]:
    if run is None or run.outcome != "completed" or not isinstance(run.metadata, dict):
        raise PipelineError(f"Stylus run is missing or did not complete cleanly (outcome={getattr(run, 'outcome', None)!r})")
    metadata = run.metadata
    for key in DRAFT_METADATA_FIELDS:
        if not isinstance(metadata.get(key), str) or not metadata[key].strip():
            raise PipelineError(f"Stylus completion metadata missing required string field {key!r}: {metadata!r}")
    return metadata


def _draft_payload(metadata: dict[str, Any]) -> dict[str, Any]:
    return {
        "subjectLine": metadata["subject_line"],
        "previewText": metadata["preview_text"],
        "bhanteAdviceHtml": metadata["bhante_advice_html"],
        "recapHtml": metadata["recap_html"],
        "featuredAnnouncementHtml": metadata["featured_announcement_html"],
        "programsHtml": metadata["programs_html"],
        "assembledHtml": metadata["assembled_html"],
    }


def handle_stylus_completion(
    conn, ops: KanbanOps, store: NewsletterPipelineStore, review_config: ReviewClientConfig, task_id: str,
    database_url: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """The ``kanban_task_completed`` hook callback's core logic. Returns a
    dict describing what happened, or ``None`` when this completion isn't ours."""
    task = ops.get_task(conn, task_id)
    if task is None or task.tenant != TENANT or task.assignee != "stylus":
        return None
    run = ops.latest_run(conn, task_id)
    metadata = _extract_stylus_metadata(run)
    refine = _REFINE_IDEMPOTENCY_RE.match(task.idempotency_key or "")
    if refine:
        return _handle_refine_completion(conn, ops, store, review_config, task, metadata, database_url)
    return _handle_first_round_completion(conn, ops, store, review_config, task, metadata, database_url)


def _handle_first_round_completion(conn, ops, store, review_config, task, metadata: dict[str, Any], database_url: Optional[str] = None) -> dict[str, Any]:
    record = store.get_issue(task.idempotency_key) or {}
    issue_month = record.get("issue_month", task.idempotency_key)
    try:
        review_url = create_review(
            review_config, task_id=task.id, issue_month=record.get("issue_month", ""),
            bhante_advice_text=record.get("bhante_advice_text", ""), bhante_advice_quote=record.get("bhante_advice_quote"),
            recap_summary=record.get("recap_summary", ""), recap_image=record.get("recap_image"),
            featured_announcement_text=record.get("featured_announcement_text", ""),
            featured_cta_label=record.get("featured_cta_label", ""), featured_cta_url=record.get("featured_cta_url", ""),
            programs_summary=record.get("programs_summary", ""), images=record.get("images", []),
            draft=_draft_payload(metadata),
            submitter_name=record.get("submitter_name"), submitter_phone=record.get("submitter_phone"),
        )
    except ReviewClientError as exc:
        raise PipelineError(str(exc)) from exc
    ops.add_comment(conn, task.id, CREATED_BY, f"Review page: {review_url}")
    # latest_draft is read back by handle_review_action's approve path to
    # send via Brevo -- Hermes never re-fetches drafted content from the
    # portal, it's the same payload just POSTed to create_review above.
    store.upsert_issue(task.idempotency_key, {"review_url": review_url, "latest_draft": _draft_payload(metadata)})
    _track_run(
        database_url, task.id, title=issue_month, state="waiting", current_step="awaiting_review",
        step="stylus_done", status="ok", detail=review_url,
    )
    _link_issue_if_resolvable(database_url, task.id, review_url)
    return {"action": "created_review_page", "task_id": task.id, "root_task_id": task.id, "review_url": review_url}


def _handle_refine_completion(conn, ops, store, review_config, task, metadata: dict[str, Any], database_url: Optional[str] = None) -> dict[str, Any]:
    record = _record_for_task(conn, ops, store, task)
    review_url = (record or {}).get("review_url")
    if not review_url:
        raise PipelineError(f"could not recover the review-page URL for refine task {task.id}")
    slug = slug_from_review_url(review_url)
    try:
        update_review(review_config, slug=slug, draft=_draft_payload(metadata))
    except ReviewClientError as exc:
        raise PipelineError(str(exc)) from exc
    root_task_id = (record or {}).get("root_task_id") or task.idempotency_key.rsplit("-", 1)[0]
    store.upsert_issue(root_task_id, {"round_task_id": task.id, "latest_draft": _draft_payload(metadata)})
    _track_run(
        database_url, root_task_id, title=(record or {}).get("issue_month", root_task_id), state="waiting",
        current_step="awaiting_review", step="stylus_done", status="ok", detail=review_url,
    )
    _link_issue_if_resolvable(database_url, root_task_id, review_url)
    return {"action": "updated_review_page", "task_id": task.id, "root_task_id": root_task_id, "review_url": review_url}


# --- Reacting to a review-page action ----------------------------------------------------


def _round_tasks(conn, ops: KanbanOps, root_task_id: str) -> list:
    """Every redraft round already created under ``root_task_id`` — queried
    against Kanban directly (never the local store), same pattern as
    event_post_pipeline.pipeline._platform_round_tasks, so the next round
    number is always correct even after a store loss or restart."""
    prefix = f"{root_task_id}-r"
    tasks = ops.list_tasks(conn, tenant=TENANT, assignee="stylus", include_archived=True)
    matches = [t for t in tasks if (t.idempotency_key or "").startswith(prefix)]
    return sorted(int((t.idempotency_key or "").rsplit("r", 1)[-1]) for t in matches)


def _maybe_send_via_brevo(
    store: NewsletterPipelineStore, review_config: ReviewClientConfig, brevo_config: Optional[BrevoConfig],
    *, root_task_id: str,
) -> Optional[dict[str, Any]]:
    """Sends the approved issue via Brevo and records the result — only when
    ``brevo_config`` is both passed (feature-flagged on, see
    ``extra.brevo_send_enabled`` / ``adapter.py``) and fully configured
    (real API key + sender + list ids). Returns a dict describing the
    outcome for the caller to comment/notify with, or ``None`` when the
    feature is simply off (the expected state until Amila has real Brevo
    access — see brevo_client.py's module docstring). Raises
    :class:`PipelineError` if the feature is on but the send itself fails —
    per the plan's "erroring loudly rather than silently no-opping" decision.
    """
    if brevo_config is None:
        return None
    record = store.get_issue(root_task_id) or {}
    review_url = record.get("review_url")
    draft = record.get("latest_draft")
    if not review_url or not draft:
        raise PipelineError(f"approved but no stored draft/review_url found for task {root_task_id!r} — cannot send")
    slug = slug_from_review_url(review_url)
    try:
        campaign_id = brevo_client.create_and_send_campaign(
            brevo_config, subject=draft["subjectLine"], html_content=draft["assembledHtml"],
            campaign_name=f"Newsletter — {record.get('issue_month', slug)}",
        )
    except BrevoClientError as exc:
        raise PipelineError(f"Brevo send failed: {exc}") from exc
    try:
        record_send(review_config, slug=slug, brevo_campaign_id=str(campaign_id))
    except ReviewClientError as exc:
        # The send itself succeeded -- don't fail the approve action over a
        # bookkeeping write, but this must not be silent either.
        logger.error("newsletter_pipeline: Brevo send succeeded (campaign=%s) but send-log recording failed: %s", campaign_id, exc)
    return {"brevo_campaign_id": campaign_id}


def handle_review_action(
    conn, ops: KanbanOps, store: NewsletterPipelineStore, review_config: ReviewClientConfig, *, task_id: str, action: str, comment: str,
    database_url: Optional[str] = None, brevo_config: Optional[BrevoConfig] = None,
) -> dict[str, Any]:
    """A review-page button click. ``task_id`` is always the *root* Stylus task."""
    root_task = ops.get_task(conn, task_id)
    if root_task is None:
        raise PipelineError(f"review action referenced unknown task {task_id!r}")

    if action in ("approved", "rejected"):
        outcome_line = f"Newsletter {action.upper()} via review page: {comment}" if comment else f"Newsletter {action.upper()} via review page"
        ops.add_comment(conn, root_task.id, CREATED_BY, outcome_line)
        # Deliberately does NOT touch pipeline_runs' state/current_step: social-post-portal
        # owns deciding when a newsletter issue is fully done (same ownership boundary as
        # event_post_pipeline) — this is only the timeline event.
        _track_event_only(database_url, task_id, "action_taken", "ok", detail=action)
        result: dict[str, Any] = {"action": action, "task_id": root_task.id}
        if action == "approved":
            send_result = _maybe_send_via_brevo(store, review_config, brevo_config, root_task_id=task_id)
            if send_result is not None:
                ops.add_comment(conn, root_task.id, CREATED_BY, f"Sent via Brevo (campaign {send_result['brevo_campaign_id']})")
                result["brevo_campaign_id"] = send_result["brevo_campaign_id"]
        return result

    if action == "refine":
        record = store.get_issue(task_id) or {}
        previous_metadata = _previous_metadata(ops.latest_run(conn, task_id))
        next_round = (max(_round_tasks(conn, ops, task_id), default=0)) + 1
        idempotency_key = f"{task_id}-r{next_round}"
        body = _refine_task_body(record=record, previous_metadata=previous_metadata, comment=comment)
        new_task_id = ops.create_task(
            conn, title=f"Redraft newsletter (round {next_round}): {root_task.title.removeprefix('Draft newsletter: ')}",
            body=body, assignee="stylus", idempotency_key=idempotency_key, tenant=TENANT, created_by=CREATED_BY,
        )
        store.upsert_issue(task_id, {"round_task_id": new_task_id})
        _track_run(
            database_url, task_id, title=record.get("issue_month", task_id), state="running",
            current_step="refine_drafting", step="refine_dispatched", status="ok", detail=f"round {next_round}",
        )
        return {"action": "refine_dispatched", "task_id": new_task_id, "round": next_round}

    raise PipelineError(f"unknown review action {action!r}")


def _previous_metadata(run) -> dict[str, Any]:
    if run is None or not isinstance(run.metadata, dict):
        return {}
    return run.metadata


def _refine_task_body(*, record: dict[str, Any], previous_metadata: dict[str, Any], comment: str) -> str:
    return "\n\n".join(
        part for part in (
            "<untrusted_submission>",
            f"Issue month: {record.get('issue_month', '')}",
            f"Bhante's advice text: {record.get('bhante_advice_text', '')}",
            f"Recap summary: {record.get('recap_summary', '')}",
            f"Featured announcement text: {record.get('featured_announcement_text', '')}",
            f"Programs summary: {record.get('programs_summary', '')}",
            "</untrusted_submission>",
            "Previous draft:\n" + "\n".join(f"{k}: {v}" for k, v in previous_metadata.items()),
            f"Requested change (reviewer's own words):\n{comment}",
            "Redraft the full issue (every Section 10 slot) incorporating this change — "
            "a newsletter issue is reviewed as a whole, not per-section.",
        ) if part
    )
