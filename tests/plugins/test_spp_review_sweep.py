"""Tests for the SPP v2 review-sweep cron script
(``scripts/spp_review_sweep.py``): the two independent, unrelated checks it
runs every tick (1hr lock-timeout release, 2hr pending-review reminder).

Mocks ``db.*`` and ``whatsapp_notify.notify_curators`` rather than a real
Postgres or a real WhatsApp send — same test-double approach as
``tests/plugins/test_event_post_pipeline_db.py`` for the same reason (no
existing fixture for a live Postgres server in this repo), plus it keeps
these tests focused on the script's own branching logic (what gets queried,
who gets notified, when the reminder timestamp gets stamped) rather than
re-testing ``db.py``'s SQL, which already has its own test file.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from plugins.platforms.event_post_pipeline import db  # noqa: E402
from scripts import spp_review_sweep as sweep  # noqa: E402


class _FakeConn:
    pass


def test_sweep_lock_timeouts_notifies_publisher_and_admin(monkeypatch):
    released_rows = [{"id": 1, "slug": "abc", "submitted_by": 7}]
    admin_and_publisher_rows = [{"id": 3, "phone_number": "+1admin", "is_admin": True}]
    monkeypatch.setattr(db, "release_expired_locks", lambda conn, ttl_seconds: released_rows)
    monkeypatch.setattr(db, "list_curators_by_role", lambda conn, **kw: admin_and_publisher_rows)
    monkeypatch.setattr(db, "get_curator", lambda conn, cid: {"id": 7, "phone_number": "+1pub"})
    notified = []

    async def fake_notify(curators, message):
        notified.append((list(curators), message))

    monkeypatch.setattr(sweep.whatsapp_notify, "notify_curators", fake_notify)

    released = sweep.sweep_lock_timeouts(_FakeConn(), lock_ttl_seconds=3600)

    assert released == released_rows
    assert len(notified) == 1
    curators, message = notified[0]
    # The publisher (submitted_by=7) isn't already in the admin/publisher-role query result
    # in this fixture, so it must be added explicitly.
    assert {"id": 3, "phone_number": "+1admin", "is_admin": True} in curators
    assert {"id": 7, "phone_number": "+1pub"} in curators
    assert "abc" in message


def test_sweep_lock_timeouts_no_expired_locks_sends_nothing(monkeypatch):
    monkeypatch.setattr(db, "release_expired_locks", lambda conn, ttl_seconds: [])
    notified = []

    async def fake_notify(curators, message):
        notified.append(1)

    monkeypatch.setattr(sweep.whatsapp_notify, "notify_curators", fake_notify)

    released = sweep.sweep_lock_timeouts(_FakeConn(), lock_ttl_seconds=3600)

    assert released == []
    assert notified == []


def test_sweep_lock_timeouts_publisher_already_in_role_pool_not_duplicated(monkeypatch):
    released_rows = [{"id": 1, "slug": "abc", "submitted_by": 7}]
    pool = [{"id": 7, "phone_number": "+1pub", "is_publisher": True}]
    monkeypatch.setattr(db, "release_expired_locks", lambda conn, ttl_seconds: released_rows)
    monkeypatch.setattr(db, "list_curators_by_role", lambda conn, **kw: pool)
    get_curator_calls = []
    monkeypatch.setattr(db, "get_curator", lambda conn, cid: get_curator_calls.append(cid))
    notified = []

    async def fake_notify(curators, message):
        notified.append(list(curators))

    monkeypatch.setattr(sweep.whatsapp_notify, "notify_curators", fake_notify)

    sweep.sweep_lock_timeouts(_FakeConn(), lock_ttl_seconds=3600)

    assert get_curator_calls == []  # publisher already present in the role-flag query result
    assert notified == [pool]


def test_sweep_pending_reminders_notifies_reviewer_pool_and_stamps(monkeypatch):
    due_rows = [{"id": 10, "slug": "xyz"}, {"id": 11, "slug": "qrs"}]
    reviewer_pool = [{"id": 4, "phone_number": "+1rev"}]
    monkeypatch.setattr(db, "find_due_reminders", lambda conn, interval_seconds: due_rows)
    monkeypatch.setattr(db, "list_curators_by_role", lambda conn, **kw: reviewer_pool)
    stamped = []
    monkeypatch.setattr(db, "stamp_reminder_sent", lambda conn, sid: stamped.append(sid))
    notified = []

    async def fake_notify(curators, message):
        notified.append(message)

    monkeypatch.setattr(sweep.whatsapp_notify, "notify_curators", fake_notify)

    due = sweep.sweep_pending_reminders(_FakeConn(), reminder_interval_seconds=7200)

    assert due == due_rows
    assert stamped == [10, 11]
    assert len(notified) == 2
    assert "xyz" in notified[0] and "qrs" in notified[1]


def test_sweep_pending_reminders_none_due_stamps_nothing(monkeypatch):
    monkeypatch.setattr(db, "find_due_reminders", lambda conn, interval_seconds: [])
    monkeypatch.setattr(db, "list_curators_by_role", lambda conn, **kw: [])
    stamped = []
    monkeypatch.setattr(db, "stamp_reminder_sent", lambda conn, sid: stamped.append(sid))

    due = sweep.sweep_pending_reminders(_FakeConn(), reminder_interval_seconds=7200)

    assert due == []
    assert stamped == []


def test_run_sweep_summarizes_both_independent_checks(monkeypatch):
    monkeypatch.setattr(sweep, "sweep_lock_timeouts", lambda conn, lock_ttl_seconds: [{"id": 1}, {"id": 2}])
    monkeypatch.setattr(sweep, "sweep_pending_reminders", lambda conn, reminder_interval_seconds: [{"id": 3}])

    summary = sweep.run_sweep(_FakeConn(), lock_ttl_seconds=3600, reminder_interval_seconds=7200)

    assert summary == {"locks_released": 2, "reminders_sent": 1}
