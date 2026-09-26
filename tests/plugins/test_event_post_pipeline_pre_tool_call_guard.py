"""Unit tests for ``pre_tool_call_guard.py`` — the synchronous ``kanban_complete``
guard added after a real production incident (2026-09-25/26, task ``t_9026ad85``):
Stylus emitted ``metadata["blog"]`` as a plain string instead of the required
``{"body": ...}`` object, ``kanban_complete`` accepted it anyway (the generic
Kanban layer only checks "is metadata a dict"), and the mismatch only surfaced
~15 hours later, silently, in a downstream hook running in a different process
with no way for Stylus to retry. This guard blocks the malformed call itself,
in the same worker turn.
"""

from __future__ import annotations

from plugins.platforms.event_post_pipeline.pre_tool_call_guard import on_pre_tool_call


def _metadata(**overrides):
    base = {"event_title": "x", "fb": "a", "ig": "b", "blog": {"body": "real body"}}
    return {**base, **overrides}


def test_blocks_the_real_live_incident_payload():
    """Exact shape of the live failure: ``blog`` came back as a bare string."""
    metadata = _metadata(blog="a plain string, not a dict")
    result = on_pre_tool_call(tool_name="kanban_complete", args={"task_id": "t_9026ad85", "metadata": metadata})
    assert result is not None
    assert result["action"] == "block"
    assert "metadata['blog']" in result["message"]


def test_blocks_blog_missing_body_key():
    metadata = _metadata(blog={"seo_title": "no body field here"})
    result = on_pre_tool_call(tool_name="kanban_complete", args={"task_id": "t_x", "metadata": metadata})
    assert result is not None and result["action"] == "block"


def test_allows_well_formed_blog():
    result = on_pre_tool_call(tool_name="kanban_complete", args={"task_id": "t_x", "metadata": _metadata()})
    assert result is None


def test_ignores_calls_without_a_blog_key():
    """A single-platform refine round (fb-only or ig-only) never carries 'blog' —
    must pass through untouched, not be forced into the three-key contract."""
    result = on_pre_tool_call(tool_name="kanban_complete", args={"task_id": "t_x", "metadata": {"fb": "only fb"}})
    assert result is None


def test_ignores_unrelated_tools():
    metadata = _metadata(blog="a plain string, not a dict")
    result = on_pre_tool_call(tool_name="kanban_comment", args={"metadata": metadata})
    assert result is None


def test_ignores_non_dict_args_and_metadata():
    assert on_pre_tool_call(tool_name="kanban_complete", args=None) is None
    assert on_pre_tool_call(tool_name="kanban_complete", args={"metadata": "not a dict"}) is None
