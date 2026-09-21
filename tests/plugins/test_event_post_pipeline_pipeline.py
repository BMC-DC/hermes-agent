"""End-to-end tests for the event-post-pipeline plugin's deterministic core
(``plugins/platforms/event_post_pipeline/pipeline.py``), run against a real, throwaway
Kanban SQLite board — no mocking of ``hermes_cli.kanban_db`` itself, only
the review-page HTTP client (``review_client``), which is the one real
network boundary.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from plugins.platforms.event_post_pipeline import pipeline
from plugins.platforms.event_post_pipeline.review_client import ReviewClientConfig
from plugins.platforms.event_post_pipeline.store import EventPostPipelineStore


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def conn(kanban_home):
    connection = kbc.connect()
    yield connection
    connection.close()


@pytest.fixture
def ops():
    return pipeline.KanbanOps(kb)


@pytest.fixture
def store(tmp_path):
    return EventPostPipelineStore(tmp_path / "store.json")


@pytest.fixture
def review_config():
    return ReviewClientConfig(base_url="https://bmcposts.example", create_secret="test-secret")


EVENT_PACK = {
    "submissionId": "sub-2026-09-12-abc123",
    "date": "2026-09-20",
    "description": "A day-long meditation retreat with a visiting monk.",
    "quote": "Peace comes from within.",
    "first": {"finalUrl": "https://res.cloudinary.com/demo/first.jpg"},
    "last": {"finalUrl": "https://res.cloudinary.com/demo/last.jpg"},
    "rest": [{"finalUrl": "https://res.cloudinary.com/demo/rest1.jpg"}],
}

STYLUS_METADATA = {
    "event_title": "Day-Long Meditation Retreat",
    "fb": "Join us for a day of stillness...",
    "ig": "A day of stillness awaits.",
    "blog": {
        "body": "On the appointed day, practitioners gathered...",
        "focus_keyword": "meditation retreat",
        "supporting_keywords": ["mindfulness", "Buddhist monk"],
        "seo_title": "Day-Long Meditation Retreat | BMC DC",
        "meta_description": "A reflection on our day-long retreat.",
        "url_slug": "day-long-meditation-retreat",
    },
}


def _complete_as_stylus(conn, task_id: str, metadata: dict) -> None:
    if kb.get_task(conn, task_id).status == "ready":
        kb.claim_task(conn, task_id, claimer="stylus")
    assert kb.complete_task(conn, task_id, summary="drafted copy", metadata=metadata)


def test_handle_intake_creates_tagged_task(conn, ops, store):
    task_id = pipeline.handle_intake(conn, ops, store, submission_id=EVENT_PACK["submissionId"], event_pack=EVENT_PACK)
    task = kb.get_task(conn, task_id)
    assert task.assignee == "stylus"
    assert task.tenant == pipeline.TENANT
    assert task.idempotency_key == EVENT_PACK["submissionId"]
    assert "meditation retreat" in task.title.lower()
    record = store.get_submission(EVENT_PACK["submissionId"])
    assert record["root_task_id"] == task_id
    assert record["images"] == [
        "https://res.cloudinary.com/demo/first.jpg",
        "https://res.cloudinary.com/demo/rest1.jpg",
        "https://res.cloudinary.com/demo/last.jpg",
    ]


def test_intake_is_idempotent(conn, ops, store):
    first = pipeline.handle_intake(conn, ops, store, submission_id="dup-1", event_pack={**EVENT_PACK, "submissionId": "dup-1"})
    second = pipeline.handle_intake(conn, ops, store, submission_id="dup-1", event_pack={**EVENT_PACK, "submissionId": "dup-1"})
    assert first == second


def test_task_completed_ignores_unrelated_tasks(conn, ops, store, review_config):
    other_task = kb.create_task(conn, title="unrelated", body="", assignee="stylus")
    _complete_as_stylus(conn, other_task, {"fb": "x", "ig": "y", "blog": {"body": "z"}})
    assert pipeline.handle_stylus_completion(conn, ops, store, review_config, other_task) is None


def test_first_round_completion_creates_review_page(conn, ops, store, review_config, monkeypatch):
    task_id = pipeline.handle_intake(conn, ops, store, submission_id=EVENT_PACK["submissionId"], event_pack=EVENT_PACK)
    _complete_as_stylus(conn, task_id, STYLUS_METADATA)

    captured = {}

    def fake_create_review(config, **kwargs):
        captured.update(kwargs)
        return "https://bmcposts.example/review/abc123"

    monkeypatch.setattr(pipeline, "create_review", fake_create_review)

    result = pipeline.handle_stylus_completion(conn, ops, store, review_config, task_id)

    assert result == {
        "action": "created_review_page", "task_id": task_id, "root_task_id": task_id,
        "review_url": "https://bmcposts.example/review/abc123",
    }
    assert captured["event_title"] == "Day-Long Meditation Retreat"
    assert captured["fb"] == STYLUS_METADATA["fb"]
    assert captured["blog_seo"]["urlSlug"] == "day-long-meditation-retreat"
    comments = kb.list_comments(conn, task_id)
    assert any("Review page: https://bmcposts.example/review/abc123" in c.body for c in comments)
    assert store.get_submission(EVENT_PACK["submissionId"])["review_url"] == "https://bmcposts.example/review/abc123"


def test_completion_with_malformed_metadata_raises(conn, ops, store, review_config):
    task_id = pipeline.handle_intake(conn, ops, store, submission_id="broken-1", event_pack={**EVENT_PACK, "submissionId": "broken-1"})
    _complete_as_stylus(conn, task_id, {"fb": "only fb, missing ig and blog"})
    with pytest.raises(pipeline.PipelineError):
        pipeline.handle_stylus_completion(conn, ops, store, review_config, task_id)


def test_approve_comments_outcome_without_creating_tasks(conn, ops, store, review_config):
    task_id = pipeline.handle_intake(conn, ops, store, submission_id="appr-1", event_pack={**EVENT_PACK, "submissionId": "appr-1"})
    _complete_as_stylus(conn, task_id, STYLUS_METADATA)

    result = pipeline.handle_review_action(conn, ops, store, task_id=task_id, platform="fb", action="approved", comment="")

    assert result == {"action": "approved", "task_id": task_id, "platform": "fb"}
    comments = kb.list_comments(conn, task_id)
    assert any("FB APPROVED via review page" in c.body for c in comments)


def test_refine_dispatches_new_scoped_task_and_completion_updates_page(conn, ops, store, review_config, monkeypatch):
    root_id = pipeline.handle_intake(conn, ops, store, submission_id="ref-1", event_pack={**EVENT_PACK, "submissionId": "ref-1"})
    _complete_as_stylus(conn, root_id, STYLUS_METADATA)
    monkeypatch.setattr(pipeline, "create_review", lambda config, **kw: "https://bmcposts.example/review/xyz")
    pipeline.handle_stylus_completion(conn, ops, store, review_config, root_id)

    refine_result = pipeline.handle_review_action(
        conn, ops, store, task_id=root_id, platform="fb", action="refine", comment="Make it warmer.",
    )
    assert refine_result["action"] == "refine_dispatched"
    assert refine_result["round"] == 1
    new_task_id = refine_result["task_id"]
    new_task = kb.get_task(conn, new_task_id)
    assert new_task.idempotency_key == f"{root_id}-fb-r1"
    assert new_task.tenant == pipeline.TENANT
    assert "Make it warmer." in new_task.body

    refined_metadata = {**STYLUS_METADATA, "fb": "A warmer, gentler invitation..."}
    _complete_as_stylus(conn, new_task_id, refined_metadata)

    updates = []
    monkeypatch.setattr(pipeline, "update_review", lambda config, **kw: updates.append(kw))
    result = pipeline.handle_stylus_completion(conn, ops, store, review_config, new_task_id)

    assert result["action"] == "updated_review_page"
    assert result["platform"] == "fb"
    assert updates[0]["text"] == "A warmer, gentler invitation..."
    assert updates[0]["slug"] == "xyz"


class _FakeVisualizerCursor:
    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def execute(self, sql, params=None):
        self._conn.executed.append((sql, params))


class _FakeVisualizerConnection:
    """Records every ``upsert_pipeline_run``/``record_pipeline_event``/``link_submission_id``
    call as ``(sql, params)`` — good enough to assert call-site wiring without a live
    Postgres (the ``db.py``-level SQL shape is already covered by
    ``test_event_post_pipeline_db.py``)."""

    def __init__(self):
        self.executed: list[tuple] = []

    def cursor(self):
        return _FakeVisualizerCursor(self)

    def close(self):
        pass


@pytest.fixture
def fake_visualizer_conn(monkeypatch):
    fake_conn = _FakeVisualizerConnection()
    monkeypatch.setattr(pipeline.db, "get_connection", lambda url=None: fake_conn)
    return fake_conn


def _steps(fake_conn) -> list[str]:
    """The ``step``/table-touched sequence recorded on the fake connection, in call order —
    inspects each statement's SQL/params rather than requiring exact string matches."""
    out = []
    for sql, params in fake_conn.executed:
        if "INSERT INTO pipeline_runs" in sql:
            out.append(f"run:{params['current_step']}")
        elif "INSERT INTO pipeline_events" in sql:
            out.append(f"event:{params[1]}:{params[2]}")
        elif "UPDATE pipeline_runs" in sql and "submission_id" in sql:
            out.append("link_submission_id")
    return out


def test_handle_intake_tracks_visualizer_run_and_events(conn, ops, store, fake_visualizer_conn, monkeypatch):
    monkeypatch.setenv("SPP_DATABASE_URL", "postgresql://irrelevant/test")
    pipeline.handle_intake(conn, ops, store, submission_id=EVENT_PACK["submissionId"], event_pack=EVENT_PACK)
    assert _steps(fake_visualizer_conn) == [
        "run:intake_received", "event:intake_received:ok",
        "run:stylus_drafting", "event:stylus_drafting:ok",
    ]


def test_first_round_completion_tracks_awaiting_review_and_links_submission(
    conn, ops, store, review_config, fake_visualizer_conn, monkeypatch,
):
    monkeypatch.setenv("SPP_DATABASE_URL", "postgresql://irrelevant/test")
    task_id = pipeline.handle_intake(conn, ops, store, submission_id=EVENT_PACK["submissionId"], event_pack=EVENT_PACK)
    fake_visualizer_conn.executed.clear()  # only care about this step's own tracking calls
    _complete_as_stylus(conn, task_id, STYLUS_METADATA)
    monkeypatch.setattr(pipeline, "create_review", lambda config, **kw: "https://bmcposts.example/review/abc123")
    monkeypatch.setattr(pipeline.db, "get_submission_id_by_slug", lambda conn, slug: 7)

    pipeline.handle_stylus_completion(conn, ops, store, review_config, task_id)

    assert _steps(fake_visualizer_conn) == [
        "run:awaiting_review", "event:stylus_done:ok", "link_submission_id",
    ]


def test_refine_dispatched_tracks_visualizer_run(conn, ops, store, review_config, fake_visualizer_conn, monkeypatch):
    monkeypatch.setenv("SPP_DATABASE_URL", "postgresql://irrelevant/test")
    root_id = pipeline.handle_intake(conn, ops, store, submission_id="ref-viz-1", event_pack={**EVENT_PACK, "submissionId": "ref-viz-1"})
    _complete_as_stylus(conn, root_id, STYLUS_METADATA)
    monkeypatch.setattr(pipeline, "create_review", lambda config, **kw: "https://bmcposts.example/review/xyz")
    pipeline.handle_stylus_completion(conn, ops, store, review_config, root_id)
    fake_visualizer_conn.executed.clear()

    pipeline.handle_review_action(conn, ops, store, task_id=root_id, platform="fb", action="refine", comment="warmer")

    assert _steps(fake_visualizer_conn) == ["run:refine_drafting", "event:refine_dispatched:ok"]


def test_approve_action_tracks_event_only_no_run_upsert(conn, ops, store, review_config, fake_visualizer_conn, monkeypatch):
    monkeypatch.setenv("SPP_DATABASE_URL", "postgresql://irrelevant/test")
    task_id = pipeline.handle_intake(conn, ops, store, submission_id="appr-viz-1", event_pack={**EVENT_PACK, "submissionId": "appr-viz-1"})
    _complete_as_stylus(conn, task_id, STYLUS_METADATA)
    fake_visualizer_conn.executed.clear()

    pipeline.handle_review_action(conn, ops, store, task_id=task_id, platform="fb", action="approved", comment="")

    # Only a pipeline_events row — approve/reject deliberately never touches
    # pipeline_runs' state/current_step (see db.py's ownership-boundary docstring).
    assert _steps(fake_visualizer_conn) == ["event:action_taken:ok"]


def test_visualizer_tracking_is_a_no_op_without_database_url(conn, ops, store, monkeypatch):
    monkeypatch.delenv("SPP_DATABASE_URL", raising=False)
    calls = []
    monkeypatch.setattr(pipeline.db, "get_connection", lambda url=None: calls.append(url) or (_ for _ in ()).throw(AssertionError("should not connect")))
    pipeline.handle_intake(conn, ops, store, submission_id=EVENT_PACK["submissionId"], event_pack=EVENT_PACK)
    assert calls == []


def test_visualizer_tracking_failure_never_raises(conn, ops, store, monkeypatch):
    """Per the plugin's own stated rule (mirroring whatsapp_notify.py): a visualizer-
    tracking failure must never take down the deterministic pipeline logic around it."""
    monkeypatch.setenv("SPP_DATABASE_URL", "postgresql://irrelevant/test")

    class _ExplodingConnection:
        def cursor(self):
            raise RuntimeError("boom")

        def close(self):
            pass

    monkeypatch.setattr(pipeline.db, "get_connection", lambda url=None: _ExplodingConnection())
    task_id = pipeline.handle_intake(conn, ops, store, submission_id=EVENT_PACK["submissionId"], event_pack=EVENT_PACK)
    assert task_id  # the actual pipeline logic completed despite the tracking failure


def test_second_refine_round_increments_and_closing_comment_targets_latest_round(conn, ops, store, review_config, monkeypatch):
    root_id = pipeline.handle_intake(conn, ops, store, submission_id="ref-2", event_pack={**EVENT_PACK, "submissionId": "ref-2"})
    _complete_as_stylus(conn, root_id, STYLUS_METADATA)
    monkeypatch.setattr(pipeline, "create_review", lambda config, **kw: "https://bmcposts.example/review/roundtwo")
    pipeline.handle_stylus_completion(conn, ops, store, review_config, root_id)

    r1 = pipeline.handle_review_action(conn, ops, store, task_id=root_id, platform="ig", action="refine", comment="shorter")
    _complete_as_stylus(conn, r1["task_id"], {**STYLUS_METADATA, "ig": "short version"})
    monkeypatch.setattr(pipeline, "update_review", lambda config, **kw: None)
    pipeline.handle_stylus_completion(conn, ops, store, review_config, r1["task_id"])

    r2 = pipeline.handle_review_action(conn, ops, store, task_id=root_id, platform="ig", action="refine", comment="even shorter")
    assert r2["round"] == 2
    assert kb.get_task(conn, r2["task_id"]).idempotency_key == f"{root_id}-ig-r2"
    _complete_as_stylus(conn, r2["task_id"], {**STYLUS_METADATA, "ig": "even shorter"})
    pipeline.handle_stylus_completion(conn, ops, store, review_config, r2["task_id"])

    # Approving IG now must comment onto round 2's task (the latest, completed IG draft) so
    # Stylus's lessons skill sees the outcome on the run it actually produced.
    approve = pipeline.handle_review_action(conn, ops, store, task_id=root_id, platform="ig", action="approved", comment="")
    assert approve["task_id"] == r2["task_id"]


def test_track_stylus_retry_requested_records_ok_event(fake_visualizer_conn, monkeypatch):
    monkeypatch.setenv("SPP_DATABASE_URL", "postgresql://irrelevant/test")
    pipeline.track_stylus_retry_requested("postgresql://irrelevant/test", "task-1", ok=True)
    assert _steps(fake_visualizer_conn) == ["event:stylus_retry_requested:ok"]


def test_track_stylus_retry_requested_records_error_event_with_detail(fake_visualizer_conn):
    pipeline.track_stylus_retry_requested(
        "postgresql://irrelevant/test", "task-1", ok=False, detail="task was not in a blocked/scheduled state",
    )
    assert _steps(fake_visualizer_conn) == ["event:stylus_retry_requested:error"]
    # detail is the 4th bound param on the INSERT INTO pipeline_events statement
    sql, params = fake_visualizer_conn.executed[0]
    assert "INSERT INTO pipeline_events" in sql
    assert params[3] == "task was not in a blocked/scheduled state"


def test_track_stylus_retry_requested_is_a_no_op_without_database_url(monkeypatch):
    monkeypatch.delenv("SPP_DATABASE_URL", raising=False)
    calls = []
    monkeypatch.setattr(
        pipeline.db, "get_connection",
        lambda url=None: calls.append(url) or (_ for _ in ()).throw(AssertionError("should not connect")),
    )
    pipeline.track_stylus_retry_requested("", "task-1", ok=True)
    assert calls == []


def test_track_stylus_retry_requested_failure_never_raises(monkeypatch):
    class _ExplodingConnection:
        def cursor(self):
            raise RuntimeError("boom")

        def close(self):
            pass

    monkeypatch.setattr(pipeline.db, "get_connection", lambda url=None: _ExplodingConnection())
    pipeline.track_stylus_retry_requested("postgresql://irrelevant/test", "task-1", ok=True)  # must not raise
