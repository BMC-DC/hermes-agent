"""Resolves this plugin's own config from the root/default profile's
``config.yaml``, regardless of which profile's process the caller is
actually running in. A self-contained copy of
``plugins.platforms.event_post_pipeline.root_config`` pointed at
``platforms.newsletter_pipeline`` — see that module's docstring for why
this indirection exists (the ``kanban_task_completed``/``kanban_task_blocked``
hooks fire inside a Kanban-dispatcher-spawned worker whose ``HERMES_HOME``
is the *completing task's assignee's own* profile directory, not the root
one an operator actually edits — this plugin's config is global, not
per-profile, so it must always resolve from the root profile regardless).
"""

from __future__ import annotations

from typing import Any


def load_root_config() -> dict[str, Any]:
    """The root/default profile's fully-merged config, regardless of the
    calling process's own profile context."""
    from hermes_constants import get_default_hermes_root, reset_hermes_home_override, set_hermes_home_override
    from hermes_cli.config import load_config

    token = set_hermes_home_override(get_default_hermes_root())
    try:
        return load_config() or {}
    finally:
        reset_hermes_home_override(token)


def load_pipeline_extra() -> dict[str, Any]:
    """``platforms.newsletter_pipeline.extra`` from the root config — the single
    source of truth every call site in this plugin should use instead of its
    own ``load_config()`` call."""
    platforms = load_root_config().get("platforms") or {}
    return dict((platforms.get("newsletter_pipeline") or {}).get("extra") or {})
