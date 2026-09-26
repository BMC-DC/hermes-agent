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

from typing import Any, Optional

from plugins.platforms.event_post_pipeline.pipeline import blog_shape_error


def on_pre_tool_call(*, tool_name: str = "", args: Any = None, **_kwargs: Any) -> Optional[dict[str, str]]:
    if tool_name != "kanban_complete" or not isinstance(args, dict):
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
