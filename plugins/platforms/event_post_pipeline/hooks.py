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

from plugins.platforms.event_post_pipeline import db, pipeline, security, whatsapp_notify
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
    db_url = db.resolve_database_url(extra)
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
            result = pipeline.handle_stylus_completion(conn, ops, store, review_config, task_id, database_url=db_url)
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
    if not message:
        return

    track_task_id = result.get("root_task_id") or result["task_id"]

    # This whole block is deliberately wrapped: the caller (Hermes's own
    # _fire_kanban_lifecycle_hook) swallows ANY exception from a hook callback
    # at logging.DEBUG level ("failures are swallowed so an observer can
    # never break a transition") — invisible in a normal production log at
    # INFO level. A notification failure here must never look like silence;
    # log it ourselves at ERROR before it disappears upstream.
    try:
        # Decided 2026-09-21: every pipeline notification goes to the shared
        # social-media WhatsApp group, never an individual curator's DM — the
        # team wants shared visibility into all activity, not fragmented
        # per-person messages. "Stylus finished" and "review link ready" are
        # deliberately sent as a single message here for the same reason
        # ``adapter.py``'s intake notification merges its own pair: both fire
        # from this one hook callback at the exact same instant with
        # overlapping information.
        _run_async(lambda: whatsapp_notify.send_whatsapp_link(message))
    except Exception as exc:
        logger.exception(
            "[event_post_pipeline] notification failed for task=%s (result=%s) — "
            "this would otherwise be silently swallowed at DEBUG level by "
            "hermes_cli.kanban_db._fire_kanban_lifecycle_hook", task_id, result,
        )
        pipeline.track_notification(db_url, track_task_id, ok=False, message=message)
        return
    pipeline.track_notification(db_url, track_task_id, ok=True, message=message)


def on_kanban_task_blocked(*, task_id: str, assignee: Optional[str] = None, reason: Optional[str] = None, **_kwargs: Any) -> None:
    """Registered for the ``kanban_task_blocked`` lifecycle hook (see
    ``hermes_cli/plugins.py``'s ``VALID_HOOKS`` and ``hermes_cli/kanban_db.py``'s
    ``block_task``, which fires it via ``_fire_task_hook`` with ``reason=reason`` and
    ``assignee`` already resolved from the blocked row). Stylus blocking is not something
    this plugin's own code triggers (unlike ``kanban_task_completed``) — it's purely a
    Kanban-dispatcher-level event this callback observes for visualizer visibility."""
    if assignee != "stylus":
        return  # cheap short-circuit before loading config or touching Kanban at all
    extra = _load_pipeline_extra()
    if not extra:
        return
    board: Optional[str] = extra.get("board")
    db_url = db.resolve_database_url(extra)
    if not db_url:
        return  # nothing to track without a visualizer database configured

    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    conn = kbc.connect(board=board)
    try:
        task = kb.get_task(conn, task_id)
    finally:
        conn.close()
    if task is None or task.tenant != pipeline.TENANT:
        return  # not ours — same tenant-filtering pattern as on_kanban_task_completed

    root_task_id = pipeline.root_task_id_for_idempotency_key(task.idempotency_key) or task_id
    try:
        pipeline.track_stylus_blocked(db_url, root_task_id, title=task.title, reason=reason)
    except Exception:
        logger.exception("[event_post_pipeline] visualizer blocked-tracking failed for task=%s", task_id)

    # Decided 2026-09-21: unlike a failed notification (which already had *something*
    # attempted and can just be retried), a blocked Stylus task has told nobody anything
    # yet — this is the one failure mode with no existing alert at all. Tell the group,
    # with a direct link to the visualizer so someone can open it and hit Retry.
    try:
        review_base_url = str(extra.get("review_base_url", "https://bmcposts.vercel.app")).rstrip("/")
        link = f"{review_base_url}/visualizer?taskId={root_task_id}"
        alert = f"⚠️ Stylus got stuck on \"{task.title}\": {reason or 'no reason given'}. Continue here: {link}"
        _run_async(lambda: whatsapp_notify.send_whatsapp_link(alert))
    except Exception:
        logger.exception("[event_post_pipeline] blocked-task alert send failed for task=%s", task_id)


def _run_async(coro_factory) -> None:
    """Runs an async call site's coroutine safely regardless of whether this
    thread already has a running event loop.

    This hook fires inside a Kanban worker process whose execution context
    this plugin cannot assume anything about (per this module's docstring —
    "the gateway may not even be running here"). A bare ``asyncio.run(...)``
    raises ``RuntimeError: asyncio.run() cannot be called from a running
    event loop`` if the calling thread already has one active — and per the
    swallowing behavior documented above, that error was disappearing
    completely rather than surfacing anywhere. Detect that case and run the
    coroutine on a dedicated thread with its own fresh loop instead of
    nesting.
    """
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
        return f"New event post ready for review: {result['review_url']}"
    if result["action"] == "updated_review_page":
        return f"Refined {result['platform'].upper()} draft ready for review: {result['review_url']}"
    return None
