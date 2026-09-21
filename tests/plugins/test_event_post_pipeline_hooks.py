"""Regression test for the silent-notification-failure bug found in production
2026-09-20: ``hooks.py`` called bare ``asyncio.run(...)`` from a Kanban worker
context that may already have a running event loop, which raises
``RuntimeError: asyncio.run() cannot be called from a running event loop`` —
and Hermes's own hook dispatcher (``_fire_kanban_lifecycle_hook``) swallows
that at logging.DEBUG level, so the failure was completely invisible. Every
real submission's "review ready" WhatsApp notification silently never sent.
"""

from __future__ import annotations

import asyncio
import types

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from plugins.platforms.event_post_pipeline import hooks, pipeline
from plugins.platforms.event_post_pipeline.hooks import _run_async


def test_run_async_with_no_existing_event_loop():
    calls = []

    async def _coro():
        calls.append("ran")

    _run_async(_coro)
    assert calls == ["ran"]


def test_run_async_inside_an_already_running_event_loop():
    """The exact failure mode found in production: this call happens from a
    thread whose asyncio loop is already running (simulated here by driving
    _run_async from inside a coroutine executed via asyncio.run at the test
    level). Before the fix, the equivalent bare `asyncio.run(...)` call here
    would raise RuntimeError; it must not, and the inner coroutine must
    actually execute."""
    calls = []

    async def _inner_coro():
        calls.append("ran")

    async def _outer():
        # _run_async is a sync function called from within a running loop —
        # exactly hooks.py's real call shape (on_kanban_task_completed is sync,
        # but may execute on a thread whose loop is already active).
        _run_async(_inner_coro)

    asyncio.run(_outer())
    assert calls == ["ran"]


# --- on_kanban_task_blocked: visualizer wiring for a Kanban-dispatcher-level event -----------
#
# ``kanban_task_blocked`` is not something this plugin's own code triggers (unlike
# ``kanban_task_completed``, which pipeline.py's own handle_stylus_completion reacts to);
# it's purely a lifecycle event the Kanban dispatcher fires. These tests fake out Kanban
# and Postgres entirely and only check the callback's own filtering/wiring logic.


class _FakeTask:
    def __init__(self, tenant, idempotency_key, title="Draft social copy: X"):
        self.tenant = tenant
        self.idempotency_key = idempotency_key
        self.title = title


def _fake_connect(board=None):
    return types.SimpleNamespace(close=lambda: None)


def test_on_kanban_task_blocked_ignores_non_stylus_assignee(monkeypatch):
    calls = []
    monkeypatch.setattr(hooks, "_load_pipeline_extra", lambda: {"board": None})
    monkeypatch.setattr(pipeline, "track_stylus_blocked", lambda *a, **kw: calls.append((a, kw)))
    hooks.on_kanban_task_blocked(task_id="t1", assignee="whatsapp", reason="x")
    assert calls == []


def test_on_kanban_task_blocked_ignores_unconfigured_profile(monkeypatch):
    calls = []
    monkeypatch.setattr(hooks, "_load_pipeline_extra", lambda: {})
    monkeypatch.setattr(pipeline, "track_stylus_blocked", lambda *a, **kw: calls.append((a, kw)))
    hooks.on_kanban_task_blocked(task_id="t1", assignee="stylus", reason="x")
    assert calls == []


def test_on_kanban_task_blocked_ignores_no_database_url_configured(monkeypatch):
    calls = []
    monkeypatch.setattr(hooks, "_load_pipeline_extra", lambda: {"board": None})
    monkeypatch.setattr(hooks.db, "resolve_database_url", lambda extra: "")
    monkeypatch.setattr(kbc, "connect", lambda **kw: (_ for _ in ()).throw(AssertionError("should not connect to Kanban")))
    monkeypatch.setattr(pipeline, "track_stylus_blocked", lambda *a, **kw: calls.append((a, kw)))
    hooks.on_kanban_task_blocked(task_id="t1", assignee="stylus", reason="x")
    assert calls == []


def test_on_kanban_task_blocked_ignores_other_tenants(monkeypatch):
    calls = []
    monkeypatch.setattr(hooks, "_load_pipeline_extra", lambda: {"board": None})
    monkeypatch.setattr(hooks.db, "resolve_database_url", lambda extra: "postgresql://x/y")
    monkeypatch.setattr(kbc, "connect", _fake_connect)
    monkeypatch.setattr(kb, "get_task", lambda conn, task_id: _FakeTask(tenant="some-other-tenant", idempotency_key="t1"))
    monkeypatch.setattr(pipeline, "track_stylus_blocked", lambda *a, **kw: calls.append((a, kw)))
    hooks.on_kanban_task_blocked(task_id="t1", assignee="stylus", reason="boom")
    assert calls == []


def test_on_kanban_task_blocked_tracks_root_task_id_for_a_refine_round(monkeypatch):
    calls = []
    monkeypatch.setattr(hooks, "_load_pipeline_extra", lambda: {"board": None})
    monkeypatch.setattr(hooks.db, "resolve_database_url", lambda extra: "postgresql://x/y")
    monkeypatch.setattr(kbc, "connect", _fake_connect)
    monkeypatch.setattr(
        kb, "get_task",
        lambda conn, task_id: _FakeTask(
            tenant=pipeline.TENANT, idempotency_key="root-1-fb-r2", title="Redraft FB (round 2): X",
        ),
    )
    monkeypatch.setattr(pipeline, "track_stylus_blocked", lambda *a, **kw: calls.append((a, kw)))

    hooks.on_kanban_task_blocked(task_id="root-1-fb-r2", assignee="stylus", reason="LLM call failed")

    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args == ("postgresql://x/y", "root-1")  # the root task id, not the refine subtask id
    assert kwargs == {"title": "Redraft FB (round 2): X", "reason": "LLM call failed"}


def test_on_kanban_task_blocked_alerts_the_group_with_a_visualizer_link(monkeypatch):
    """A blocked task hasn't told anyone anything yet (unlike a failed notification,
    which already attempted something and can be retried) — this is the one failure
    mode that needs its own alert, decided 2026-09-21 after live testing found it
    otherwise sat silently stuck with no signal to anyone."""
    sent = []
    monkeypatch.setattr(hooks, "_load_pipeline_extra", lambda: {"board": None, "review_base_url": "https://bmcposts.vercel.app"})
    monkeypatch.setattr(hooks.db, "resolve_database_url", lambda extra: "postgresql://x/y")
    monkeypatch.setattr(kbc, "connect", _fake_connect)
    monkeypatch.setattr(
        kb, "get_task",
        lambda conn, task_id: _FakeTask(tenant=pipeline.TENANT, idempotency_key="root-1", title="Draft social copy: X"),
    )
    monkeypatch.setattr(pipeline, "track_stylus_blocked", lambda *a, **kw: None)

    async def fake_send(message):
        sent.append(message)

    monkeypatch.setattr(hooks.whatsapp_notify, "send_whatsapp_link", fake_send)

    hooks.on_kanban_task_blocked(task_id="root-1", assignee="stylus", reason="LLM call failed")

    assert len(sent) == 1
    assert "root-1" in sent[0]
    assert "https://bmcposts.vercel.app/visualizer?taskId=root-1" in sent[0]
    assert "LLM call failed" in sent[0]
    assert "Draft social copy: X" in sent[0]


def test_on_kanban_task_blocked_alert_failure_never_raises(monkeypatch):
    monkeypatch.setattr(hooks, "_load_pipeline_extra", lambda: {"board": None})
    monkeypatch.setattr(hooks.db, "resolve_database_url", lambda extra: "postgresql://x/y")
    monkeypatch.setattr(kbc, "connect", _fake_connect)
    monkeypatch.setattr(
        kb, "get_task",
        lambda conn, task_id: _FakeTask(tenant=pipeline.TENANT, idempotency_key="root-1"),
    )
    monkeypatch.setattr(pipeline, "track_stylus_blocked", lambda *a, **kw: None)

    async def boom(message):
        raise RuntimeError("bridge down")

    monkeypatch.setattr(hooks.whatsapp_notify, "send_whatsapp_link", boom)

    hooks.on_kanban_task_blocked(task_id="root-1", assignee="stylus", reason="x")  # must not raise


def test_on_kanban_task_blocked_falls_back_to_task_id_when_not_a_refine_round(monkeypatch):
    calls = []
    monkeypatch.setattr(hooks, "_load_pipeline_extra", lambda: {"board": None})
    monkeypatch.setattr(hooks.db, "resolve_database_url", lambda extra: "postgresql://x/y")
    monkeypatch.setattr(kbc, "connect", _fake_connect)
    monkeypatch.setattr(
        kb, "get_task",
        lambda conn, task_id: _FakeTask(tenant=pipeline.TENANT, idempotency_key="root-1", title="Draft social copy: X"),
    )
    monkeypatch.setattr(pipeline, "track_stylus_blocked", lambda *a, **kw: calls.append((a, kw)))

    hooks.on_kanban_task_blocked(task_id="root-1", assignee="stylus", reason="timeout")

    args, kwargs = calls[0]
    assert args == ("postgresql://x/y", "root-1")
    assert kwargs["reason"] == "timeout"


def test_on_kanban_task_blocked_tracking_failure_never_raises(monkeypatch):
    """Non-fatal by design (this whole hook fires inside a Kanban-dispatcher process it
    must never disrupt)."""
    monkeypatch.setattr(hooks, "_load_pipeline_extra", lambda: {"board": None})
    monkeypatch.setattr(hooks.db, "resolve_database_url", lambda extra: "postgresql://x/y")
    monkeypatch.setattr(kbc, "connect", _fake_connect)
    monkeypatch.setattr(
        kb, "get_task", lambda conn, task_id: _FakeTask(tenant=pipeline.TENANT, idempotency_key="root-1"),
    )

    def _boom(*a, **kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(pipeline, "track_stylus_blocked", _boom)
    hooks.on_kanban_task_blocked(task_id="root-1", assignee="stylus", reason="x")  # must not raise
