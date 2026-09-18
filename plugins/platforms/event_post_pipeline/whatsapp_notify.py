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

Two recipient shapes, same two send paths underneath:

- ``send_whatsapp_link`` (kept from v1, unchanged): the single hardcoded
  ``platforms.whatsapp.home_channel.chat_id`` from ``config.yaml`` — still
  used wherever a call site hasn't been migrated to curator-resolved
  recipients.
- ``send_whatsapp_to_curator`` / ``send_whatsapp_to_phone`` (new, v2): resolve
  a JID from a ``social_media_curators`` row (or a bare phone number) via
  ``gateway/whatsapp_identity.py``'s ``to_whatsapp_jid()``, then send through
  the exact same ``_via_live_adapter``/``_via_standalone_sender`` machinery —
  only the target chat_id changes, never the transport.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import Iterable, Optional

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
    """v1 behavior, unchanged: the single hardcoded home-channel recipient. Still used by
    any call site not yet migrated to curator-resolved recipients."""
    _, chat_id = _load_whatsapp_config()
    if not chat_id:
        raise WhatsAppNotifyError("no WhatsApp home_channel configured")
    await send_whatsapp_message(chat_id, message)


def curator_jid(curator: dict) -> str:
    """The outbound JID for a ``social_media_curators`` row (or any dict with a
    ``phone_number`` key) — never ``config.yaml``, per the plan doc's requirement that
    v2 recipients come exclusively from the curator registry."""
    from gateway.whatsapp_identity import to_whatsapp_jid

    phone = (curator or {}).get("phone_number") or ""
    return to_whatsapp_jid(phone)


async def send_whatsapp_to_curator(curator: dict, message: str) -> None:
    """Sends to one curator row, resolved to a JID via ``to_whatsapp_jid()``."""
    jid = curator_jid(curator)
    if not jid:
        raise WhatsAppNotifyError(f"curator has no usable phone_number to resolve a JID from: {curator!r}")
    await send_whatsapp_message(jid, message)


async def send_whatsapp_to_phone(phone_number: str, message: str) -> None:
    """Sends to a bare phone number, resolved to a JID via ``to_whatsapp_jid()`` — for
    call sites that have a phone but haven't (yet) upserted/looked up the curator row."""
    from gateway.whatsapp_identity import to_whatsapp_jid

    jid = to_whatsapp_jid(phone_number)
    if not jid:
        raise WhatsAppNotifyError(f"could not resolve a JID from phone_number={phone_number!r}")
    await send_whatsapp_message(jid, message)


async def notify_curators(curators: Iterable[dict], message: str) -> None:
    """Best-effort fan-out to several curators (e.g. "publisher + admin"): one curator's
    send failure is logged and does not stop the others from being notified — matching
    this plugin's overall rule that a notification problem should never take down the
    deterministic pipeline logic around it."""
    seen_phones: set[str] = set()
    for curator in curators:
        phone = (curator or {}).get("phone_number")
        if not phone or phone in seen_phones:
            continue  # de-dup: the same person may hold more than one matching role flag
        seen_phones.add(phone)
        try:
            await send_whatsapp_to_curator(curator, message)
        except WhatsAppNotifyError as exc:
            logger.error("event_post_pipeline: WhatsApp notify failed for curator id=%s: %s", curator.get("id"), exc)


def run_notify_curators(curators: Iterable[dict], message: str) -> None:
    """Sync convenience wrapper (``asyncio.run``) for call sites that aren't already
    inside an event loop — mirrors ``hooks.py``'s existing ``asyncio.run(...)`` usage."""
    asyncio.run(notify_curators(list(curators), message))
