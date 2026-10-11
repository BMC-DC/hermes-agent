"""The ``kanban_task_completed`` lifecycle hook callback.

Registered once, at plugin-discovery time, in ``register(ctx)`` — and plugin
discovery runs in **every** Hermes process that touches Kanban, including
the dispatcher-spawned worker subprocess a Stylus task actually completes
in (confirmed: ``hermes_cli/plugins.py``'s hook comment — "completed/blocked
fire in the WORKER"). So unlike ``adapter.py``'s HTTP routes, this callback
can never assume a live ``EventPostPipelineAdapter`` instance exists in its
process (the gateway may not even be running here) — it reloads its own
config on every call via ``root_config.load_pipeline_extra()``, which always
resolves the root/default profile's ``config.yaml`` regardless of which
profile's process this callback happens to be running in — see that
module's docstring for why (a real bug this fixes, not just style).
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from plugins.platforms.event_post_pipeline import db, pipeline, research, security, whatsapp_notify
from plugins.platforms.event_post_pipeline.review_client import ReviewClientConfig
from plugins.platforms.event_post_pipeline.root_config import load_pipeline_extra as _load_pipeline_extra
from plugins.platforms.event_post_pipeline.store import EventPostPipelineStore, resolve_store_path

logger = logging.getLogger("plugins.platforms.event_post_pipeline")


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
    store = EventPostPipelineStore(resolve_store_path(extra.get("store_path")))

    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    conn = kbc.connect(board=board)
    try:
        ops = pipeline.KanbanOps(kb)
        try:
            # Research task (WebMaster): events/bookkeeping only. Never a review page, never a WhatsApp message.
            research_result = research.handle_research_completion(
                conn, ops, store, task_id, tenant=pipeline.TENANT, author=pipeline.CREATED_BY, extra=extra,
                database_url=db_url, shadow=_shadow_recorder(extra, store),
            )
        except Exception:  # noqa: BLE001 - a research problem must never break a completion
            logger.exception("[event_post_pipeline] research completion handling failed for task=%s", task_id)
            return
        if research_result is not None:
            return
        try:
            result = pipeline.handle_stylus_completion(
                conn, ops, store, review_config, task_id, database_url=db_url, shadow=_shadow_recorder(extra, store),
            )
        except pipeline.PipelineError as exc:
            logger.error("[event_post_pipeline] task_completed handling failed for task=%s: %s", task_id, exc)
            ops.add_comment(conn, task_id, pipeline.CREATED_BY,
                            f"event-post-pipeline: could not process this completion automatically: {exc}")
            return
    finally:
        conn.close()

    if result is None:
        return  # not one of this pipeline's tasks
    _send_result_notification(result, db_url, task_id)


def _send_result_notification(result: dict, db_url: Optional[str], task_id: str) -> None:
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


def _shadow_recorder(extra: dict, store: EventPostPipelineStore):
    """Task 08 shadow mode: ``None`` (the common case: flags off) or a log-only recorder. Never raises."""
    try:
        from plugins.platforms.event_post_pipeline.shadow import ShadowRecorder
        return ShadowRecorder.from_extra(extra, store.path)
    except Exception:  # noqa: BLE001
        return None


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
        review_base_url = str(extra.get("review_base_url", "https://spp.buddhameditationdc.org")).rstrip("/")
        link = f"{review_base_url}/visualizer?taskId={root_task_id}"
        alert = f"⚠️ Stylus got stuck on \"{task.title}\": {reason or 'no reason given'}. Continue here: {link}"
        # 2026-09-26: WhatsApp group no longer gets lifecycle/status updates — only
        # review-link and review-reminder messages still go to the group. Left commented
        # (not deleted) in case this needs reverting.
        # _run_async(lambda: whatsapp_notify.send_whatsapp_link(alert))
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


def run_join_sweep(now: Optional[float] = None) -> list[dict]:
    """Split-intake join sweep for the dispatcher-tick hook (``on_kanban_dispatch_tick``, which also runs the research sweep): loads config fresh,
    runs ``pipeline.sweep_join`` and sends the ONE WhatsApp message for every page it creates. Never raises."""
    import time
    try:
        extra = _load_pipeline_extra()
        if not extra:
            return []
        db_url = db.resolve_database_url(extra)
        review_config = ReviewClientConfig(
            base_url=str(extra.get("review_base_url", "https://spp.buddhameditationdc.org")).rstrip("/"),
            create_secret=security.resolve_review_create_secret(extra),
        )
        store = EventPostPipelineStore(resolve_store_path(extra.get("store_path")))
        from hermes_cli import kanban_db as kb
        from hermes_cli import kanban_db_connect as kbc

        conn = kbc.connect(board=extra.get("board"))
        try:
            results = pipeline.sweep_join(
                conn, pipeline.KanbanOps(kb), store, review_config, now=time.time() if now is None else now,
                blog_wait_seconds=pipeline.blog_wait_seconds(extra), database_url=db_url,
                shadow=_shadow_recorder(extra, store),
            )
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        logger.exception("[event_post_pipeline] join sweep failed")
        return []
    for result in results:
        _send_result_notification(result, db_url, result["task_id"])
    return results


def run_research_sweep(now: Optional[float] = None) -> list[dict]:
    """Research fail-open sweep for the dispatcher-tick hook: retires blocked/overdue/never-spawned research tasks so the blog
    task starts without a brief. Not gated by the research flag. Loads config fresh; never raises."""
    import time
    try:
        extra = _load_pipeline_extra()
        if not extra:
            return []
        db_url = db.resolve_database_url(extra)
        store = EventPostPipelineStore(resolve_store_path(extra.get("store_path")))
        from hermes_cli import kanban_db as kb
        from hermes_cli import kanban_db_connect as kbc

        conn = kbc.connect(board=extra.get("board"))
        try:
            return research.sweep(
                conn, pipeline.KanbanOps(kb), store, now=time.time() if now is None else now, tenant=pipeline.TENANT,
                author=pipeline.CREATED_BY, default_deadline_s=research.ResearchConfig.from_extra(extra).deadline_s, database_url=db_url,
            )
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        logger.exception("[event_post_pipeline] research sweep failed")
        return []


def on_kanban_dispatch_tick(*, board: Optional[str] = None, profile_name: Optional[str] = None, dry_run: bool = False,
                            outcome: Optional[str] = None, result: Any = None, **_kwargs: Any) -> None:
    """Registered for ``on_kanban_dispatch_tick`` (``hermes_cli/kanban_db.py::_fire_dispatch_tick_hook``): once per dispatcher
    tick, after the dispatch lock is released, in the dispatcher process (the gateway). Runs the research fail-open sweep and the
    split-intake join sweep. Observer only: each sweep is isolated and neither can raise."""
    if dry_run:
        return
    run_research_sweep()
    run_join_sweep()


def _background(fn) -> None:
    """Runs ``fn`` on a daemon thread (the claim hook fires inside the board's dispatch lock; it must return at once)."""
    import threading
    threading.Thread(target=fn, name="bmc-epp-claim-hook", daemon=True).start()


def on_kanban_task_claimed(*, task_id: str, assignee: Optional[str] = None, **_kwargs: Any) -> None:
    """Registered for ``kanban_task_claimed`` (dispatcher process, right before the worker is spawned): the visualizer's
    ``research_running`` / ``blog_drafting`` events. Observer only, never raises."""
    try:
        extra = _load_pipeline_extra()
        if not extra:
            return
        db_url = db.resolve_database_url(extra)
        if not db_url:
            return  # events go to the visualizer database; nothing to do without one
        store = EventPostPipelineStore(resolve_store_path(extra.get("store_path")))
        board = extra.get("board")

        def work() -> None:
            try:
                from hermes_cli import kanban_db as kb
                from hermes_cli import kanban_db_connect as kbc

                conn = kbc.connect(board=board)
                try:
                    research.track_claim(conn, pipeline.KanbanOps(kb), store, task_id, tenant=pipeline.TENANT, database_url=db_url)
                finally:
                    conn.close()
            except Exception:  # noqa: BLE001
                logger.exception("[event_post_pipeline] claim tracking failed for task=%s", task_id)

        _background(work)
    except Exception:  # noqa: BLE001
        logger.exception("[event_post_pipeline] claim hook failed for task=%s", task_id)


def _notification_message(result: dict) -> Optional[str]:
    if result["action"] == "created_review_page":
        return f"New event post ready for review: {result['review_url']}"
    if result["action"] == "updated_review_page" and result.get("late_blog"):
        return f"Blog draft ready for review: {result['review_url']}"
    if result["action"] == "updated_review_page":
        return f"Refined {result['platform'].upper()} draft ready for review: {result['review_url']}"
    return None
