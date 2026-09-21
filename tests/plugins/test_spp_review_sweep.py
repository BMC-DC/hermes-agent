"""Tests for the SPP v2 review-sweep cron script
(``scripts/spp_review_sweep.py``): the two independent, unrelated checks it
runs every tick (1hr lock-timeout release, 2hr pending-review reminder).

Mocks ``db.*`` and ``whatsapp_notify.send_whatsapp_link`` rather than a real
Postgres or a real WhatsApp send — same test-double approach as
``tests/plugins/test_event_post_pipeline_db.py`` for the same reason (no
existing fixture for a live Postgres server in this repo).

Decided 2026-09-21: every pipeline notification goes to the shared
social-media WhatsApp group (``send_whatsapp_link``), never an individual
curator's DM (``notify_curators``) — the team wants shared visibility into
all activity rather than fragmented per-person messages. These tests no
longer assert anything about curator-pool resolution for routing purposes.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from plugins.platforms.event_post_pipeline import db  # noqa: E402
from scripts import spp_review_sweep as sweep  # noqa: E402


class _FakeConn:
    pass


def test_sweep_lock_timeouts_notifies_the_group(monkeypatch):
    released_rows = [{"id": 1, "slug": "abc", "submitted_by": 7}]
    monkeypatch.setattr(db, "release_expired_locks", lambda conn, ttl_seconds: released_rows)
    notified = []

    async def fake_send(message):
        notified.append(message)

    monkeypatch.setattr(sweep.whatsapp_notify, "send_whatsapp_link", fake_send)

    released = sweep.sweep_lock_timeouts(_FakeConn(), lock_ttl_seconds=3600)

    assert released == released_rows
    assert len(notified) == 1
    assert "abc" in notified[0]


def test_sweep_lock_timeouts_no_expired_locks_sends_nothing(monkeypatch):
    monkeypatch.setattr(db, "release_expired_locks", lambda conn, ttl_seconds: [])
    notified = []

    async def fake_send(message):
        notified.append(message)

    monkeypatch.setattr(sweep.whatsapp_notify, "send_whatsapp_link", fake_send)

    released = sweep.sweep_lock_timeouts(_FakeConn(), lock_ttl_seconds=3600)

    assert released == []
    assert notified == []


def test_sweep_lock_timeouts_sends_one_group_message_per_release(monkeypatch):
    released_rows = [{"id": 1, "slug": "abc", "submitted_by": 7}, {"id": 2, "slug": "def", "submitted_by": 8}]
    monkeypatch.setattr(db, "release_expired_locks", lambda conn, ttl_seconds: released_rows)
    notified = []

    async def fake_send(message):
        notified.append(message)

    monkeypatch.setattr(sweep.whatsapp_notify, "send_whatsapp_link", fake_send)

    sweep.sweep_lock_timeouts(_FakeConn(), lock_ttl_seconds=3600)

    assert len(notified) == 2
    assert "abc" in notified[0]
    assert "def" in notified[1]


def test_sweep_pending_reminders_notifies_the_group_and_stamps(monkeypatch):
    due_rows = [{"id": 10, "slug": "xyz"}, {"id": 11, "slug": "qrs"}]
    monkeypatch.setattr(db, "find_due_reminders", lambda conn, interval_seconds: due_rows)
    stamped = []
    monkeypatch.setattr(db, "stamp_reminder_sent", lambda conn, sid: stamped.append(sid))
    notified = []

    async def fake_send(message):
        notified.append(message)

    monkeypatch.setattr(sweep.whatsapp_notify, "send_whatsapp_link", fake_send)

    due = sweep.sweep_pending_reminders(_FakeConn(), reminder_interval_seconds=7200)

    assert due == due_rows
    assert stamped == [10, 11]
    assert len(notified) == 2
    assert "xyz" in notified[0] and "qrs" in notified[1]


def test_sweep_pending_reminders_none_due_stamps_nothing(monkeypatch):
    monkeypatch.setattr(db, "find_due_reminders", lambda conn, interval_seconds: [])
    stamped = []
    monkeypatch.setattr(db, "stamp_reminder_sent", lambda conn, sid: stamped.append(sid))

    due = sweep.sweep_pending_reminders(_FakeConn(), reminder_interval_seconds=7200)

    assert due == []
    assert stamped == []


def test_run_sweep_summarizes_both_independent_checks(monkeypatch):
    monkeypatch.setattr(sweep, "sweep_lock_timeouts", lambda conn, lock_ttl_seconds, **kw: [{"id": 1}, {"id": 2}])
    monkeypatch.setattr(sweep, "sweep_pending_reminders", lambda conn, reminder_interval_seconds, **kw: [{"id": 3}])

    summary = sweep.run_sweep(_FakeConn(), lock_ttl_seconds=3600, reminder_interval_seconds=7200)

    assert summary == {"locks_released": 2, "reminders_sent": 1}


def test_reminder_message_uses_the_real_review_link_not_the_bare_slug():
    """Regression test (2026-09-21): the reminder used to say "the review for 'abc'
    (submission #1)" -- a bare slug, not a clickable link. Fixed to build the actual
    /review/<slug> URL."""
    message = sweep._reminder_message({"id": 1, "slug": "abc123"}, "https://spp.buddhameditationdc.org")
    assert message == "Reminder: the review for https://spp.buddhameditationdc.org/review/abc123 is still waiting."


def test_lock_timeout_message_uses_the_real_review_link():
    message = sweep._lock_timeout_message({"id": 1, "slug": "abc123"}, "https://spp.buddhameditationdc.org")
    assert "https://spp.buddhameditationdc.org/review/abc123" in message


def test_sweep_pending_reminders_passes_review_base_url_through(monkeypatch):
    due_rows = [{"id": 10, "slug": "xyz"}]
    monkeypatch.setattr(db, "find_due_reminders", lambda conn, interval_seconds: due_rows)
    monkeypatch.setattr(db, "stamp_reminder_sent", lambda conn, sid: None)
    notified = []

    async def fake_send(message):
        notified.append(message)

    monkeypatch.setattr(sweep.whatsapp_notify, "send_whatsapp_link", fake_send)

    sweep.sweep_pending_reminders(_FakeConn(), reminder_interval_seconds=7200, review_base_url="https://example.test")

    assert notified == ["Reminder: the review for https://example.test/review/xyz is still waiting."]
