"""Tests for the event-post-pipeline plugin's Postgres client
(``plugins/platforms/event_post_pipeline/db.py``).

No real Postgres is spun up here — this repo's existing tests run the real
thing only for storage that's cheap and local (a throwaway SQLite Kanban
board in ``test_event_post_pipeline_pipeline.py``); a Postgres server is a
genuine external dependency this test suite has no existing convention or
fixture for. Instead these use a small hand-rolled fake connection/cursor
that records every executed query and replays canned ``fetchone``/
``fetchall`` results in call order — enough to exercise ``db.py``'s actual
Python-level logic (which query runs, with which params, how the result row
is turned into a dict, the CAS claim's two-branch fallback) without needing
a live database.
"""

from __future__ import annotations

from plugins.platforms.event_post_pipeline import db


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
    """``responses`` is consumed one entry per ``cursor().execute(...)`` call, in order."""

    def __init__(self, responses: list[dict]):
        self.responses = list(responses)
        self.executed: list[tuple] = []

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self)

    def close(self) -> None:
        pass


# --- resolve_database_url: pure function, no connection needed ---------------------------


def test_resolve_database_url_prefers_explicit_config(monkeypatch):
    monkeypatch.setenv("MY_SPP_DB_URL", "postgresql://from-env/db")
    assert db.resolve_database_url({"database_url": "${MY_SPP_DB_URL}"}) == "postgresql://from-env/db"


def test_resolve_database_url_falls_back_to_env_var(monkeypatch):
    monkeypatch.setenv("SPP_DATABASE_URL", "postgresql://direct-env/db")
    assert db.resolve_database_url({}) == "postgresql://direct-env/db"
    assert db.resolve_database_url(None) == "postgresql://direct-env/db"


def test_resolve_database_url_empty_when_unconfigured(monkeypatch):
    monkeypatch.delenv("SPP_DATABASE_URL", raising=False)
    assert db.resolve_database_url({}) == ""


# --- curator resolution --------------------------------------------------------------------


def test_get_curator_by_phone_found():
    conn = _FakeConnection([{"one": {"id": 1, "phone_number": "+15551234567", "name": "Amila"}}])
    curator = db.get_curator_by_phone(conn, "+15551234567")
    assert curator == {"id": 1, "phone_number": "+15551234567", "name": "Amila"}
    sql, params = conn.executed[0]
    assert "social_media_curators" in sql
    assert params == ("+15551234567",)


def test_get_curator_by_phone_missing_returns_none():
    conn = _FakeConnection([{"one": None}])
    assert db.get_curator_by_phone(conn, "+1000") is None


def test_upsert_curator_sends_role_flags_and_returns_row():
    conn = _FakeConnection([{"one": {"id": 2, "phone_number": "+1555", "name": "Nissanka", "is_publisher": True}}])
    curator = db.upsert_curator(conn, "+1555", "Nissanka", is_publisher=True)
    assert curator["is_publisher"] is True
    sql, params = conn.executed[0]
    assert "ON CONFLICT (phone_number)" in sql
    assert params == {"phone": "+1555", "name": "Nissanka", "is_publisher": True, "is_reviewer": None, "is_admin": None}


def test_upsert_curator_rejects_empty_phone():
    conn = _FakeConnection([])
    try:
        db.upsert_curator(conn, "", "Nobody")
        assert False, "expected DatabaseError"
    except db.DatabaseError:
        pass


def test_list_curators_by_role_ors_the_requested_flags():
    conn = _FakeConnection([{"all": [{"id": 1, "is_admin": True}, {"id": 2, "is_publisher": True}]}])
    curators = db.list_curators_by_role(conn, is_admin=True, is_publisher=True)
    assert len(curators) == 2
    sql, _ = conn.executed[0]
    assert "is_admin OR is_publisher" in sql or "is_publisher OR is_admin" in sql
    assert "active" in sql


def test_list_curators_by_role_no_flags_short_circuits_without_querying():
    conn = _FakeConnection([])
    assert db.list_curators_by_role(conn) == []
    assert conn.executed == []


# --- claim_submission: the ported Kanban CAS pattern ---------------------------------------


def test_claim_submission_success_returns_locked_row():
    conn = _FakeConnection([{"one": {"id": 5, "locked_by": 9, "slug": "abc"}}])
    result = db.claim_submission(conn, 5, 9)
    assert result.ok is True
    assert result.submission["locked_by"] == 9
    sql, params = conn.executed[0]
    assert "locked_by IS NULL" in sql
    assert params == (9, 5)


def test_claim_submission_contention_returns_current_holder_not_none():
    # First execute (the CAS UPDATE) returns no row (lost the race); the fallback then
    # looks up the submission and its current holder's curator row.
    conn = _FakeConnection([
        {"one": None},
        {"one": {"id": 5, "locked_by": 3, "slug": "abc"}},
        {"one": {"id": 3, "name": "Other Reviewer"}},
    ])
    result = db.claim_submission(conn, 5, 9)
    assert result.ok is False
    assert result.locked_by == {"id": 3, "name": "Other Reviewer"}
    assert result.submission["locked_by"] == 3


def test_claim_submission_contention_with_no_lock_holder_on_record():
    # Race lost, but by the time we look again the lock has already been released too.
    conn = _FakeConnection([{"one": None}, {"one": {"id": 5, "locked_by": None}}])
    result = db.claim_submission(conn, 5, 9)
    assert result.ok is False
    assert result.locked_by is None


def test_release_lock_clears_lock_fields():
    conn = _FakeConnection([{"one": None}])
    db.release_lock(conn, 5)
    sql, params = conn.executed[0]
    assert "locked_by = NULL" in sql
    assert params == (5,)


def test_release_expired_locks_returns_affected_rows():
    conn = _FakeConnection([{"all": [{"id": 1, "locked_by": 2}, {"id": 2, "locked_by": 3}]}])
    released = db.release_expired_locks(conn, ttl_seconds=3600)
    assert len(released) == 2
    sql, params = conn.executed[0]
    assert "locked_at < now() - %s::interval" in sql
    assert params == ("3600 seconds",)


def test_find_due_reminders_query_shape():
    conn = _FakeConnection([{"all": [{"id": 1, "status": "pending"}]}])
    due = db.find_due_reminders(conn, interval_seconds=7200)
    assert len(due) == 1
    sql, params = conn.executed[0]
    assert "status = 'pending'" in sql
    assert "last_reminder_at" in sql
    assert params == ("7200 seconds",)


def test_stamp_reminder_sent_updates_timestamp():
    conn = _FakeConnection([{"one": None}])
    db.stamp_reminder_sent(conn, 42)
    sql, params = conn.executed[0]
    assert "last_reminder_at = now()" in sql
    assert params == (42,)


def test_list_drafts_orders_by_platform_and_round():
    conn = _FakeConnection([{"all": [{"platform": "fb", "round": 1}]}])
    drafts = db.list_drafts(conn, 7)
    assert drafts == [{"platform": "fb", "round": 1}]
    sql, params = conn.executed[0]
    assert "post_platform_drafts" in sql
    assert params == (7,)


# --- pipeline_runs / pipeline_events: visualizer tracking -----------------------------------


def test_upsert_pipeline_run_sends_expected_upsert_shape():
    conn = _FakeConnection([{"one": None}])
    db.upsert_pipeline_run(conn, "task-1", title="A short title", state="running", current_step="intake_received")
    sql, params = conn.executed[0]
    assert "INSERT INTO pipeline_runs" in sql
    assert "ON CONFLICT (task_id) DO UPDATE SET" in sql
    assert "title = EXCLUDED.title" in sql
    assert "state = EXCLUDED.state" in sql
    assert "current_step = EXCLUDED.current_step" in sql
    assert "updated_at = now()" in sql
    assert params == {
        "task_id": "task-1", "title": "A short title", "state": "running", "current_step": "intake_received",
    }


def test_record_pipeline_event_inserts_with_optional_fields():
    conn = _FakeConnection([{"one": None}])
    db.record_pipeline_event(conn, "task-1", "stylus_done", "ok", detail="https://example/review", actor="stylus")
    sql, params = conn.executed[0]
    assert "INSERT INTO pipeline_events" in sql
    assert params == ("task-1", "stylus_done", "ok", "https://example/review", "stylus")


def test_record_pipeline_event_defaults_detail_and_actor_to_none():
    conn = _FakeConnection([{"one": None}])
    db.record_pipeline_event(conn, "task-1", "intake_received", "ok")
    _sql, params = conn.executed[0]
    assert params == ("task-1", "intake_received", "ok", None, None)


def test_get_submission_id_by_slug_found():
    conn = _FakeConnection([{"one": {"id": 42}}])
    assert db.get_submission_id_by_slug(conn, "my-slug") == 42
    sql, params = conn.executed[0]
    assert "post_submissions" in sql
    assert params == ("my-slug",)


def test_get_submission_id_by_slug_missing_returns_none():
    conn = _FakeConnection([{"one": None}])
    assert db.get_submission_id_by_slug(conn, "no-such-slug") is None


def test_link_submission_id_updates_pipeline_runs():
    conn = _FakeConnection([{"one": None}])
    db.link_submission_id(conn, "task-1", 42)
    sql, params = conn.executed[0]
    assert "UPDATE pipeline_runs" in sql
    assert "submission_id" in sql
    assert params == (42, "task-1")
