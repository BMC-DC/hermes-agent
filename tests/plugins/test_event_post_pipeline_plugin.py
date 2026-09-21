"""Registration smoke test for the event-post-pipeline plugin package: a
real ``register()`` call against a real ``PluginContext``/``PluginManager``
(same pattern ``test_teams_pipeline_plugin.py`` uses), verifying the
platform and the ``kanban_task_completed`` hook both land correctly."""

from __future__ import annotations

from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest
from gateway.config import Platform
from plugins.platforms.event_post_pipeline import register
from plugins.platforms.event_post_pipeline.hooks import on_kanban_task_blocked, on_kanban_task_completed


def test_register_adds_platform_and_hook():
    mgr = PluginManager()
    manifest = PluginManifest(name="event_post_pipeline")
    ctx = PluginContext(manifest, mgr)

    register(ctx)

    from gateway.platform_registry import platform_registry
    entry = platform_registry.get("event_post_pipeline")
    assert entry is not None
    assert entry.label == "Event Post Pipeline"
    assert callable(entry.adapter_factory)

    assert on_kanban_task_completed in mgr._hooks.get("kanban_task_completed", [])
    assert on_kanban_task_blocked in mgr._hooks.get("kanban_task_blocked", [])

    # Confirmed real-world consequence of registration: the dynamic Platform
    # enum member now resolves without needing plugins/platforms/ scanning at all.
    assert Platform("event_post_pipeline").value == "event_post_pipeline"
