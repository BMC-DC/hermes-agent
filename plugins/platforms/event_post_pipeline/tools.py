"""Agent-callable tools so Vidu (the general WhatsApp-facing orchestrator) can query
and unstick this plugin's deterministic pipeline — without drafting anything itself
(Stylus stays the only LLM-drafting step) and without an extra ``delegate_task`` LLM
hop (plain ``register_tool`` function calls, per this plugin's established
zero-extra-LLM-cost philosophy).

Two tools, one toolset (``event_post_pipeline``):

- ``social_post_status`` — read-only. Recent pipeline runs (state, current step,
  review link) from the same ``pipeline_runs`` table the portal-side visualizer
  already reads. Answers "what's the status", "show me past posts".
- ``social_post_retry`` — the chat equivalent of the review-page visualizer's own
  "Retry" button: unblocks a stuck Stylus drafting task, or resends a WhatsApp
  notification that failed to send. Never touches drafting/review content itself.

Deliberately NOT here: a "submit a new post from chat" tool. Starting a real
submission means handling photos (the intake form's Cloudinary upload + lower-third
compositing), which is the publisher's job, not something to reimplement here with a
degraded image story. Vidu's role for a new submission is to tell the user to use the
intake form — see the skill file, not a tool.

Also NOT here: anything for handing off writing work to Stylus in general. That's
already a fully generic capability via the ``kanban`` toolset (``kanban_create`` with
``assignee="stylus"``) and the ``subagent-orchestration`` skill — this plugin adds
nothing on top of it, for event posts or any other writing task.

Config is reloaded fresh from ``config.yaml`` on every call rather than held on a
long-lived object — same reasoning as ``hooks.py``'s own docstring: a tool call
can't assume it's running in the same process as a live ``EventPostPipelineAdapter``
instance.
"""

from __future__ import annotations

import logging
from typing import Any

from plugins.platforms.event_post_pipeline import db, pipeline, whatsapp_notify
from plugins.platforms.event_post_pipeline.hooks import _run_async
from plugins.platforms.event_post_pipeline.root_config import load_pipeline_extra as _load_pipeline_extra
from tools.registry import tool_error, tool_result

logger = logging.getLogger("plugins.platforms.event_post_pipeline")


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
        "blocked), and the review-page link. Read-only. To start a new submission, "
        "tell the user to use the intake form — that's not something you can do from "
        "chat. Optionally filter by a keyword from the event title."
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

SOCIAL_POST_RETRY_SCHEMA = {
    "name": "social_post_retry",
    "description": (
        "Unstick a social-media post pipeline run: either retries a Stylus drafting "
        "task that got stuck 'blocked', or resends a WhatsApp notification that "
        "previously failed to send. This is the exact same pair of actions the "
        "review-page visualizer's own 'Retry' button performs — the chat equivalent, "
        "not a new capability. Never drafts or edits content. Use social_post_status "
        "first to find the task_id of a blocked/stuck run."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "The Kanban task id to act on (social_post_status's output gives you this)."},
            "kind": {
                "type": "string", "enum": ["stylus_blocked", "notify"],
                "description": "'stylus_blocked' unblocks a stuck Stylus drafting task so the dispatcher picks it back up. 'notify' resends a WhatsApp notification that previously failed.",
            },
            "message": {"type": "string", "description": "Required when kind='notify': the exact message text to resend."},
        },
        "required": ["task_id", "kind"],
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


def social_post_retry_handler(args: dict, **_kwargs: Any) -> str:
    extra = _load_pipeline_extra()
    if not extra:
        return tool_error("event_post_pipeline is not configured for this profile")
    task_id = str(args.get("task_id") or "").strip()
    if not task_id:
        return tool_error("task_id is required")
    kind = str(args.get("kind") or "").strip()
    if kind not in ("stylus_blocked", "notify"):
        return tool_error("kind must be 'stylus_blocked' or 'notify'")

    db_url = db.resolve_database_url(extra)
    if kind == "notify":
        message = str(args.get("message") or "").strip()
        if not message:
            return tool_error("message is required when kind='notify'")
        try:
            _run_async(lambda: whatsapp_notify.send_whatsapp_link(message))
        except Exception as exc:
            logger.exception("event_post_pipeline: social_post_retry notify failed for task=%s", task_id)
            pipeline.track_notification(db_url, task_id, ok=False, message=message)
            return tool_error(f"notify_failed: {exc}")
        pipeline.track_notification(db_url, task_id, ok=True, message=message)
        return tool_result(status="sent", task_id=task_id)

    board = extra.get("board")
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    conn = kbc.connect(board=board)
    try:
        unblocked = kb.unblock_task(conn, task_id)
    finally:
        conn.close()
    if unblocked:
        pipeline.track_stylus_retry_requested(db_url, task_id, ok=True)
        return tool_result(status="retrying", task_id=task_id)
    pipeline.track_stylus_retry_requested(db_url, task_id, ok=False, detail="task was not in a blocked/scheduled state")
    return tool_error("task is not currently blocked or scheduled — it may have already been retried or resolved")
