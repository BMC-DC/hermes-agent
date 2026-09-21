"""Agent-callable tools so Vidu (the general WhatsApp-facing orchestrator) can query
and trigger this plugin's deterministic pipeline — without drafting anything itself
(Stylus stays the only LLM-drafting step, exactly as it already is for form
submissions) and without an extra ``delegate_task`` LLM hop (plain ``register_tool``
function calls, per this plugin's established zero-extra-LLM-cost philosophy).

Two tools, one toolset (``event_post_pipeline``):

- ``social_post_status`` — read-only. Recent pipeline runs (state, current step,
  review link) from the same ``pipeline_runs`` table the portal-side visualizer
  already reads. Answers "what's the status", "show me past posts".
- ``social_post_submit`` — write. Runs the exact same deterministic intake step
  (``pipeline.handle_intake``) the HTTP ``/intake`` route runs for a form
  submission — creates the Stylus handoff task, nothing more. Lets a submission
  start from a WhatsApp chat message instead of the intake form.

Config is reloaded fresh from ``config.yaml`` on every call rather than held on a
long-lived object — same reasoning as ``hooks.py``'s own docstring: a tool call
can't assume it's running in the same process as a live ``EventPostPipelineAdapter``
instance.

Image handling (``social_post_submit``): accepts already-public image URLs only
(``image_urls``). It deliberately does NOT accept raw WhatsApp media attachments —
turning a locally-cached WhatsApp attachment into a public URL would mean either
giving Hermes its own Cloudinary credentials or adding a new upload endpoint on
social-post-portal, both real integration decisions nobody has made yet. Chat
submissions also skip the intake form's lower-third photo compositing (a
browser-side-only nicety today) — plain photos, same as any other URL. See this
plugin's README/plan doc for the open follow-up if BMC wants full parity.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Optional

from plugins.platforms.event_post_pipeline import db, pipeline
from plugins.platforms.event_post_pipeline.store import EventPostPipelineStore, resolve_store_path
from tools.registry import tool_error, tool_result

logger = logging.getLogger("plugins.platforms.event_post_pipeline")


def _load_pipeline_extra() -> dict:
    from hermes_cli.config import load_config

    platforms = (load_config() or {}).get("platforms") or {}
    return dict((platforms.get("event_post_pipeline") or {}).get("extra") or {})


def check_pipeline_tools_available() -> bool:
    """Gates both tools' visibility: only meaningful once a profile has actually
    opted into the ``event_post_pipeline`` platform (``config.yaml``'s
    ``platforms.event_post_pipeline`` block) — same "configured, not just
    installed" convention ``adapter.py``'s own ``check_fn`` follows."""
    return bool(_load_pipeline_extra())


SOCIAL_POST_STATUS_SCHEMA = {
    "name": "social_post_status",
    "description": (
        "Look up the status of social-media event posts going through the pipeline: "
        "recent submissions, which step each is on (drafting / awaiting review / "
        "blocked), and the review-page link. Read-only — use social_post_submit to "
        "start a new one. Optionally filter by a keyword from the event title."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Optional keyword to filter by (matched against the event title). Omit to list the most recent activity.",
            },
            "limit": {
                "type": "integer",
                "description": "Max number of runs to return (default 10, max 25).",
            },
        },
        "required": [],
    },
}

SOCIAL_POST_SUBMIT_SCHEMA = {
    "name": "social_post_submit",
    "description": (
        "Start a new event post submission directly from chat, skipping the intake "
        "form. Creates the Stylus drafting handoff exactly like a form submission "
        "does — this tool never drafts or edits the post content itself; Stylus "
        "still writes the actual FB/IG/blog copy, and a review link comes back via "
        "the usual WhatsApp notification once it's ready. Images must already be "
        "public URLs (e.g. a link the user shared) — a raw photo attached in chat "
        "can't be used here yet."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "description": {"type": "string", "description": "What the event/post is about, in the submitter's own words."},
            "date": {"type": "string", "description": "The event date, as given (free text, e.g. '2026-09-27' or 'this Saturday')."},
            "quote": {"type": "string", "description": "An optional quote to feature in the post."},
            "image_urls": {
                "type": "array", "items": {"type": "string"},
                "description": "Zero or more already-public image URLs, in display order (first one becomes the lead photo, last becomes the closing photo).",
            },
            "submitter_name": {"type": "string", "description": "Name of the person submitting, if known."},
            "submitter_phone": {"type": "string", "description": "WhatsApp number of the person submitting, if known (E.164-ish, whatever the chat gives)."},
        },
        "required": ["description"],
    },
}


def _status_row(run: dict[str, Any]) -> dict[str, Any]:
    return {
        "task_id": run.get("task_id"),
        "title": run.get("title"),
        "state": run.get("state"),
        "current_step": run.get("current_step"),
        "updated_at": str(run.get("updated_at") or ""),
    }


def social_post_status_handler(args: dict, **_kwargs: Any) -> str:
    extra = _load_pipeline_extra()
    if not extra:
        return tool_error("event_post_pipeline is not configured for this profile")
    db_url = db.resolve_database_url(extra)
    if not db_url:
        return tool_error(
            "no SPP_DATABASE_URL configured for this profile — pipeline status tracking isn't wired up, "
            "so past-run history isn't available here"
        )
    query = str(args.get("query") or "").strip() or None
    try:
        limit = max(1, min(int(args.get("limit") or 10), 25))
    except (TypeError, ValueError):
        limit = 10
    try:
        conn = db.get_connection(db_url)
    except db.DatabaseError as exc:
        return tool_error(f"could not reach the pipeline status database: {exc}")
    try:
        runs = db.list_recent_pipeline_runs(conn, limit=limit, query=query)
    except Exception as exc:
        logger.exception("event_post_pipeline: social_post_status query failed")
        return tool_error(f"status query failed: {exc}")
    finally:
        conn.close()
    return tool_result(runs=[_status_row(r) for r in runs], count=len(runs))


def _build_event_pack(args: dict) -> dict[str, Any]:
    urls = [str(u).strip() for u in (args.get("image_urls") or []) if str(u or "").strip()]
    first = {"finalUrl": urls[0]} if urls else {}
    last = {"finalUrl": urls[-1]} if len(urls) > 1 else {}
    rest = [{"finalUrl": u} for u in urls[1:-1]] if len(urls) > 2 else []
    return {
        "date": str(args.get("date") or ""),
        "description": str(args.get("description") or ""),
        "quote": str(args.get("quote") or ""),
        "first": first, "last": last, "rest": rest,
        "submitterName": str(args.get("submitter_name") or ""),
        "submitterPhone": str(args.get("submitter_phone") or ""),
    }


def social_post_submit_handler(args: dict, **_kwargs: Any) -> str:
    extra = _load_pipeline_extra()
    if not extra:
        return tool_error("event_post_pipeline is not configured for this profile")
    description = str(args.get("description") or "").strip()
    if not description:
        return tool_error("description is required")

    event_pack = _build_event_pack(args)
    submission_id = f"chat-{uuid.uuid4().hex[:12]}"
    board: Optional[str] = extra.get("board")
    db_url = db.resolve_database_url(extra)
    store = EventPostPipelineStore(resolve_store_path(extra.get("store_path")))

    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    conn = kbc.connect(board=board)
    try:
        ops = pipeline.KanbanOps(kb)
        try:
            task_id = pipeline.handle_intake(
                conn, ops, store, submission_id=submission_id, event_pack=event_pack, database_url=db_url,
            )
        except pipeline.PipelineError as exc:
            logger.error("event_post_pipeline: social_post_submit intake failed for submission=%s: %s", submission_id, exc)
            return tool_error(f"could not start the pipeline: {exc}")
    finally:
        conn.close()
    return tool_result(
        status="accepted", task_id=task_id, submission_id=submission_id,
        note="Drafting has started. A review link will be posted to the group once Stylus finishes — no need to check back manually.",
    )
