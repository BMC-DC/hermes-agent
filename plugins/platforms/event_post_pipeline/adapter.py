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
from plugins.platforms.event_post_pipeline import db, models, pipeline, security, whatsapp_notify
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
        # v2 curator registry + lock fields (see db.py's module docstring). Deliberately
        # optional here: a profile that hasn't opted into the Postgres path yet (no
        # SPP_DATABASE_URL) just gets no curator-resolved notifications, never a crash —
        # same "degrade gracefully" rule the rest of this plugin already follows.
        self._db_url: str = db.resolve_database_url(extra)
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
        app.router.add_post("/retry", self._handle_retry)
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
        await self._notify_submission_arrived(submission_id, payload, task_id)
        return web.json_response({"status": "accepted", "task_id": task_id}, status=202)

    async def _notify_submission_arrived(self, submission_id: str, payload: dict, task_id: str) -> None:
        """Lifecycle events "submission arrived" and "Stylus started drafting" (plan doc's
        lifecycle table) — merged into one instant WhatsApp message rather than two,
        since both fire from this exact call site at the exact same instant with
        overlapping information (a second near-duplicate message a moment later would be
        pure noise, not a distinct event a recipient could act on differently). See the
        plugin's own review comment / final report for this deviation from the plan doc's
        literal two-row table.

        ``submitterName``/``submitterPhone`` are read from the intake payload if present
        (the "required name + WhatsApp number" field the plan doc describes adding to the
        shared intake form) — this degrades to an "unidentified submitter" message rather
        than failing if they're absent.

        Decided 2026-09-21: this always sends to the shared social-media WhatsApp group
        (never an individual curator's DM) — the team wants shared visibility into all
        activity, not fragmented per-person messages. The Postgres upsert below is
        unrelated bookkeeping for the "Social Media Curator" ledger (so Vidu can later
        answer "what happened to X's post"), not a step in choosing who gets notified.
        """
        submitter_name = str(payload.get("submitterName") or "").strip()
        submitter_phone = str(payload.get("submitterPhone") or "").strip()
        if self._db_url and submitter_phone:
            try:
                await asyncio.to_thread(self._upsert_submitter_curator, submitter_phone, submitter_name)
            except db.DatabaseError as exc:
                logger.error("[event_post_pipeline] could not upsert submitter curator: %s", exc)
        who = f"{submitter_name} ({submitter_phone})" if (submitter_name or submitter_phone) else "an unidentified submitter"
        message = f"New event post submission from {who} — drafting has started (task {task_id})."
        try:
            await whatsapp_notify.send_whatsapp_link(message)
        except Exception as exc:
            logger.exception("[event_post_pipeline] intake notify send failed for task=%s", task_id)
            await asyncio.to_thread(pipeline.track_notification, self._db_url, task_id, ok=False, detail=str(exc))
            return
        await asyncio.to_thread(pipeline.track_notification, self._db_url, task_id, ok=True)

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
                conn, ops, self._store, submission_id=submission_id, event_pack=payload, database_url=self._db_url,
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
            logger.error("[event_post_pipeline] review action failed for task=%s: %s", action.task_id, exc)
            return _json_error("action_failed", 502)
        await self._notify_review_action(action, result)
        return web.json_response({"ok": True, **result})

    async def _notify_review_action(self, action: models.ReviewActionPayload, result: dict) -> None:
        """Lifecycle event "review action taken" (plan doc's lifecycle table): notifies
        the shared social-media WhatsApp group that a draft was approved/rejected/sent
        back for a refine (decided 2026-09-21: every pipeline notification goes to the
        group, never an individual curator's DM — the original submitter's name is
        included in the message text instead, since "your draft was approved" only makes
        sense addressed to one person). Silently degrades to "the submitter" when
        Postgres isn't configured, or the submission predates the v2 cutover (no
        ``post_submissions`` row / no ``submitted_by``) — never fails the review action
        itself over a notification problem."""
        submitter_name = "the submitter"
        if self._db_url:
            try:
                _submission, curator = await asyncio.to_thread(self._resolve_submitter, action.task_id)
                if curator:
                    submitter_name = curator.get("name") or submitter_name
            except db.DatabaseError as exc:
                logger.error("[event_post_pipeline] could not resolve submitter for review-action notify: %s", exc)
        verb = {"approved": "approved", "rejected": "rejected", "refine": "sent back for a refine"}.get(action.action, action.action)
        message = f"{submitter_name}'s {action.platform.upper()} draft was {verb} on review."
        if action.comment:
            message += f" Reviewer note: {action.comment}"
        try:
            await whatsapp_notify.send_whatsapp_link(message)
        except whatsapp_notify.WhatsAppNotifyError as exc:
            logger.error("[event_post_pipeline] review-action notify send failed: %s", exc)
            await asyncio.to_thread(pipeline.track_notification, self._db_url, action.task_id, ok=False, detail=str(exc))
            return
        await asyncio.to_thread(pipeline.track_notification, self._db_url, action.task_id, ok=True)

    def _resolve_submitter(self, task_id: str) -> "tuple[Optional[dict], Optional[dict]]":
        conn = db.get_connection(self._db_url)
        try:
            submission = db.get_submission_by_task_id(conn, task_id)
            if submission is None or not submission.get("submitted_by"):
                return submission, None
            return submission, db.get_curator(conn, submission["submitted_by"])
        finally:
            conn.close()

    def _run_review_action(self, action: models.ReviewActionPayload) -> dict:
        from hermes_cli import kanban_db as kb
        from hermes_cli import kanban_db_connect as kbc

        conn = kbc.connect(board=self._board)
        try:
            ops = pipeline.KanbanOps(kb)
            return pipeline.handle_review_action(
                conn, ops, self._store, task_id=action.task_id, platform=action.platform,
                action=action.action, comment=action.comment, database_url=self._db_url,
            )
        finally:
            conn.close()

    async def _handle_retry(self, request: "web.Request") -> "web.Response":
        """``POST /retry`` — the visualizer's "unstick this" action: retries a failed
        WhatsApp notification, or unblocks a Stylus task that's stuck ``blocked``/
        ``scheduled``. Same HMAC auth as every other route on this listener (this is
        the plugin's own adapter, not a new auth surface). See the contract in the
        plan doc / this repo's own commit message for the exact request/response
        shape the portal-side visualizer relies on."""
        payload, error = await self._read_authenticated_json(request)
        if error is not None:
            return error
        if not isinstance(payload, dict):
            return _json_error("invalid retry payload", 400)
        task_id = payload.get("taskId")
        kind = payload.get("kind")
        if not isinstance(task_id, str) or not models.SAFE_ID_RE.match(task_id):
            return _json_error("missing or invalid taskId", 400)
        if kind == "stylus_blocked":
            return await self._retry_stylus_blocked(task_id)
        if kind == "notify":
            message = payload.get("message")
            if not isinstance(message, str) or not message.strip():
                return _json_error("missing or empty message for kind=notify", 400)
            return await self._retry_notify(task_id, message)
        return _json_error(f"unknown kind {kind!r}", 400)

    async def _retry_stylus_blocked(self, task_id: str) -> "web.Response":
        try:
            unblocked = await asyncio.to_thread(self._run_unblock_task, task_id)
        except Exception as exc:
            logger.exception("[event_post_pipeline] retry unblock failed for task=%s", task_id)
            return _json_error(f"unblock_failed: {exc}", 502)
        if unblocked:
            await asyncio.to_thread(pipeline.track_stylus_retry_requested, self._db_url, task_id, ok=True)
            return web.json_response({"status": "retrying"}, status=202)
        await asyncio.to_thread(
            pipeline.track_stylus_retry_requested, self._db_url, task_id, ok=False,
            detail="task was not in a blocked/scheduled state",
        )
        return _json_error("task is not currently blocked or scheduled — it may have already been retried or resolved", 409)

    def _run_unblock_task(self, task_id: str) -> bool:
        from hermes_cli import kanban_db as kb
        from hermes_cli import kanban_db_connect as kbc

        conn = kbc.connect(board=self._board)
        try:
            return kb.unblock_task(conn, task_id)
        finally:
            conn.close()

    async def _retry_notify(self, task_id: str, message: str) -> "web.Response":
        try:
            await whatsapp_notify.send_whatsapp_link(message)
        except whatsapp_notify.WhatsAppNotifyError as exc:
            logger.error("[event_post_pipeline] retry notify send failed for task=%s: %s", task_id, exc)
            await asyncio.to_thread(pipeline.track_notification, self._db_url, task_id, ok=False, detail=str(exc))
            return _json_error(f"notify_failed: {exc}", 502)
        await asyncio.to_thread(pipeline.track_notification, self._db_url, task_id, ok=True)
        return web.json_response({"status": "sent"})


def _build_adapter(config: PlatformConfig) -> EventPostPipelineAdapter:
    return EventPostPipelineAdapter(config)
