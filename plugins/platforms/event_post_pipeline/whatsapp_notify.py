"""Sends the single review-link WhatsApp notification the skill file's Step
3/5 describe.

This has to work from **two different kinds of process**: the plugin's own
``/intake``/``/review-action`` HTTP routes run inside the gateway process,
but the ``kanban_task_completed`` hook this module is really for fires
inside whichever process drove the completion — for a Stylus task that is
the Kanban-dispatcher-spawned **worker** subprocess, not the gateway
(confirmed: ``hermes_cli/plugins.py``'s hook comment: "claimed fires in the
DISPATCHER right before spawn; completed/blocked fire in the WORKER"). A
worker process has no live gateway adapter to reach into.

So this always goes through the same out-of-process path real cron jobs use
(``standalone_sender_fn`` on the WhatsApp platform's registry entry — see
``plugins/platforms/whatsapp/adapter.py``'s own ``register(ctx)`` call and
``tools/send_message_senders.py``'s identical live-adapter-first-else-
standalone fallback), rather than assuming an in-process adapter exists.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Optional

logger = logging.getLogger("plugins.platforms.event_post_pipeline")


class WhatsAppNotifyError(RuntimeError):
    """Raised when no home channel is configured or the send itself fails."""


def _load_whatsapp_config() -> tuple[dict, Optional[str]]:
    """``(extra, home_chat_id)`` straight from config.yaml — never a live runtime object,
    since this may run in a process that never started a gateway."""
    from hermes_cli.config import load_config

    platforms = (load_config() or {}).get("platforms") or {}
    whatsapp = platforms.get("whatsapp") or {}
    extra = dict(whatsapp.get("extra") or {})
    home = whatsapp.get("home_channel") or {}
    chat_id = home.get("chat_id") if isinstance(home, dict) else None
    return extra, chat_id


async def _via_live_adapter(message: str) -> Optional[bool]:
    """``True``/``False`` if a live gateway adapter answered, ``None`` if this process
    has no running gateway to check (the common case for a worker-process hook fire)."""
    try:
        from gateway.run import _gateway_runner_ref
        from gateway.config import Platform

        runner = _gateway_runner_ref()
        if runner is None:
            return None
        adapter = runner.adapters.get(Platform.WHATSAPP)
        if adapter is None:
            return None
        home = runner.config.get_home_channel(Platform.WHATSAPP)
        if not home:
            return None
        result = await adapter.send(home.chat_id, message)
        return bool(result.success)
    except Exception:
        logger.debug("event_post_pipeline: live WhatsApp adapter check failed", exc_info=True)
        return None


async def _via_standalone_sender(message: str) -> bool:
    from hermes_cli.plugins import discover_plugins
    from gateway.platform_registry import platform_registry

    discover_plugins()
    entry = platform_registry.get("whatsapp")
    if entry is None or entry.standalone_sender_fn is None:
        raise WhatsAppNotifyError("whatsapp plugin not registered or missing standalone_sender_fn")
    extra, chat_id = _load_whatsapp_config()
    if not chat_id:
        raise WhatsAppNotifyError("no WhatsApp home_channel configured")
    pconfig = SimpleNamespace(extra=extra)
    result = await entry.standalone_sender_fn(pconfig, chat_id, message)
    if isinstance(result, dict) and result.get("error"):
        raise WhatsAppNotifyError(str(result["error"]))
    return True


async def send_whatsapp_link(message: str) -> None:
    live_result = await _via_live_adapter(message)
    if live_result is True:
        return
    if live_result is False:
        raise WhatsAppNotifyError("live WhatsApp adapter reported a send failure")
    await _via_standalone_sender(message)
