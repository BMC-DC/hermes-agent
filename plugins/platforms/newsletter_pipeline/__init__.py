"""Newsletter-pipeline deterministic-glue plugin.

See ``extra/plans/newsletter/newsletter-pipeline-plan.md`` (VIDUOPS repo) for
the full design. Registers:

- A dedicated platform adapter (``adapter.py``) with its own HTTP listener
  and port, independent of every other platform/plugin.
- A ``kanban_task_completed``/``kanban_task_blocked`` hook pair (``hooks.py``)
  reacting to Stylus's completions with zero LLM cost — same pattern as
  ``plugins.platforms.event_post_pipeline``.

Nothing here runs unless a profile's ``config.yaml`` adds a
``platforms.newsletter_pipeline`` block.
"""

from __future__ import annotations

from plugins.platforms.newsletter_pipeline.adapter import (
    NewsletterPipelineAdapter, check_newsletter_pipeline_requirements, _build_adapter,
)
from plugins.platforms.newsletter_pipeline.hooks import on_kanban_task_blocked, on_kanban_task_completed


def register(ctx) -> None:
    ctx.register_platform(
        name="newsletter_pipeline",
        label="Newsletter Pipeline",
        adapter_factory=_build_adapter,
        check_fn=check_newsletter_pipeline_requirements,
        install_hint="Deterministic newsletter-pipeline glue; see plugins/platforms/newsletter_pipeline/.",
        required_env=[],
        emoji="📰",
    )
    ctx.register_hook("kanban_task_completed", on_kanban_task_completed)
    ctx.register_hook("kanban_task_blocked", on_kanban_task_blocked)
