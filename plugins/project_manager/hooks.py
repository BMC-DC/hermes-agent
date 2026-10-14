"""``pre_gateway_dispatch`` hook: catches the approver's YES/NO replies in a
WhatsApp DM before any agent sees them (PM_PLAN §7 step 2).

Order of checks, cheapest first, so ordinary traffic costs one regex:
1. WhatsApp DM? text parses as a decision ("yes", "NO 12", "👍" ...)?
   Otherwise return None (normal dispatch), with no config or DB access.
2. pm.yaml present, sender is the effective approver (phone/LID aliases via
   the bridge's mapping files), and an approval is pending -> decide it in the
   DB (conditional UPDATE: a repeated YES never decides twice), send a short
   acknowledgement, and drop the message ({"action": "skip"}).
3. Any error -> log and return None: a PM failure must never break chat.

Replies that are not a clear decision pass through untouched to the routed
agent. The approval message tells the approver the exact words to send.
Execution happens in the executor cron job, not here (the hook runs inside
the gateway event loop and must stay fast).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from plugins.project_manager import config as pm_config
from plugins.project_manager import messaging, service, store

logger = logging.getLogger("plugins.project_manager")


def _source_fields(event: Any) -> tuple[str, str, str, str]:
    source = getattr(event, "source", None)
    platform = getattr(getattr(source, "platform", None), "value", "") or str(getattr(source, "platform", "") or "")
    return (platform.lower(), str(getattr(source, "chat_type", "") or "").lower(),
            str(getattr(source, "chat_id", "") or ""), str(getattr(source, "user_id", "") or ""))


def _schedule_ack(chat_id: str, text: str, approval_id: Optional[int]) -> None:
    async def _send() -> None:
        try:
            with store.connection() as conn:
                await messaging.deliver(conn, purpose="approval_ack", chat_id=chat_id, body=text, dedupe_key=None,
                                        dry_run=False, approval_id=approval_id)
        except Exception as exc:
            logger.warning("project_manager: approval ack failed: %s", exc)
    try:
        asyncio.get_running_loop().create_task(_send())
    except RuntimeError:
        asyncio.run(_send())


def on_pre_gateway_dispatch(event: Any = None, **_: Any) -> Optional[dict]:
    try:
        platform, chat_type, chat_id, user_id = _source_fields(event)
        if platform != "whatsapp" or chat_type != "dm":
            return None
        text = getattr(event, "text", "") or ""
        if service.parse_reply(text) is None or not pm_config.is_configured():
            return None
        from gateway.whatsapp_identity import expand_whatsapp_aliases

        sender_aliases = expand_whatsapp_aliases(user_id) | expand_whatsapp_aliases(chat_id)
        cfg = pm_config.load_config()
        with store.connection() as conn:
            outcome = service.handle_reply(cfg, conn, sender_aliases=sender_aliases, text=text,
                                           aliases_of=expand_whatsapp_aliases)
        if not outcome.handled:
            return None
        if outcome.reply:
            _schedule_ack(chat_id, outcome.reply, outcome.approval_id)
        logger.info("project_manager: approval reply handled (approval=%s decision=%s)",
                    outcome.approval_id, outcome.decision)
        return {"action": "skip", "reason": "project_manager approval reply"}
    except Exception as exc:
        logger.warning("project_manager: approval reply hook failed, passing message through: %s", exc)
        return None
