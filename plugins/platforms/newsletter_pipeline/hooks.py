"""The ``kanban_task_completed`` lifecycle hook callback for the newsletter
pipeline — mirrors ``event_post_pipeline.hooks`` exactly in structure
(registered at plugin-discovery time, reloads its own config on every call
via ``root_config.load_pipeline_extra()`` since it may run inside a
Kanban-dispatcher-spawned worker with no live gateway — see that module's
docstring for the full rationale, unchanged here).
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from plugins.platforms.newsletter_pipeline import db, pipeline, security, whatsapp_notify
from plugins.platforms.newsletter_pipeline.review_client import ReviewClientConfig
from plugins.platforms.newsletter_pipeline.root_config import load_pipeline_extra as _load_pipeline_extra
from plugins.platforms.newsletter_pipeline.store import NewsletterPipelineStore, resolve_store_path

logger = logging.getLogger("plugins.platforms.newsletter_pipeline")


def on_kanban_task_completed(*, task_id: str, **_kwargs: Any) -> None:
    extra = _load_pipeline_extra()
    if not extra:
        return  # plugin not configured in this profile — nothing to react to
    board: Optional[str] = extra.get("board")
    db_url = db.resolve_database_url(extra)
    review_config = ReviewClientConfig(
        base_url=str(extra.get("review_base_url", "https://spp.buddhameditationdc.org")).rstrip("/"),
        create_secret=security.resolve_review_create_secret(extra),
    )
    store = NewsletterPipelineStore(resolve_store_path(extra.get("store_path")))

    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    conn = kbc.connect(board=board)
    try:
        ops = pipeline.KanbanOps(kb)
        try:
            result = pipeline.handle_stylus_completion(conn, ops, store, review_config, task_id, database_url=db_url)
        except pipeline.PipelineError as exc:
            logger.error("[newsletter_pipeline] task_completed handling failed for task=%s: %s", task_id, exc)
            ops.add_comment(conn, task_id, pipeline.CREATED_BY,
                            f"newsletter-pipeline: could not process this completion automatically: {exc}")
            return
    finally:
        conn.close()

    if result is None:
        return  # not one of this pipeline's tasks
    message = _notification_message(result)
    if not message:
        return

    track_task_id = result.get("root_task_id") or result["task_id"]

    try:
        _run_async(lambda: whatsapp_notify.send_whatsapp_link(message))
    except Exception:
        logger.exception(
            "[newsletter_pipeline] notification failed for task=%s (result=%s) — this would "
            "otherwise be silently swallowed at DEBUG level by "
            "hermes_cli.kanban_db._fire_kanban_lifecycle_hook", task_id, result,
        )
        pipeline.track_notification(db_url, track_task_id, ok=False, message=message)
        return
    pipeline.track_notification(db_url, track_task_id, ok=True, message=message)


def on_kanban_task_blocked(*, task_id: str, assignee: Optional[str] = None, reason: Optional[str] = None, **_kwargs: Any) -> None:
    """Registered for ``kanban_task_blocked`` — mirrors
    ``event_post_pipeline.hooks.on_kanban_task_blocked``, including visualizer
    tracking (wired up alongside pipeline_runs.newsletter_issue_id — see
    extra/plans/newsletter/migrations/0002_newsletter_visualizer.sql)."""
    if assignee != "stylus":
        return
    extra = _load_pipeline_extra()
    if not extra:
        return
    db_url = db.resolve_database_url(extra)

    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    conn = kbc.connect(board=extra.get("board"))
    try:
        task = kb.get_task(conn, task_id)
    finally:
        conn.close()
    if task is None or task.tenant != pipeline.TENANT:
        return

    root_task_id = pipeline.root_task_id_for_idempotency_key(task.idempotency_key) or task_id
    try:
        pipeline.track_stylus_blocked(db_url, root_task_id, title=task.title, reason=reason)
    except Exception:
        logger.exception("[newsletter_pipeline] visualizer blocked-tracking failed for task=%s", task_id)

    try:
        alert = f"⚠️ Stylus got stuck on \"{task.title}\": {reason or 'no reason given'}. Task id: {task_id}"
        _run_async(lambda: whatsapp_notify.send_whatsapp_link(alert))
    except Exception:
        logger.exception("[newsletter_pipeline] blocked-task alert send failed for task=%s", task_id)


def _run_async(coro_factory) -> None:
    """Runs an async call site's coroutine safely regardless of whether this
    thread already has a running event loop — identical to
    event_post_pipeline.hooks._run_async."""
    import asyncio

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(coro_factory())
        return
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(asyncio.run, coro_factory()).result()


def _notification_message(result: dict) -> Optional[str]:
    if result["action"] == "created_review_page":
        return f"New newsletter issue ready for review: {result['review_url']}"
    if result["action"] == "updated_review_page":
        return f"Refined newsletter draft ready for review: {result['review_url']}"
    return None
