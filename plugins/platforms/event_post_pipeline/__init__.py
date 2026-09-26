"""Event-post-pipeline deterministic-glue plugin.

See ``extra/plans/socialpost/event-post-pipeline-deterministic-glue-plan.md``
(VIDUOPS repo) for the full design. Registers:

- A dedicated platform adapter (``adapter.py``) with its own HTTP listener,
  replacing the intake/review-action LLM turns the ``event-post-pipeline``
  skill currently performs — inert until ``config.yaml`` opts a profile into
  it (never the existing ``webhook`` platform's routes).
- A ``kanban_task_completed`` hook (``hooks.py``) reacting to Stylus's
  completions with zero LLM cost.
- A ``pre_tool_call`` guard (``pre_tool_call_guard.py``) that blocks
  ``kanban_complete`` synchronously, in Stylus's own worker turn, if
  ``metadata["blog"]`` isn't a well-formed object — the deterministic
  version of the contract check ``hooks.py`` otherwise only catches
  after the task is already done (see that module's docstring).

Nothing here runs unless a profile's ``config.yaml`` adds a
``platforms.event_post_pipeline`` block — see the plan's testing section.
"""

from __future__ import annotations

from plugins.platforms.event_post_pipeline.adapter import (
    EventPostPipelineAdapter, check_event_post_pipeline_requirements, _build_adapter,
)
from plugins.platforms.event_post_pipeline.hooks import on_kanban_task_blocked, on_kanban_task_completed
from plugins.platforms.event_post_pipeline.pre_tool_call_guard import on_pre_tool_call as _on_pre_tool_call
from plugins.platforms.event_post_pipeline.tools import (
    SOCIAL_POST_RETRY_SCHEMA, SOCIAL_POST_STATUS_SCHEMA, check_pipeline_tools_available,
    social_post_retry_handler, social_post_status_handler,
)


def register(ctx) -> None:
    ctx.register_platform(
        name="event_post_pipeline",
        label="Event Post Pipeline",
        adapter_factory=_build_adapter,
        check_fn=check_event_post_pipeline_requirements,
        install_hint="Deterministic event-post-pipeline glue; see plugins/platforms/event_post_pipeline/README.",
        required_env=[],
        emoji="🗞️",
    )
    ctx.register_hook("kanban_task_completed", on_kanban_task_completed)
    ctx.register_hook("kanban_task_blocked", on_kanban_task_blocked)
    ctx.register_hook("pre_tool_call", _on_pre_tool_call)
    # Agent-callable tools so Vidu (the general orchestrator) can query/unstick this
    # pipeline directly — see tools.py's module docstring for the design rationale
    # (plain register_tool, no extra delegate_task LLM hop; Stylus stays the only
    # drafting step; no "submit from chat" tool -- that's the publisher's job, gated
    # on real image handling this plugin doesn't have).
    ctx.register_tool(
        name="social_post_status", toolset="event_post_pipeline", schema=SOCIAL_POST_STATUS_SCHEMA,
        handler=social_post_status_handler, check_fn=check_pipeline_tools_available, emoji="📋",
    )
    ctx.register_tool(
        name="social_post_retry", toolset="event_post_pipeline", schema=SOCIAL_POST_RETRY_SCHEMA,
        handler=social_post_retry_handler, check_fn=check_pipeline_tools_available, emoji="🔁",
    )
