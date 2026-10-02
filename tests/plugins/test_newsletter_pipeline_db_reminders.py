"""Newsletter reminder/lock defaults must match the event-post pipeline's (decided 2026-10-02):
1hr lock TTL, first reminder 12h after the "ready for review" message, then every 24h."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from plugins.platforms.event_post_pipeline import db as event_db  # noqa: E402
from plugins.platforms.newsletter_pipeline import db  # noqa: E402


class _Cursor:
    def __init__(self, log):
        self.log = log

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.log.append((sql, params))

    def fetchall(self):
        return [{"id": 1}]


class _Conn:
    def __init__(self):
        self.executed = []

    def cursor(self):
        return _Cursor(self.executed)


def test_defaults_match_event_post_pipeline():
    assert db.DEFAULT_LOCK_TTL_SECONDS == event_db.DEFAULT_LOCK_TTL_SECONDS == 3600
    assert db.DEFAULT_REMINDER_FIRST_DELAY_SECONDS == event_db.DEFAULT_REMINDER_FIRST_DELAY_SECONDS == 12 * 3600
    assert db.DEFAULT_REMINDER_INTERVAL_SECONDS == event_db.DEFAULT_REMINDER_INTERVAL_SECONDS == 24 * 3600


def test_find_due_reminders_query_shape():
    conn = _Conn()
    assert db.find_due_reminders(conn) == [{"id": 1}]
    sql, params = conn.executed[0]
    assert "'pending', 'refine_requested'" in sql  # completed (approved/rejected) never reminded
    assert "MIN(created_at) FROM newsletter_drafts" in sql
    assert "last_reminder_at" in sql
    assert params == ("43200 seconds", "86400 seconds")
