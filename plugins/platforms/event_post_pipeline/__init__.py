"""Event-post-pipeline deterministic-glue plugin.

See ``extra/plans/socialpost/event-post-pipeline-deterministic-glue-plan.md``
(VIDUOPS repo) for the full design. Registers:

- A dedicated platform adapter (``adapter.py``) with its own HTTP listener,
  replacing the intake/review-action LLM turns the ``event-post-pipeline``
  skill currently performs — inert until ``config.yaml`` opts a profile into
  it (never the existing ``webhook`` platform's routes).
- A ``kanban_task_completed`` hook (``hooks.py``) reacting to Stylus's
  completions with zero LLM cost.

Nothing here runs unless a profile's ``config.yaml`` adds a
``platforms.event_post_pipeline`` block — see the plan's testing section.
"""

from __future__ import annotations

from plugins.platforms.event_post_pipeline.adapter import (
    EventPostPipelineAdapter, check_event_post_pipeline_requirements, _build_adapter,
)
from plugins.platforms.event_post_pipeline.hooks import on_kanban_task_blocked, on_kanban_task_completed


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
