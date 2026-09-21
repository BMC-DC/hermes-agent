"""Sends WhatsApp lifecycle notifications for the event-post pipeline.

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

Every notification (decided 2026-09-21) goes to the single hardcoded
``platforms.whatsapp.home_channel.chat_id`` from ``config.yaml`` — the
shared social-media group, never an individual curator's DM. An earlier v2
design resolved per-curator JIDs from ``social_media_curators`` for
targeted DMs; the team decided every notification should be visible to the
whole group instead, so that resolution path was removed as dead code
(git history has it if a future targeted-DM need ever comes back).
"""

from __future__ import annotations

import logging
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


async def _via_live_adapter(chat_id: str, message: str) -> Optional[bool]:
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
        result = await adapter.send(chat_id, message)
        return bool(result.success)
    except Exception:
        logger.debug("event_post_pipeline: live WhatsApp adapter check failed", exc_info=True)
        return None


async def _via_standalone_sender(chat_id: str, message: str) -> bool:
    from hermes_cli.plugins import discover_plugins
    from gateway.platform_registry import platform_registry

    discover_plugins()
    entry = platform_registry.get("whatsapp")
    if entry is None or entry.standalone_sender_fn is None:
        raise WhatsAppNotifyError("whatsapp plugin not registered or missing standalone_sender_fn")
    extra, _ = _load_whatsapp_config()
    pconfig = SimpleNamespace(extra=extra)
    result = await entry.standalone_sender_fn(pconfig, chat_id, message)
    if isinstance(result, dict) and result.get("error"):
        raise WhatsAppNotifyError(str(result["error"]))
    return True


async def send_whatsapp_message(chat_id: str, message: str) -> None:
    """The shared low-level send: live-adapter path first, standalone-sender fallback
    otherwise — the one place both v1's fixed-recipient path and v2's curator-resolved
    path actually touch the network."""
    if not chat_id:
        raise WhatsAppNotifyError("no chat_id/JID to send to")
    live_result = await _via_live_adapter(chat_id, message)
    if live_result is True:
        return
    if live_result is False:
        raise WhatsAppNotifyError("live WhatsApp adapter reported a send failure")
    await _via_standalone_sender(chat_id, message)


async def send_whatsapp_link(message: str) -> None:
    """The one send path every call site uses: the single hardcoded home-channel
    recipient (the shared social-media WhatsApp group)."""
    _, chat_id = _load_whatsapp_config()
    if not chat_id:
        raise WhatsAppNotifyError("no WhatsApp home_channel configured")
    await send_whatsapp_message(chat_id, message)
