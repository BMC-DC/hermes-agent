"""The plugin's own HTTP listener: a dedicated platform adapter (own port,
own two routes), independent of ``gateway/platforms/webhook.py``'s generic
webhook routes — see the design doc for why (a plugin can register a full
custom platform via ``PluginContext.register_platform``, exactly like
``plugins/platforms/whatsapp/adapter.py`` already does for its own bridge).

Kept deliberately separate from the existing ``event-post-pack``/
``event-post-review`` routes in ``config.yaml`` so the old, skill-driven
path is never touched: this listens on its own port, and nothing points at
it until the Vercel env-var switch (a later, separate step) says so.
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
from plugins.platforms.event_post_pipeline import models, pipeline, security
from plugins.platforms.event_post_pipeline.store import EventPostPipelineStore, resolve_store_path

logger = logging.getLogger("plugins.platforms.event_post_pipeline")

DEFAULT_PORT = 8645
_MAX_BODY_BYTES = 1_048_576


def check_event_post_pipeline_requirements() -> bool:
    return AIOHTTP_AVAILABLE


def _json_error(message: str, status: int) -> "web.Response":
    return web.json_response({"error": message}, status=status)


class EventPostPipelineAdapter(BasePlatformAdapter):
    """Owns ``/intake`` and ``/review-action`` on its own loopback port."""

    interactive_resume = False
    supports_async_delivery = False

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform("event_post_pipeline"))
        extra = config.extra
        self._host: str = extra.get("host", "127.0.0.1")
        self._port: int = int(extra.get("port", DEFAULT_PORT))
        self._secret: str = security.resolve_env_secret(extra.get("secret", ""))
        self._board: Optional[str] = extra.get("board")
        self._store = EventPostPipelineStore(resolve_store_path(extra.get("store_path")))
        self._runner = None

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        # Only this route pair's own HMAC secret gates startup here; the review-page
        # bearer secret is resolved fresh (file or ${VAR}) by hooks.py, which is what
        # actually calls that API — these routes never do.
        if not self._secret:
            logger.error("[event_post_pipeline] no 'secret' configured; refusing to start")
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
            logger.error("[event_post_pipeline] could not bind %s:%d: %s", self._host, self._port, exc)
            return False
        self._mark_connected()
        logger.info("[event_post_pipeline] listening on %s:%d", self._host, self._port)
        return True

    async def disconnect(self) -> None:
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
        self._mark_disconnected()

    async def send(self, chat_id: str, content: str, reply_to=None, metadata=None) -> SendResult:
        return SendResult(success=False, error="event_post_pipeline is a webhook-only platform; it never sends")

    async def get_chat_info(self, chat_id: str) -> dict:
        return {"name": chat_id, "type": "webhook"}

    # --- HTTP handlers ---

    async def _handle_health(self, request: "web.Request") -> "web.Response":
        return web.json_response({"status": "ok", "platform": "event_post_pipeline"})

    async def _read_authenticated_json(self, request: "web.Request") -> "tuple[Optional[Any], Optional[web.Response]]":
        if (request.content_length or 0) > _MAX_BODY_BYTES:
            return None, _json_error("Payload too large", 413)
        try:
            raw_body = await request.read()
        except Exception as exc:
            logger.error("[event_post_pipeline] failed to read body: %s", exc)
            return None, _json_error("Bad request", 400)
        timestamp = request.headers.get("X-Webhook-Timestamp", "")
        signature = request.headers.get("X-Webhook-Signature-V2", "")
        if not security.verify_signature(secret=self._secret, timestamp=timestamp, signature=signature, body=raw_body):
            logger.warning("[event_post_pipeline] invalid or missing signature")
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
            logger.error("[event_post_pipeline] intake failed for submission=%s: %s", submission_id, exc)
            return _json_error("intake_failed", 502)
        return web.json_response({"status": "accepted", "task_id": task_id}, status=202)

    def _run_intake(self, submission_id: str, payload: dict) -> str:
        from hermes_cli import kanban_db as kb
        from hermes_cli import kanban_db_connect as kbc

        conn = kbc.connect(board=self._board)
        try:
            ops = pipeline.KanbanOps(kb)
            return pipeline.handle_intake(conn, ops, self._store, submission_id=submission_id, event_pack=payload)
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
            logger.error("[event_post_pipeline] review action failed for task=%s: %s", action.task_id, exc)
            return _json_error("action_failed", 502)
        return web.json_response({"ok": True, **result})

    def _run_review_action(self, action: models.ReviewActionPayload) -> dict:
        from hermes_cli import kanban_db as kb
        from hermes_cli import kanban_db_connect as kbc

        conn = kbc.connect(board=self._board)
        try:
            ops = pipeline.KanbanOps(kb)
            return pipeline.handle_review_action(
                conn, ops, self._store, task_id=action.task_id, platform=action.platform,
                action=action.action, comment=action.comment,
            )
        finally:
            conn.close()

def _build_adapter(config: PlatformConfig) -> EventPostPipelineAdapter:
    return EventPostPipelineAdapter(config)
