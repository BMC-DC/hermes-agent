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
