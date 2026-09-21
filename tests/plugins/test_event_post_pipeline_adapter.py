"""HTTP-level tests for the event-post-pipeline plugin's own listener
(``adapter.py``) — real signed requests against a real bound socket and a
real throwaway Kanban board, no mocking of the adapter itself."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from pathlib import Path

import aiohttp
import pytest

from gateway.config import PlatformConfig
from hermes_cli import kanban_db as kb
from plugins.platforms.event_post_pipeline.adapter import EventPostPipelineAdapter

TEST_PORT = 18645
SECRET = "adapter-test-secret"


def _sign(body: bytes, timestamp: str) -> str:
    return hmac.new(SECRET.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()


def _signed_headers(body: bytes) -> dict:
    timestamp = str(int(time.time()))
    return {"X-Webhook-Timestamp": timestamp, "X-Webhook-Signature-V2": _sign(body, timestamp), "Content-Type": "application/json"}


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
async def running_adapter(kanban_home, tmp_path):
    config = PlatformConfig(enabled=True, extra={
        "port": TEST_PORT, "secret": SECRET, "review_create_secret": "unused-in-these-tests",
        "store_path": str(tmp_path / "store.json"),
    })
    adapter = EventPostPipelineAdapter(config)
    assert await adapter.connect()
    try:
        yield adapter
    finally:
        await adapter.disconnect()


EVENT_PACK = {
    "submissionId": "adapter-test-1", "date": "2026-09-20", "description": "A test event.",
    "quote": "Be here now.", "first": {"finalUrl": "https://example.com/first.jpg"},
    "last": {"finalUrl": "https://example.com/last.jpg"}, "rest": [],
}


@pytest.mark.asyncio
async def test_intake_rejects_bad_signature(running_adapter):
    body = json.dumps(EVENT_PACK).encode()
    async with aiohttp.ClientSession() as session:
        async with session.post(f"http://127.0.0.1:{TEST_PORT}/intake", data=body,
                                 headers={"X-Webhook-Timestamp": str(int(time.time())),
                                          "X-Webhook-Signature-V2": "deadbeef", "Content-Type": "application/json"}) as resp:
            assert resp.status == 401


@pytest.mark.asyncio
async def test_intake_creates_kanban_task(running_adapter, kanban_home):
    body = json.dumps(EVENT_PACK).encode()
    async with aiohttp.ClientSession() as session:
        async with session.post(f"http://127.0.0.1:{TEST_PORT}/intake", data=body, headers=_signed_headers(body)) as resp:
            assert resp.status == 202
            payload = await resp.json()
            assert payload["status"] == "accepted"
            task_id = payload["task_id"]

    from hermes_cli import kanban_db_connect as kbc
    conn = kbc.connect()
    try:
        task = kb.get_task(conn, task_id)
        assert task.assignee == "stylus"
        assert task.idempotency_key == "adapter-test-1"
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_review_action_rejects_invalid_shape(running_adapter):
    body = json.dumps({"taskId": "t_x", "platform": "not-a-platform", "action": "approved"}).encode()
    async with aiohttp.ClientSession() as session:
        async with session.post(f"http://127.0.0.1:{TEST_PORT}/review-action", data=body, headers=_signed_headers(body)) as resp:
            assert resp.status == 400


@pytest.mark.asyncio
async def test_review_action_approve_comments_on_task(running_adapter, kanban_home):
    from hermes_cli import kanban_db_connect as kbc
    conn = kbc.connect()
    try:
        task_id = kb.create_task(conn, title="Draft social copy: test", body="", assignee="stylus",
                                  idempotency_key="adapter-test-2", tenant="event-post-pipeline-v2")
    finally:
        conn.close()

    body = json.dumps({"taskId": task_id, "platform": "fb", "action": "approved", "comment": ""}).encode()
    async with aiohttp.ClientSession() as session:
        async with session.post(f"http://127.0.0.1:{TEST_PORT}/review-action", data=body, headers=_signed_headers(body)) as resp:
            assert resp.status == 200
            payload = await resp.json()
            assert payload["ok"] is True
            assert payload["action"] == "approved"

    conn = kbc.connect()
    try:
        comments = kb.list_comments(conn, task_id)
        assert any("FB APPROVED" in c.body for c in comments)
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_retry_rejects_bad_signature(running_adapter):
    body = json.dumps({"taskId": "t_x", "kind": "notify", "message": "hi"}).encode()
    async with aiohttp.ClientSession() as session:
        async with session.post(f"http://127.0.0.1:{TEST_PORT}/retry", data=body,
                                 headers={"X-Webhook-Timestamp": str(int(time.time())),
                                          "X-Webhook-Signature-V2": "deadbeef", "Content-Type": "application/json"}) as resp:
            assert resp.status == 401


@pytest.mark.asyncio
async def test_retry_rejects_missing_task_id(running_adapter):
    body = json.dumps({"kind": "notify", "message": "hi"}).encode()
    async with aiohttp.ClientSession() as session:
        async with session.post(f"http://127.0.0.1:{TEST_PORT}/retry", data=body, headers=_signed_headers(body)) as resp:
            assert resp.status == 400


@pytest.mark.asyncio
async def test_retry_rejects_unknown_kind(running_adapter):
    body = json.dumps({"taskId": "adapter-retry-1", "kind": "bogus"}).encode()
    async with aiohttp.ClientSession() as session:
        async with session.post(f"http://127.0.0.1:{TEST_PORT}/retry", data=body, headers=_signed_headers(body)) as resp:
            assert resp.status == 400


@pytest.mark.asyncio
async def test_retry_notify_requires_message(running_adapter):
    body = json.dumps({"taskId": "adapter-retry-1", "kind": "notify"}).encode()
    async with aiohttp.ClientSession() as session:
        async with session.post(f"http://127.0.0.1:{TEST_PORT}/retry", data=body, headers=_signed_headers(body)) as resp:
            assert resp.status == 400
    body = json.dumps({"taskId": "adapter-retry-1", "kind": "notify", "message": "   "}).encode()
    async with aiohttp.ClientSession() as session:
        async with session.post(f"http://127.0.0.1:{TEST_PORT}/retry", data=body, headers=_signed_headers(body)) as resp:
            assert resp.status == 400


@pytest.mark.asyncio
async def test_retry_notify_success(running_adapter, monkeypatch):
    from plugins.platforms.event_post_pipeline import adapter as adapter_module

    async def _fake_send(message):
        return None

    monkeypatch.setattr(adapter_module.whatsapp_notify, "send_whatsapp_link", _fake_send)
    body = json.dumps({"taskId": "adapter-retry-1", "kind": "notify", "message": "please retry"}).encode()
    async with aiohttp.ClientSession() as session:
        async with session.post(f"http://127.0.0.1:{TEST_PORT}/retry", data=body, headers=_signed_headers(body)) as resp:
            assert resp.status == 200
            payload = await resp.json()
            assert payload == {"status": "sent"}


@pytest.mark.asyncio
async def test_retry_notify_failure_returns_502(running_adapter, monkeypatch):
    from plugins.platforms.event_post_pipeline import adapter as adapter_module
    from plugins.platforms.event_post_pipeline import whatsapp_notify

    async def _fake_send(message):
        raise whatsapp_notify.WhatsAppNotifyError("no home channel configured")

    monkeypatch.setattr(adapter_module.whatsapp_notify, "send_whatsapp_link", _fake_send)
    body = json.dumps({"taskId": "adapter-retry-1", "kind": "notify", "message": "please retry"}).encode()
    async with aiohttp.ClientSession() as session:
        async with session.post(f"http://127.0.0.1:{TEST_PORT}/retry", data=body, headers=_signed_headers(body)) as resp:
            assert resp.status == 502


@pytest.mark.asyncio
async def test_retry_stylus_blocked_unblocks_task(running_adapter, kanban_home):
    from hermes_cli import kanban_db_connect as kbc

    conn = kbc.connect()
    try:
        task_id = kb.create_task(conn, title="Draft social copy: test", body="", assignee="stylus",
                                  idempotency_key="adapter-retry-2", tenant="event-post-pipeline-v2")
        assert kb.block_task(conn, task_id, reason="stuck waiting")
        assert kb.get_task(conn, task_id).status == "blocked"
    finally:
        conn.close()

    body = json.dumps({"taskId": task_id, "kind": "stylus_blocked"}).encode()
    async with aiohttp.ClientSession() as session:
        async with session.post(f"http://127.0.0.1:{TEST_PORT}/retry", data=body, headers=_signed_headers(body)) as resp:
            assert resp.status == 202
            payload = await resp.json()
            assert payload == {"status": "retrying"}

    conn = kbc.connect()
    try:
        assert kb.get_task(conn, task_id).status != "blocked"
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_retry_stylus_blocked_returns_409_when_not_blocked(running_adapter, kanban_home):
    from hermes_cli import kanban_db_connect as kbc

    conn = kbc.connect()
    try:
        task_id = kb.create_task(conn, title="Draft social copy: test", body="", assignee="stylus",
                                  idempotency_key="adapter-retry-3", tenant="event-post-pipeline-v2")
        assert kb.get_task(conn, task_id).status == "ready"
    finally:
        conn.close()

    body = json.dumps({"taskId": task_id, "kind": "stylus_blocked"}).encode()
    async with aiohttp.ClientSession() as session:
        async with session.post(f"http://127.0.0.1:{TEST_PORT}/retry", data=body, headers=_signed_headers(body)) as resp:
            assert resp.status == 409
