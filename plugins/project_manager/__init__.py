"""Captain (project manager) plugin — Phase 1 of extra/plans/pm/PM_PLAN.md.

Registers:
- read-only tools (toolset ``project_manager``) and the change-proposal tools
  (toolset ``project_manager_write``); an agent sees them only if its
  profile's ``toolsets`` lists the toolset and /opt/data/pm/pm.yaml exists;
- the ``pre_gateway_dispatch`` hook that captures the approver's YES/NO.

Scheduled work (weekly brief, meeting reminders, approval executor) is not
registered here: it runs as a Hermes no-agent cron script calling
``python -m plugins.project_manager.jobs tick`` (see jobs.py).
"""

from __future__ import annotations

from plugins.project_manager.hooks import on_pre_gateway_dispatch
from plugins.project_manager.tools import READ_TOOLSET, READ_TOOLS, WRITE_TOOLSET, WRITE_TOOLS, check_available


def register(ctx) -> None:
    for schema, handler, emoji in READ_TOOLS:
        ctx.register_tool(name=schema["name"], toolset=READ_TOOLSET, schema=schema, handler=handler,
                          check_fn=check_available, emoji=emoji)
    for schema, handler, emoji, is_async in WRITE_TOOLS:
        ctx.register_tool(name=schema["name"], toolset=WRITE_TOOLSET, schema=schema, handler=handler,
                          check_fn=check_available, is_async=is_async, emoji=emoji)
    ctx.register_hook("pre_gateway_dispatch", on_pre_gateway_dispatch)
