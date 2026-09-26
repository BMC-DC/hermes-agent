"""Regression tests for newsletter_pipeline's store-identity bugs (found
2026-09-26, the same day and same bug class as event_post_pipeline's — both
plugins share the same design, and newsletter_pipeline copy-pasted the buggy
version of these code paths). Run against a real, throwaway Kanban SQLite
board, mirroring test_event_post_pipeline_pipeline.py's own fixtures.

No test file existed for this plugin before this incident — these bugs
shipped untested.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from plugins.platforms.newsletter_pipeline import pipeline
from plugins.platforms.newsletter_pipeline.review_client import ReviewClientConfig
from plugins.platforms.newsletter_pipeline.store import NewsletterPipelineStore


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
    return NewsletterPipelineStore(tmp_path / "store.json")


@pytest.fixture
def review_config():
    return ReviewClientConfig(base_url="https://bmcposts.example", create_secret="test-secret")


INTAKE = {
    "issueMonth": "2026-09-01",
    "bhanteAdviceText": "Be kind.",
    "bhanteAdviceQuote": "May all beings be happy.",
    "recapSummary": "We gathered for a wonderful evening.",
    "recapImage": {"url": "https://res.cloudinary.com/demo/recap.jpg"},
    "featuredAnnouncementText": "Join our next retreat.",
    "featuredCtaLabel": "Learn more",
    "featuredCtaUrl": "https://example.org/retreat",
}

STYLUS_METADATA = {
    "subject_line": "A Mindful September",
    "preview_text": "Reflections from this month.",
    "eyebrow_label": "September Newsletter",
    "hero_headline": "A Mindful September",
    "bhante_advice_paragraph": "Be kind to yourself and others.",
    "recap_paragraph": "We gathered for a wonderful evening of practice.",
    "featured_announcement_paragraph": "Join us for our next retreat.",
}


def _complete_as_stylus(conn, task_id: str, metadata: dict) -> None:
    if kb.get_task(conn, task_id).status == "ready":
        kb.claim_task(conn, task_id, claimer="stylus")
    assert kb.complete_task(conn, task_id, summary="drafted newsletter", metadata=metadata)


def test_handle_intake_creates_tagged_task(conn, ops, store):
    task_id = pipeline.handle_intake(conn, ops, store, submission_id="issue-1", intake=INTAKE)
    task = kb.get_task(conn, task_id)
    assert task.assignee == "stylus"
    assert task.tenant == pipeline.TENANT
    assert task.idempotency_key == "issue-1"
    record = store.get_issue("issue-1")
    assert record["root_task_id"] == task_id


def test_refine_dispatch_and_completion_update_the_same_store_record(conn, ops, store, review_config, monkeypatch):
    """Regression test for a real production incident (2026-09-26, newsletter issue
    #6 / task t_7630d30a): both handle_review_action's refine dispatch and
    _handle_refine_completion upserted round-tracking data using the Kanban root
    task id as the store's dict key, when the store is actually keyed by
    submission_id (an intake-time slug) — a different identity, per
    handle_intake()'s own upsert. Every refine silently created a second, orphaned
    store record instead of updating the real issue, live in production for both
    round 1 (2026-09-24) and round 2 (2026-09-26) of this exact issue."""
    root_id = pipeline.handle_intake(conn, ops, store, submission_id="issue-2", intake=INTAKE)
    _complete_as_stylus(conn, root_id, STYLUS_METADATA)
    monkeypatch.setattr(pipeline, "create_review", lambda config, **kw: "https://bmcposts.example/review/newsletter/xyz")
    pipeline.handle_stylus_completion(conn, ops, store, review_config, root_id)
    assert store.get_issue("issue-2")["review_url"] == "https://bmcposts.example/review/newsletter/xyz"

    refine_result = pipeline.handle_review_action(
        conn, ops, store, review_config, task_id=root_id, action="refine", comment="Make it warmer.",
    )
    new_task_id = refine_result["task_id"]

    # The dispatch's own round_task_id write must land on the original key.
    assert store.get_issue("issue-2")["round_task_id"] == new_task_id
    assert store.get_issue(root_id) is None  # never a shadow record keyed by task id

    refined_metadata = {**STYLUS_METADATA, "recap_paragraph": "A warmer, gentler recap."}
    _complete_as_stylus(conn, new_task_id, refined_metadata)

    updates = []
    monkeypatch.setattr(pipeline, "update_review", lambda config, **kw: updates.append(kw))
    result = pipeline.handle_stylus_completion(conn, ops, store, review_config, new_task_id)

    assert result["action"] == "updated_review_page"
    assert result["review_url"] == "https://bmcposts.example/review/newsletter/xyz"
    assert store.get_issue("issue-2")["round_task_id"] == new_task_id
    assert store.get_issue(root_id) is None


def test_refine_recovers_review_url_from_kanban_comment_when_store_record_lacks_it(conn, ops, store, review_config, monkeypatch):
    """Mirrors event_post_pipeline's same regression test: a store record can exist
    (from handle_intake) but be missing review_url if the first-round completion
    failed before ever persisting it. A human's manual "Review page: <url>"
    remediation comment must still be recoverable for the next refine round."""
    root_id = pipeline.handle_intake(conn, ops, store, submission_id="issue-3", intake=INTAKE)
    _complete_as_stylus(conn, root_id, STYLUS_METADATA)
    assert store.get_issue("issue-3").get("review_url") is None

    ops.add_comment(conn, root_id, pipeline.CREATED_BY, "Review page: https://bmcposts.example/review/newsletter/recovered")

    refine_result = pipeline.handle_review_action(
        conn, ops, store, review_config, task_id=root_id, action="refine", comment="warmer",
    )
    new_task_id = refine_result["task_id"]
    _complete_as_stylus(conn, new_task_id, STYLUS_METADATA)

    updates = []
    monkeypatch.setattr(pipeline, "update_review", lambda config, **kw: updates.append(kw))
    result = pipeline.handle_stylus_completion(conn, ops, store, review_config, new_task_id)

    assert result["action"] == "updated_review_page"
    assert result["review_url"] == "https://bmcposts.example/review/newsletter/recovered"


def test_maybe_send_via_brevo_finds_the_real_record_by_task_id(store):
    """Regression test for the same identity mismatch in the Brevo-send path
    (currently untriggered in production since Brevo is feature-flagged off —
    see _maybe_send_via_brevo's own docstring — but would have failed every
    single time with "no stored draft/review_url found" the moment it was
    turned on, since it looked up the store by the wrong key)."""
    store.upsert_issue("issue-4", {
        "root_task_id": "t_abc123", "review_url": "https://bmcposts.example/review/newsletter/xyz",
        "latest_draft": {"subjectLine": "Hi", "assembledHtml": "<html></html>"},
    })
    record = store.find_by_task_id("t_abc123") or store.get_issue("t_abc123")
    assert record is not None
    assert record["review_url"] == "https://bmcposts.example/review/newsletter/xyz"
