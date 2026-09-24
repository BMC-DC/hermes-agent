"""Sends WhatsApp lifecycle notifications for the newsletter pipeline — a
self-contained copy of ``event_post_pipeline.whatsapp_notify`` pointed at
``platforms.newsletter_pipeline.extra`` instead, kept separate per that
module's own convention (each plugin's own config, so one feature's
home-channel change never silently redirects another pipeline's
notifications). See that module's docstring for the full "two kinds of
process" / live-adapter-vs-standalone-sender rationale — unchanged here.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Optional

logger = logging.getLogger("plugins.platforms.newsletter_pipeline")


class WhatsAppNotifyError(RuntimeError):
    """Raised when no home channel is configured or the send itself fails."""


def _load_whatsapp_config() -> tuple[dict, Optional[str]]:
    """Same test/production group switch as event_post_pipeline, under this
    plugin's own config key:

      platforms.newsletter_pipeline.extra:
        whatsapp_group_mode: test              # "test" or "production"
        whatsapp_test_group_chat_id: "..."
        whatsapp_production_group_chat_id: ""
    """
    from plugins.platforms.newsletter_pipeline.root_config import load_root_config

    platforms = load_root_config().get("platforms") or {}
    whatsapp = platforms.get("whatsapp") or {}
    whatsapp_extra = dict(whatsapp.get("extra") or {})

    newsletter_pipeline = platforms.get("newsletter_pipeline") or {}
    pipeline_extra = dict(newsletter_pipeline.get("extra") or {})

    mode = str(pipeline_extra.get("whatsapp_group_mode") or "test").strip().lower()
    test_chat_id = pipeline_extra.get("whatsapp_test_group_chat_id")
    production_chat_id = pipeline_extra.get("whatsapp_production_group_chat_id")

    if not test_chat_id:
        home = whatsapp.get("home_channel") or {}
        test_chat_id = home.get("chat_id") if isinstance(home, dict) else None

    if mode == "production":
        if production_chat_id:
            return whatsapp_extra, production_chat_id
        logger.warning(
            "[newsletter_pipeline] whatsapp_group_mode=production but "
            "whatsapp_production_group_chat_id is not set — falling back to the test "
            "group rather than sending nowhere"
        )
    return whatsapp_extra, test_chat_id


async def _via_live_adapter(chat_id: str, message: str) -> Optional[bool]:
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
        logger.debug("newsletter_pipeline: live WhatsApp adapter check failed", exc_info=True)
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
    if not chat_id:
        raise WhatsAppNotifyError("no chat_id/JID to send to")
    live_result = await _via_live_adapter(chat_id, message)
    if live_result is True:
        return
    if live_result is False:
        raise WhatsAppNotifyError("live WhatsApp adapter reported a send failure")
    await _via_standalone_sender(chat_id, message)


async def send_whatsapp_link(message: str) -> None:
    _, chat_id = _load_whatsapp_config()
    if not chat_id:
        raise WhatsAppNotifyError("no WhatsApp home_channel configured")
    await send_whatsapp_message(chat_id, message)
