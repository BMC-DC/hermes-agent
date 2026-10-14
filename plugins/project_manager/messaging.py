"""WhatsApp delivery for the Captain plugin.

Sends go straight to the WhatsApp bridge (live gateway adapter when running
inside the gateway, the platform's standalone sender otherwise — same two
paths as ``event_post_pipeline.whatsapp_notify``, kept as a separate copy per
the plugins' convention). They never pass through an agent's chat session, so
Vidu does not see PM sends unless it reads them with a tool (Kusal,
2026-10-14).

Targets (pm.yaml ``whatsapp.mode``):
- ``test``: every PM send — briefs, reminders, approval requests — goes to
  the test recipient's DM (Kusal), and that person is also the effective
  approver, so the whole flow can be exercised end to end before cutover.
- ``production``: briefs and reminders go to the Vidu main group
  (``platforms.whatsapp.home_channel.chat_id`` in the root config.yaml);
  approval requests go to the configured approver's DM (Yohan).

Phone numbers come only from ``pm.people``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Awaitable, Callable, Optional

from plugins.project_manager import store
from plugins.project_manager.config import PMConfig

logger = logging.getLogger("plugins.project_manager")

GROUP_PURPOSES = ("weekly_brief", "meeting_reminder")
DM_PURPOSES = ("approval_request", "approval_ack", "approval_result", "test")


class DeliveryError(RuntimeError):
    """No usable target, or the send itself failed."""


def load_root_config() -> dict[str, Any]:
    """The root profile's merged config.yaml, whatever profile this process runs as
    (same indirection as the pipeline plugins' root_config modules)."""
    from hermes_constants import get_default_hermes_root, reset_hermes_home_override, set_hermes_home_override
    from hermes_cli.config import load_config

    token = set_hermes_home_override(get_default_hermes_root())
    try:
        return load_config() or {}
    finally:
        reset_hermes_home_override(token)


def management_group_chat_id(root_config: Optional[dict] = None) -> str:
    cfg = root_config if root_config is not None else load_root_config()
    whatsapp = (cfg.get("platforms") or {}).get("whatsapp") or {}
    home = whatsapp.get("home_channel") or {}
    chat_id = str(home.get("chat_id") or "").strip() if isinstance(home, dict) else ""
    if not chat_id.endswith("@g.us"):
        raise DeliveryError("platforms.whatsapp.home_channel.chat_id is not a WhatsApp group JID")
    return chat_id


def whatsapp_aliases(identifier: str) -> set[str]:
    """Phone/LID aliases of a WhatsApp identifier, read from the ROOT profile's bridge session.

    The bridge writes its ``lid-mapping-*.json`` files only under the root home. A DM routed to
    another profile (captain) runs with that profile's home, where the gateway's own lookup finds
    no mapping and returns just the LID (seen live 2026-10-14), so PM always resolves here."""
    from gateway.whatsapp_identity import expand_whatsapp_aliases
    from hermes_constants import get_default_hermes_root, reset_hermes_home_override, set_hermes_home_override

    token = set_hermes_home_override(get_default_hermes_root())
    try:
        return expand_whatsapp_aliases(identifier)
    finally:
        reset_hermes_home_override(token)


def person_for_whatsapp_sender(conn, identifiers: list[str],
                               aliases_of: Callable[[str], set[str]] = whatsapp_aliases) -> Optional[dict]:
    """The active pm.people row whose WhatsApp number is an alias of the sender, else None."""
    sender: set[str] = set()
    for ident in identifiers:
        if ident:
            sender |= aliases_of(ident)
    if not sender:
        return None
    for person in store.active_people(conn):
        number = person.get("whatsapp_number")
        if number and aliases_of(str(number)) & sender:
            return person
    return None


def dm_chat_id(person: Optional[dict], who: str) -> str:
    from gateway.whatsapp_identity import to_whatsapp_jid

    number = (person or {}).get("whatsapp_number")
    if not number:
        raise DeliveryError(f"{who} has no WhatsApp number in pm.people")
    return to_whatsapp_jid(str(number))


def effective_approver(conn, cfg: PMConfig) -> dict:
    """The person whose DM receives approval requests and whose replies count."""
    uid = cfg.test_recipient_clickup_user_id if cfg.whatsapp_mode == "test" else cfg.approver_clickup_user_id
    person = store.person_by_clickup_id(conn, uid)
    if person is None:
        raise DeliveryError(f"no active pm.people row for ClickUp user {uid} "
                            f"({'test recipient' if cfg.whatsapp_mode == 'test' else 'approver'})")
    return person


def target_chat_id(conn, cfg: PMConfig, purpose: str, *, root_config: Optional[dict] = None) -> str:
    if purpose not in GROUP_PURPOSES + DM_PURPOSES:
        raise DeliveryError(f"unknown purpose {purpose!r}")
    if cfg.whatsapp_mode == "test":
        person = store.person_by_clickup_id(conn, cfg.test_recipient_clickup_user_id)
        return dm_chat_id(person, "the test recipient")
    if purpose in GROUP_PURPOSES:
        return management_group_chat_id(root_config)
    return dm_chat_id(effective_approver(conn, cfg), "the approver")


# ── transport ────────────────────────────────────────────────────────────────
async def _via_live_adapter(chat_id: str, message: str) -> Optional[bool]:
    try:
        from gateway.config import Platform
        from gateway.run import _gateway_runner_ref

        runner = _gateway_runner_ref()
        if runner is None:
            return None
        adapter = runner.adapters.get(Platform.WHATSAPP)
        if adapter is None:
            return None
        result = await adapter.send(chat_id, message)
        return bool(result.success)
    except Exception:
        logger.debug("project_manager: live WhatsApp adapter check failed", exc_info=True)
        return None


async def _via_standalone_sender(chat_id: str, message: str) -> None:
    from gateway.platform_registry import platform_registry
    from hermes_cli.plugins import discover_plugins

    discover_plugins()
    entry = platform_registry.get("whatsapp")
    if entry is None or entry.standalone_sender_fn is None:
        raise DeliveryError("whatsapp platform not registered or has no standalone sender")
    whatsapp = (load_root_config().get("platforms") or {}).get("whatsapp") or {}
    result = await entry.standalone_sender_fn(SimpleNamespace(extra=dict(whatsapp.get("extra") or {})), chat_id, message)
    if isinstance(result, dict) and result.get("error"):
        raise DeliveryError(str(result["error"])[:300])


async def send_whatsapp(chat_id: str, message: str) -> None:
    if not chat_id:
        raise DeliveryError("empty chat id")
    live = await _via_live_adapter(chat_id, message)
    if live is True:
        return
    if live is False:
        raise DeliveryError("live WhatsApp adapter reported a send failure")
    await _via_standalone_sender(chat_id, message)


Sender = Callable[[str, str], Awaitable[None]]
MAX_SEND_ATTEMPTS = 3


@dataclass(frozen=True)
class DeliveryResult:
    status: str                     # sent | failed | skipped_duplicate | dry_run | gave_up
    message_id: Optional[int] = None
    error: Optional[str] = None


async def deliver(conn, *, purpose: str, chat_id: str, body: str, dedupe_key: Optional[str],
                  dry_run: bool, sender: Sender = send_whatsapp, approval_id: Optional[int] = None,
                  report_id: Optional[int] = None, meeting_id: Optional[int] = None) -> DeliveryResult:
    """Claim (by dedupe_key) -> send -> record. A dry run never claims the key,
    so it can never block the real send."""
    if dry_run:
        return DeliveryResult("dry_run")
    if dedupe_key and store.failed_attempts(conn, dedupe_key) >= MAX_SEND_ATTEMPTS:
        return DeliveryResult("gave_up", error=f"{MAX_SEND_ATTEMPTS} failed attempts")
    message_id = store.claim_send(conn, dedupe_key=dedupe_key, purpose=purpose, chat_id=chat_id, body=body,
                                  approval_id=approval_id, report_id=report_id, meeting_id=meeting_id)
    if message_id is None:
        return DeliveryResult("skipped_duplicate")
    try:
        await sender(chat_id, body)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"[:500]
        store.finish_send(conn, message_id, "failed", error)
        logger.warning("project_manager: %s send failed (message_log %s): %s", purpose, message_id, error)
        return DeliveryResult("failed", message_id, error)
    store.finish_send(conn, message_id, "sent")
    return DeliveryResult("sent", message_id)
