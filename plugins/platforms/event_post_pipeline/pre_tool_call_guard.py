"""Synchronous ``kanban_complete`` guard for Stylus's fb/ig/blog contract.

Found the hard way (2026-09-25/26, live production, task ``t_9026ad85``):
this plugin's design has always depended on Stylus *reliably* emitting
``metadata["blog"]`` as ``{"body": ..., ...}`` rather than a plain string —
see ``pipeline.py``'s module docstring, which already flagged this as "the
one deliberate exception this plugin depends on". Once Stylus emitted
``blog`` as a bare string, ``kanban_complete`` still succeeded (the generic
Kanban layer only checks "is metadata a dict"), the task sat in Kanban as
done, and the mismatch only surfaced ~15 hours later as a silently-swallowed
error in ``hooks.py::on_kanban_task_completed`` (which runs *after* the task
has already transitioned, in a separate worker-process hook call whose
exceptions ``kanban_db._fire_kanban_lifecycle_hook`` swallows at DEBUG level)
— by which point Stylus's session was long gone and nothing retried it.

This closes the gap at the only point where a correction is still possible:
before ``kanban_complete`` commits, in the same worker turn, so a malformed
``blog`` gets a tool-call rejection Stylus can act on immediately instead of
a rotting Kanban card. Mirrors ``plugins/security-guidance``'s ``pre_tool_call``
block pattern. Deliberately keyed on the *content* of ``metadata`` (does it
contain a "blog" key at all?) rather than the task's board/tenant, so it
needs no extra Kanban lookup and cannot miss a differently-configured board
using the same contract — the shape check is cheap and a false positive is
not possible for a completion that never claims to carry a 'blog' object.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

from plugins.platforms.event_post_pipeline.pipeline import blog_shape_error

logger = logging.getLogger("plugins.platforms.event_post_pipeline")


def on_pre_tool_call(*, tool_name: str = "", args: Any = None, **_kwargs: Any) -> Optional[dict[str, str]]:
    if tool_name not in ("kanban_complete", "kanban_comment") or not isinstance(args, dict):
        return None
    # Research tasks (WebMaster) are decided by TASK IDENTITY (tenant + ``-research`` key), never by the shape of the metadata:
    # a research completion without a research_brief key, with extra keys, or with a free-text summary must not slip through,
    # because B is linked under R and the kanban layer hands R's summary and metadata to B unfenced.
    is_research, verdict = _research_check(tool_name, args)
    if is_research:
        return verdict
    if tool_name != "kanban_complete":
        return None
    metadata = args.get("metadata")
    if not isinstance(metadata, dict) or "blog" not in metadata:
        return None  # not a first-round/blog-refine Stylus completion — nothing to check
    err = blog_shape_error(metadata.get("blog"))
    if err is None:
        return None
    return {
        "action": "block",
        "message": (
            "kanban_complete blocked: " + err + ". Fix metadata['blog'] to be an "
            "object (not a string) and retry kanban_complete — the task is still "
            "open, nothing was lost."
        ),
    }


def _research_check(tool_name: str, args: dict[str, Any]) -> tuple[bool, Optional[dict[str, str]]]:
    """``(is_research_task, verdict)``. For a research task the verdict is authoritative and FAILS CLOSED: any exception while
    checking blocks with a retry message (the pipeline-level fail-open is the sweeper: a stuck R is unlinked and archived, so B is
    never fed raw output). If the task's identity cannot be established at all (no config, board unreachable) the guard stays
    out of the way, exactly as before: ``(False, None)``.

    * ``kanban_complete`` by a research task: metadata must be exactly ``{schema, research_brief}``, the brief must pass
      ``brief.validate_brief`` and the ``summary`` ``brief.validate_summary`` (research.guard_research_completion).
    * ``kanban_comment`` by a research worker onto ANOTHER task is blocked: the kanban layer allows cross-task comments and a
      comment on B is injected into B's worker context unfenced."""
    try:
        from plugins.platforms.event_post_pipeline import pipeline, research
        from plugins.platforms.event_post_pipeline.root_config import load_pipeline_extra
        from plugins.platforms.event_post_pipeline.store import EventPostPipelineStore, resolve_store_path

        env_task = os.environ.get("HERMES_KANBAN_TASK")
        task_id = (args.get("task_id") or env_task) if tool_name == "kanban_complete" else env_task
        if not task_id:
            return False, None
        extra = load_pipeline_extra()
        if not extra:
            return False, None
        from hermes_cli import kanban_db as kb
        from hermes_cli import kanban_db_connect as kbc

        conn = kbc.connect(board=extra.get("board"))
        ops = pipeline.KanbanOps(kb)
        try:
            if not research.is_research_task(ops.get_task(conn, str(task_id)), pipeline.TENANT):
                return False, None
            try:
                if tool_name == "kanban_comment":
                    target = args.get("task_id")
                    if target and str(target) != str(env_task):
                        return True, {"action": "block", "message": (
                            "kanban_comment blocked: a research task may only comment on its own task. Put your answer in "
                            "kanban_complete (metadata research_brief); comments on other tasks are not allowed.")}
                    return True, None
                return True, research.guard_research_completion(
                    conn, ops, EventPostPipelineStore(resolve_store_path(extra.get("store_path"))), str(task_id), args,
                    tenant=pipeline.TENANT, author=pipeline.CREATED_BY, extra=extra,
                )
            except Exception:  # noqa: BLE001 - research task: fail CLOSED
                logger.exception("[event_post_pipeline] research guard failed for task=%s; blocking (fail closed)", task_id)
                return True, {"action": "block", "message": (
                    f"{tool_name} blocked: the research check could not run just now. Retry the same call in a moment; if it keeps "
                    "failing, complete with status nothing_useful or stop: the writer starts without a brief after a few minutes.")}
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 - identity unknown: leave everything as it was
        logger.exception("[event_post_pipeline] research guard could not identify the task; letting the call through")
        return False, None
