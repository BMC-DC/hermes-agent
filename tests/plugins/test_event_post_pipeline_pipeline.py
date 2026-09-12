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

    assert result == {"action": "created_review_page", "task_id": task_id, "review_url": "https://bmcposts.example/review/abc123"}
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
