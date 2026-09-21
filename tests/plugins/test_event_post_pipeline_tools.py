"""Tests for the agent-callable tools Vidu (the general orchestrator) uses to
query/unstick the event-post-pipeline (``plugins/platforms/event_post_pipeline/tools.py``).

``social_post_status`` is tested against the same hand-rolled fake Postgres
connection ``test_event_post_pipeline_db.py`` uses (no real Postgres in this
suite). ``social_post_retry``'s ``stylus_blocked`` path is tested end-to-end
against a real, throwaway Kanban SQLite board (same convention as
``test_event_post_pipeline_adapter.py``'s own retry tests, since this is the
same underlying action via a different transport); its ``notify`` path mocks
``whatsapp_notify.send_whatsapp_link``, same as the adapter tests do.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from plugins.platforms.event_post_pipeline import db, tools


class _FakeCursor:
    def __init__(self, conn: "_FakeConnection"):
        self._conn = conn
        self._response: dict = {"one": None, "all": []}

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *exc_info) -> bool:
        return False

    def execute(self, sql: str, params=None) -> None:
        self._conn.executed.append((sql, params))
        self._response = self._conn.responses.pop(0) if self._conn.responses else {"one": None, "all": []}

    def fetchone(self):
        return self._response.get("one")

    def fetchall(self):
        return self._response.get("all") or []


class _FakeConnection:
    def __init__(self, responses: list[dict]):
        self.responses = list(responses)
        self.executed: list[tuple] = []

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self)

    def close(self) -> None:
        pass


# --- check_pipeline_tools_available -------------------------------------------------------


def test_check_pipeline_tools_available_false_when_unconfigured(monkeypatch):
    monkeypatch.setattr(tools, "_load_pipeline_extra", lambda: {})
    assert tools.check_pipeline_tools_available() is False


def test_check_pipeline_tools_available_true_when_configured(monkeypatch):
    monkeypatch.setattr(tools, "_load_pipeline_extra", lambda: {"port": 8645})
    assert tools.check_pipeline_tools_available() is True


# --- social_post_status_handler --------------------------------------------------------


def test_status_handler_reports_unconfigured_profile(monkeypatch):
    monkeypatch.setattr(tools, "_load_pipeline_extra", lambda: {})
    out = json.loads(tools.social_post_status_handler({}))
    assert "error" in out
    assert "not configured" in out["error"]


def test_status_handler_reports_no_database(monkeypatch):
    monkeypatch.setattr(tools, "_load_pipeline_extra", lambda: {"port": 8645})
    monkeypatch.setattr(db, "resolve_database_url", lambda extra=None: "")
    out = json.loads(tools.social_post_status_handler({}))
    assert "error" in out
    assert "SPP_DATABASE_URL" in out["error"]


def test_status_handler_returns_recent_runs(monkeypatch):
    monkeypatch.setattr(tools, "_load_pipeline_extra", lambda: {"port": 8645})
    monkeypatch.setattr(db, "resolve_database_url", lambda extra=None: "postgresql://x/db")
    rows = [
        {"task_id": "t1", "title": "Retreat Day", "state": "waiting", "current_step": "awaiting_review", "updated_at": "2026-09-21 10:00:00"},
    ]
    fake_conn = _FakeConnection([{"all": rows}])
    monkeypatch.setattr(db, "get_connection", lambda url: fake_conn)
    out = json.loads(tools.social_post_status_handler({"query": "retreat", "limit": 5}))
    assert out["count"] == 1
    assert out["runs"][0]["task_id"] == "t1"
    assert out["runs"][0]["state"] == "waiting"


def test_status_handler_connection_failure_returns_tool_error(monkeypatch):
    monkeypatch.setattr(tools, "_load_pipeline_extra", lambda: {"port": 8645})
    monkeypatch.setattr(db, "resolve_database_url", lambda extra=None: "postgresql://x/db")

    def _boom(url):
        raise db.DatabaseError("could not connect")

    monkeypatch.setattr(db, "get_connection", _boom)
    out = json.loads(tools.social_post_status_handler({}))
    assert "error" in out


# --- social_post_status_handler(task_id=...): the submitted pack ------------------------


def test_status_handler_task_id_returns_full_pack_with_submitter_and_drafts(monkeypatch):
    monkeypatch.setattr(tools, "_load_pipeline_extra", lambda: {"port": 8645})
    monkeypatch.setattr(db, "resolve_database_url", lambda extra=None: "postgresql://x/db")
    submission = {
        "id": 7, "task_id": "t1", "event_title": "Retreat Day", "event_date": "2026-09-27",
        "description": "A day-long retreat.", "quote": "Be present.", "images": ["https://x/1.jpg"],
        "submitted_by": 3, "status": "pending",
    }
    curator = {"id": 3, "name": "Amila"}
    drafts = [
        {"platform": "fb", "round": 1, "status": "approved", "text": "FB copy", "comment": None},
        {"platform": "ig", "round": 1, "status": "pending", "text": "IG copy", "comment": None},
    ]
    fake_conn = _FakeConnection([{"one": submission}, {"one": curator}, {"all": drafts}])
    monkeypatch.setattr(db, "get_connection", lambda url: fake_conn)

    out = json.loads(tools.social_post_status_handler({"task_id": "t1"}))

    assert out["task_id"] == "t1"
    assert out["event_title"] == "Retreat Day"
    assert out["description"] == "A day-long retreat."
    assert out["images"] == ["https://x/1.jpg"]
    assert out["submitted_by"] == "Amila"
    assert len(out["drafts"]) == 2
    assert out["drafts"][0]["platform"] == "fb"
    assert out["drafts"][0]["text"] == "FB copy"


def test_status_handler_task_id_no_submitter_skips_curator_lookup(monkeypatch):
    monkeypatch.setattr(tools, "_load_pipeline_extra", lambda: {"port": 8645})
    monkeypatch.setattr(db, "resolve_database_url", lambda extra=None: "postgresql://x/db")
    submission = {
        "id": 7, "task_id": "t1", "event_title": "Retreat Day", "event_date": None,
        "description": "A day-long retreat.", "quote": None, "images": [],
        "submitted_by": None, "status": "pending",
    }
    fake_conn = _FakeConnection([{"one": submission}, {"all": []}])
    monkeypatch.setattr(db, "get_connection", lambda url: fake_conn)

    out = json.loads(tools.social_post_status_handler({"task_id": "t1"}))

    assert out["submitted_by"] is None
    assert out["drafts"] == []
    # Only 2 queries ran (submission + drafts) -- no curator lookup attempted.
    assert len(fake_conn.executed) == 2


def test_status_handler_task_id_not_found_returns_tool_error(monkeypatch):
    monkeypatch.setattr(tools, "_load_pipeline_extra", lambda: {"port": 8645})
    monkeypatch.setattr(db, "resolve_database_url", lambda extra=None: "postgresql://x/db")
    fake_conn = _FakeConnection([{"one": None}])
    monkeypatch.setattr(db, "get_connection", lambda url: fake_conn)

    out = json.loads(tools.social_post_status_handler({"task_id": "no-such-task"}))

    assert "error" in out
    assert "no-such-task" in out["error"]


# --- social_post_retry_handler ----------------------------------------------------------


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def test_retry_handler_reports_unconfigured_profile(monkeypatch):
    monkeypatch.setattr(tools, "_load_pipeline_extra", lambda: {})
    out = json.loads(tools.social_post_retry_handler({"task_id": "t1", "kind": "notify", "message": "hi"}))
    assert "error" in out
    assert "not configured" in out["error"]


def test_retry_handler_requires_task_id(monkeypatch):
    monkeypatch.setattr(tools, "_load_pipeline_extra", lambda: {"port": 8645})
    out = json.loads(tools.social_post_retry_handler({"kind": "notify", "message": "hi"}))
    assert "error" in out
    assert "task_id" in out["error"]


def test_retry_handler_rejects_unknown_kind(monkeypatch):
    monkeypatch.setattr(tools, "_load_pipeline_extra", lambda: {"port": 8645})
    out = json.loads(tools.social_post_retry_handler({"task_id": "t1", "kind": "bogus"}))
    assert "error" in out
    assert "kind" in out["error"]


def test_retry_handler_notify_requires_message(monkeypatch):
    monkeypatch.setattr(tools, "_load_pipeline_extra", lambda: {"port": 8645})
    out = json.loads(tools.social_post_retry_handler({"task_id": "t1", "kind": "notify"}))
    assert "error" in out
    assert "message" in out["error"]


def test_retry_handler_notify_success(monkeypatch):
    monkeypatch.setattr(tools, "_load_pipeline_extra", lambda: {"port": 8645})

    async def _fake_send(message):
        return None

    monkeypatch.setattr(tools.whatsapp_notify, "send_whatsapp_link", _fake_send)
    out = json.loads(tools.social_post_retry_handler({"task_id": "t1", "kind": "notify", "message": "please retry"}))
    assert out == {"status": "sent", "task_id": "t1"}


def test_retry_handler_notify_failure_returns_tool_error(monkeypatch):
    monkeypatch.setattr(tools, "_load_pipeline_extra", lambda: {"port": 8645})

    async def _fake_send(message):
        raise tools.whatsapp_notify.WhatsAppNotifyError("no home channel configured")

    monkeypatch.setattr(tools.whatsapp_notify, "send_whatsapp_link", _fake_send)
    out = json.loads(tools.social_post_retry_handler({"task_id": "t1", "kind": "notify", "message": "please retry"}))
    assert "error" in out


def test_retry_handler_stylus_blocked_unblocks_task(kanban_home, monkeypatch):
    monkeypatch.setattr(tools, "_load_pipeline_extra", lambda: {"port": 8645})
    conn = kbc.connect()
    try:
        task_id = kb.create_task(conn, title="Draft social copy: test", body="", assignee="stylus",
                                  idempotency_key="tools-retry-1", tenant="event-post-pipeline-v2")
        assert kb.block_task(conn, task_id, reason="stuck waiting")
        assert kb.get_task(conn, task_id).status == "blocked"
    finally:
        conn.close()

    out = json.loads(tools.social_post_retry_handler({"task_id": task_id, "kind": "stylus_blocked"}))
    assert out == {"status": "retrying", "task_id": task_id}

    conn = kbc.connect()
    try:
        assert kb.get_task(conn, task_id).status != "blocked"
    finally:
        conn.close()


def test_retry_handler_stylus_blocked_returns_error_when_not_blocked(kanban_home, monkeypatch):
    monkeypatch.setattr(tools, "_load_pipeline_extra", lambda: {"port": 8645})
    out = json.loads(tools.social_post_retry_handler({"task_id": "no-such-task", "kind": "stylus_blocked"}))
    assert "error" in out
