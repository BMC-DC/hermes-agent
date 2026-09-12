"""The ``kanban_task_completed`` lifecycle hook callback.

Registered once, at plugin-discovery time, in ``register(ctx)`` — and plugin
discovery runs in **every** Hermes process that touches Kanban, including
the dispatcher-spawned worker subprocess a Stylus task actually completes
in (confirmed: ``hermes_cli/plugins.py``'s hook comment — "completed/blocked
fire in the WORKER"). So unlike ``adapter.py``'s HTTP routes, this callback
can never assume a live ``EventPostPipelineAdapter`` instance exists in its
process (the gateway may not even be running here) — it reloads its own
config straight from ``config.yaml`` on every call, exactly like
``whatsapp_notify.py`` does for the same reason.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from plugins.platforms.event_post_pipeline import pipeline, security, whatsapp_notify
from plugins.platforms.event_post_pipeline.review_client import ReviewClientConfig
from plugins.platforms.event_post_pipeline.store import EventPostPipelineStore, resolve_store_path

logger = logging.getLogger("plugins.platforms.event_post_pipeline")


def _load_pipeline_extra() -> dict:
    from hermes_cli.config import load_config

    platforms = (load_config() or {}).get("platforms") or {}
    return dict((platforms.get("event_post_pipeline") or {}).get("extra") or {})


def on_kanban_task_completed(*, task_id: str, **_kwargs: Any) -> None:
    extra = _load_pipeline_extra()
    if not extra:
        return  # plugin not configured in this profile — nothing to react to
    board: Optional[str] = extra.get("board")
    review_config = ReviewClientConfig(
        base_url=str(extra.get("review_base_url", "https://bmcposts.vercel.app")).rstrip("/"),
        create_secret=security.resolve_review_create_secret(extra),
    )
    store = EventPostPipelineStore(resolve_store_path(extra.get("store_path")))

    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    conn = kbc.connect(board=board)
    try:
        ops = pipeline.KanbanOps(kb)
        try:
            result = pipeline.handle_stylus_completion(conn, ops, store, review_config, task_id)
        except pipeline.PipelineError as exc:
            logger.error("[event_post_pipeline] task_completed handling failed for task=%s: %s", task_id, exc)
            ops.add_comment(conn, task_id, pipeline.CREATED_BY,
                            f"event-post-pipeline: could not process this completion automatically: {exc}")
            return
    finally:
        conn.close()

    if result is None:
        return  # not one of this pipeline's tasks
    message = _notification_message(result)
    if message:
        import asyncio
        asyncio.run(whatsapp_notify.send_whatsapp_link(message))


def _notification_message(result: dict) -> Optional[str]:
    if result["action"] == "created_review_page":
        return f"New event post ready for review: {result['review_url']}"
    if result["action"] == "updated_review_page":
        return f"Refined {result['platform'].upper()} draft ready for review: {result['review_url']}"
    return None
