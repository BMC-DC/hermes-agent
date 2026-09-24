"""The plugin's own HTTP listener: ``/intake`` and ``/review-action`` on a
dedicated port, independent of every other route in the system — mirrors
``event_post_pipeline.adapter`` exactly in shape (a full custom platform via
``PluginContext.register_platform``, same as the WhatsApp bridge and the
event-post-pipeline plugin already do).
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Optional

try:
    from aiohttp import web
    AIOHTTP_AVAILABLE = True
except ImportError:
    AIOHTTP_AVAILABLE = False
    web = None  # type: ignore[assignment]

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult
from plugins.platforms.newsletter_pipeline import brevo_client, db, models, pipeline, security, whatsapp_notify
from plugins.platforms.newsletter_pipeline.brevo_client import BrevoConfig
from plugins.platforms.newsletter_pipeline.review_client import ReviewClientConfig
from plugins.platforms.newsletter_pipeline.store import NewsletterPipelineStore, resolve_store_path

logger = logging.getLogger("plugins.platforms.newsletter_pipeline")

DEFAULT_PORT = 8646  # event_post_pipeline uses 8645 — next free port in the same block
_MAX_BODY_BYTES = 1_048_576


def check_newsletter_pipeline_requirements() -> bool:
    return AIOHTTP_AVAILABLE


def _json_error(message: str, status: int) -> "web.Response":
    return web.json_response({"error": message}, status=status)


class NewsletterPipelineAdapter(BasePlatformAdapter):
    """Owns ``/intake`` and ``/review-action`` on its own loopback port."""

    interactive_resume = False
    supports_async_delivery = False

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform("newsletter_pipeline"))
        extra = config.extra
        self._host: str = extra.get("host", "127.0.0.1")
        self._port: int = int(extra.get("port", DEFAULT_PORT))
        self._secret: str = security.resolve_env_secret(extra.get("secret", ""))
        self._board: Optional[str] = extra.get("board")
        self._store = NewsletterPipelineStore(resolve_store_path(extra.get("store_path")))
        self._db_url: str = db.resolve_database_url(extra)
        self._review_base_url: str = str(extra.get("review_base_url", "https://spp.buddhameditationdc.org")).rstrip("/")
        self._review_config = ReviewClientConfig(
            base_url=self._review_base_url, create_secret=security.resolve_review_create_secret(extra),
        )
        # Feature flag, off by default -- per newsletter-pipeline-plan.md's
        # explicit "build the infra, wire it later" decision. Even once
        # BREVO_API_KEY exists, nothing sends until this is deliberately
        # flipped on in config.yaml: platforms.newsletter_pipeline.extra.brevo_send_enabled: true
        self._brevo_config: Optional[BrevoConfig] = (
            brevo_client.resolve_brevo_config(extra) if extra.get("brevo_send_enabled") else None
        )
        self._runner = None

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        if not self._secret:
            logger.error("[newsletter_pipeline] no 'secret' configured; refusing to start")
            return False
        app = web.Application(client_max_size=_MAX_BODY_BYTES)
        app.router.add_get("/health", self._handle_health)
        app.router.add_post("/intake", self._handle_intake)
        app.router.add_post("/review-action", self._handle_review_action)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self._host, self._port)
        try:
            await site.start()
        except OSError as exc:
            await self._runner.cleanup()
            self._runner = None
            logger.error("[newsletter_pipeline] could not bind %s:%d: %s", self._host, self._port, exc)
            return False
        self._mark_connected()
        logger.info("[newsletter_pipeline] listening on %s:%d", self._host, self._port)
        return True

    async def disconnect(self) -> None:
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
        self._mark_disconnected()

    async def send(self, chat_id: str, content: str, reply_to=None, metadata=None) -> SendResult:
        return SendResult(success=False, error="newsletter_pipeline is a webhook-only platform; it never sends")

    async def get_chat_info(self, chat_id: str) -> dict:
        return {"name": chat_id, "type": "webhook"}

    # --- HTTP handlers ---

    async def _handle_health(self, request: "web.Request") -> "web.Response":
        return web.json_response({"status": "ok", "platform": "newsletter_pipeline"})

    async def _read_authenticated_json(self, request: "web.Request") -> "tuple[Optional[Any], Optional[web.Response]]":
        if (request.content_length or 0) > _MAX_BODY_BYTES:
            return None, _json_error("Payload too large", 413)
        try:
            raw_body = await request.read()
        except Exception as exc:
            logger.error("[newsletter_pipeline] failed to read body: %s", exc)
            return None, _json_error("Bad request", 400)
        timestamp = request.headers.get("X-Webhook-Timestamp", "")
        signature = request.headers.get("X-Webhook-Signature-V2", "")
        if not security.verify_signature(secret=self._secret, timestamp=timestamp, signature=signature, body=raw_body):
            logger.warning("[newsletter_pipeline] invalid or missing signature")
            return None, _json_error("Invalid signature", 401)
        try:
            return json.loads(raw_body), None
        except json.JSONDecodeError:
            return None, _json_error("Cannot parse body", 400)

    async def _handle_intake(self, request: "web.Request") -> "web.Response":
        payload, error = await self._read_authenticated_json(request)
        if error is not None:
            return error
        submission_id = models.parse_submission_id(payload)
        if not submission_id:
            return _json_error("missing or invalid submissionId", 400)
        try:
            task_id = await asyncio.to_thread(self._run_intake, submission_id, payload)
        except pipeline.PipelineError as exc:
            logger.error("[newsletter_pipeline] intake failed for submission=%s: %s", submission_id, exc)
            return _json_error("intake_failed", 502)
        await self._notify_submission_arrived(submission_id, payload, task_id)
        return web.json_response({"status": "accepted", "task_id": task_id}, status=202)

    async def _notify_submission_arrived(self, submission_id: str, payload: dict, task_id: str) -> None:
        submitter_name = str(payload.get("submitterName") or "").strip()
        submitter_phone = str(payload.get("submitterPhone") or "").strip()
        if self._db_url and submitter_phone:
            try:
                await asyncio.to_thread(self._upsert_submitter_curator, submitter_phone, submitter_name)
            except db.DatabaseError as exc:
                logger.error("[newsletter_pipeline] could not upsert submitter curator: %s", exc)
        who = f"{submitter_name} ({submitter_phone})" if (submitter_name or submitter_phone) else "an unidentified submitter"
        message = f"New newsletter submission from {who} for {payload.get('issueMonth', '')} — drafting has started."
        try:
            await whatsapp_notify.send_whatsapp_link(message)
        except Exception:
            logger.exception("[newsletter_pipeline] intake notify send failed for task=%s", task_id)
            await asyncio.to_thread(pipeline.track_notification, self._db_url, task_id, ok=False, message=message)
            return
        await asyncio.to_thread(pipeline.track_notification, self._db_url, task_id, ok=True, message=message)

    def _upsert_submitter_curator(self, submitter_phone: str, submitter_name: str) -> None:
        conn = db.get_connection(self._db_url)
        try:
            db.upsert_curator(conn, submitter_phone, submitter_name or submitter_phone, is_publisher=True)
        finally:
            conn.close()

    def _run_intake(self, submission_id: str, payload: dict) -> str:
        from hermes_cli import kanban_db as kb
        from hermes_cli import kanban_db_connect as kbc

        conn = kbc.connect(board=self._board)
        try:
            ops = pipeline.KanbanOps(kb)
            return pipeline.handle_intake(
                conn, ops, self._store, submission_id=submission_id, intake=payload, database_url=self._db_url,
            )
        finally:
            conn.close()

    async def _handle_review_action(self, request: "web.Request") -> "web.Response":
        payload, error = await self._read_authenticated_json(request)
        if error is not None:
            return error
        action = models.parse_review_action(payload)
        if action is None:
            return _json_error("invalid review action payload", 400)
        try:
            result = await asyncio.to_thread(self._run_review_action, action)
        except pipeline.PipelineError as exc:
            logger.error("[newsletter_pipeline] review action failed for task=%s: %s", action.task_id, exc)
            return _json_error("action_failed", 502)
        await self._notify_review_action(action, result)
        return web.json_response({"ok": True, **result})

    async def _notify_review_action(self, action: models.ReviewActionPayload, result: dict) -> None:
        verb = {"approved": "approved", "rejected": "rejected", "refine": "sent back for a refine"}.get(action.action, action.action)
        message = f"The newsletter draft was {verb} on review."
        if action.comment:
            message += f" Reviewer note: {action.comment}"
        if result.get("brevo_campaign_id"):
            message += f" Sent via Brevo (campaign {result['brevo_campaign_id']})."
        try:
            await whatsapp_notify.send_whatsapp_link(message)
        except whatsapp_notify.WhatsAppNotifyError as exc:
            logger.error("[newsletter_pipeline] review-action notify send failed: %s", exc)
            await asyncio.to_thread(pipeline.track_notification, self._db_url, action.task_id, ok=False, message=message)
            return
        await asyncio.to_thread(pipeline.track_notification, self._db_url, action.task_id, ok=True, message=message)

    def _run_review_action(self, action: models.ReviewActionPayload) -> dict:
        from hermes_cli import kanban_db as kb
        from hermes_cli import kanban_db_connect as kbc

        conn = kbc.connect(board=self._board)
        try:
            ops = pipeline.KanbanOps(kb)
            return pipeline.handle_review_action(
                conn, ops, self._store, self._review_config, task_id=action.task_id, action=action.action,
                comment=action.comment, database_url=self._db_url, brevo_config=self._brevo_config,
            )
        finally:
            conn.close()


def _build_adapter(config: PlatformConfig) -> NewsletterPipelineAdapter:
    return NewsletterPipelineAdapter(config)
