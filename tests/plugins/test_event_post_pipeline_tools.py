"""Tests for the agent-callable tools Vidu (the general orchestrator) uses to
query/trigger the event-post-pipeline (``plugins/platforms/event_post_pipeline/tools.py``).

``social_post_status`` is tested against the same hand-rolled fake Postgres
connection ``test_event_post_pipeline_db.py`` uses (no real Postgres in this
suite). ``social_post_submit`` is tested end-to-end against a real, throwaway
Kanban SQLite board — same convention as ``test_event_post_pipeline_pipeline.py``
— since its whole point is to run the real ``pipeline.handle_intake`` path.
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


# --- social_post_submit_handler --------------------------------------------------------


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def test_submit_handler_reports_unconfigured_profile(monkeypatch):
    monkeypatch.setattr(tools, "_load_pipeline_extra", lambda: {})
    out = json.loads(tools.social_post_submit_handler({"description": "A talk on mindfulness"}))
    assert "error" in out
    assert "not configured" in out["error"]


def test_submit_handler_requires_description(monkeypatch):
    monkeypatch.setattr(tools, "_load_pipeline_extra", lambda: {"port": 8645})
    out = json.loads(tools.social_post_submit_handler({"description": "  "}))
    assert "error" in out
    assert "description" in out["error"]


def test_submit_handler_creates_stylus_task(kanban_home, tmp_path, monkeypatch):
    monkeypatch.setattr(tools, "_load_pipeline_extra", lambda: {"store_path": str(tmp_path / "store.json")})
    out = json.loads(tools.social_post_submit_handler({
        "description": "A day-long meditation retreat with a visiting monk.",
        "date": "2026-09-27",
        "image_urls": ["https://example.com/a.jpg", "https://example.com/b.jpg", "https://example.com/c.jpg"],
        "submitter_name": "Amila",
        "submitter_phone": "+15551234567",
    }))
    assert out["status"] == "accepted"
    assert out["submission_id"].startswith("chat-")

    conn = kbc.connect()
    try:
        task = kb.get_task(conn, out["task_id"])
    finally:
        conn.close()
    assert task.assignee == "stylus"
    assert task.tenant == "event-post-pipeline-v2"
    assert "meditation retreat" in task.body.lower()
    assert "https://example.com/a.jpg" in task.body  # first
    assert "https://example.com/c.jpg" in task.body  # last
    assert "https://example.com/b.jpg" in task.body  # rest


def test_submit_handler_builds_valid_event_pack_with_one_image():
    pack = tools._build_event_pack({"description": "x", "image_urls": ["https://example.com/only.jpg"]})
    assert pack["first"] == {"finalUrl": "https://example.com/only.jpg"}
    assert pack["last"] == {}
    assert pack["rest"] == []


def test_submit_handler_builds_valid_event_pack_with_no_images():
    pack = tools._build_event_pack({"description": "x"})
    assert pack["first"] == {}
    assert pack["last"] == {}
    assert pack["rest"] == []
