"""Resolves this plugin's own config from the root/default profile's
``config.yaml`` — regardless of which profile's process the caller is
actually running in.

Why this exists (found the hard way, 2026-09-21, live production): every
call site that needs this plugin's config (``hooks.py``, ``whatsapp_notify.py``,
``tools.py``) can run inside a Kanban-dispatcher-spawned **worker** subprocess,
not just the gateway — per ``hermes_cli/plugins.py``'s own hook comment,
``kanban_task_completed``/``kanban_task_blocked`` fire in the worker. That
worker's ``HERMES_HOME`` is set to the completing task's assignee's own
profile directory (``resolve_profile_env()`` in ``hermes_cli/profiles.py``),
so a plain ``load_config()`` call there reads *that profile's own*
``config.yaml`` — not the root one an operator actually edits.

This plugin's config (webhook secret, review base URL, WhatsApp group
switch, Postgres URL) is deliberately **global, not per-profile** — there is
exactly one event-post pipeline, one review site, one WhatsApp group
switch. Duplicating it into every profile that might ever complete a task
here is real config drift waiting to happen: Stylus's own profile copy was
missing the WhatsApp group-switch keys entirely and silently fell back to
the wrong group for months of testing before this was caught (2 real
notifications, confirmed in ``whatsapp/bridge.log``, sent to a group Vidu
isn't even a member of).

Fix: always resolve config from the root/default profile's home
(``hermes_constants.get_default_hermes_root()`` — built for exactly this,
"profile-level ops that need the root regardless of current profile
context"), via a context-local override
(``set_hermes_home_override``/``reset_hermes_home_override``) rather than
mutating ``HERMES_HOME`` itself, so this never leaks into unrelated code
running later in the same process.
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
    """``platforms.event_post_pipeline.extra`` from the root config — the single
    source of truth every call site in this plugin should use instead of its
    own ``load_config()`` call."""
    platforms = load_root_config().get("platforms") or {}
    return dict((platforms.get("event_post_pipeline") or {}).get("extra") or {})
