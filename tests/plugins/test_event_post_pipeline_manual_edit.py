"""Manual reviewer edits (portal "Edit" -> Hermes ``edited`` action) for the event-post
pipeline: the Stylus acknowledgement (Kanban comment + store record) and the guarantee
that a later redraft starts from the hand-edited text — but only while that edit is
still the latest draft. Real throwaway Kanban board, same fixtures as
test_event_post_pipeline_pipeline.py."""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from plugins.platforms.event_post_pipeline import models, pipeline
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
    "date": "2026-09-20",
    "description": "A day-long meditation retreat with a visiting monk.",
    "quote": "Peace comes from within.",
    "first": {"finalUrl": "https://res.cloudinary.com/demo/first.jpg"},
    "last": {"finalUrl": "https://res.cloudinary.com/demo/last.jpg"},
    "rest": [],
}

STYLUS_METADATA = {
    "event_title": "Day-Long Meditation Retreat",
    "fb": "Stylus FB original",
    "ig": "Stylus IG original",
    "blog": {
        "body": "Stylus blog original", "focus_keyword": "retreat", "supporting_keywords": ["a"],
        "seo_title": "T", "meta_description": "M", "url_slug": "s",
    },
}

SEO = {"seoTitle": "My title", "urlSlug": "my-slug", "focusKeyword": "kw", "supportingKeywords": ["x", "y"], "metaDescription": "md"}


def _complete_as_stylus(conn, task_id: str, metadata: dict) -> None:
    if kb.get_task(conn, task_id).status == "ready":
        kb.claim_task(conn, task_id, claimer="stylus")
    assert kb.complete_task(conn, task_id, summary="drafted copy", metadata=metadata)


def _first_round(conn, ops, store, review_config, monkeypatch, sid):
    root_id = pipeline.handle_intake(conn, ops, store, submission_id=sid, event_pack={**EVENT_PACK, "submissionId": sid})
    _complete_as_stylus(conn, root_id, STYLUS_METADATA)
    monkeypatch.setattr(pipeline, "create_review", lambda config, **kw: "https://bmcposts.example/review/xyz")
    pipeline.handle_stylus_completion(conn, ops, store, review_config, root_id)
    return root_id


# --- models.parse_review_action -------------------------------------------------------


def test_parse_edited_action_accepts_a_valid_fb_edit():
    parsed = models.parse_review_action({"taskId": "t_1", "platform": "fb", "action": "edited", "comment": "", "editedText": "Hello"})
    assert parsed is not None
    assert (parsed.action, parsed.edited_text, parsed.edited_seo) == ("edited", "Hello", None)


def test_parse_edited_action_requires_text_and_blog_seo():
    base = {"taskId": "t_1", "platform": "fb", "action": "edited"}
    assert models.parse_review_action(base) is None  # no text
    assert models.parse_review_action({**base, "editedText": "   "}) is None
    assert models.parse_review_action({**base, "editedText": "x" * (models.EDITED_TEXT_MAX_CHARS + 1)}) is None
    assert models.parse_review_action({**base, "editedText": 5}) is None
    blog = {"taskId": "t_1", "platform": "blog", "action": "edited", "editedText": "body"}
    assert models.parse_review_action(blog) is None  # blog needs SEO
    assert models.parse_review_action({**blog, "editedSeo": {**SEO, "supportingKeywords": "nope"}}) is None
    assert models.parse_review_action({**blog, "editedSeo": {**SEO, "seoTitle": 3}}) is None
    ok = models.parse_review_action({**blog, "editedSeo": SEO})
    assert ok is not None and ok.edited_seo == SEO


def test_existing_actions_still_parse_unchanged():
    for action in ("approved", "rejected", "refine"):
        parsed = models.parse_review_action({"taskId": "t_1", "platform": "ig", "action": action, "comment": "c"})
        assert parsed is not None and parsed.edited_text is None and parsed.edited_seo is None


# --- pipeline: edited ack -------------------------------------------------------------


def test_edit_comments_final_text_and_records_it_without_approving(conn, ops, store, review_config, monkeypatch):
    root_id = _first_round(conn, ops, store, review_config, monkeypatch, "e-1")
    result = pipeline.handle_review_action(
        conn, ops, store, task_id=root_id, platform="fb", action="edited", comment="",
        edited_text="My hand-written FB post",
    )
    assert result == {"action": "edited", "task_id": root_id, "platform": "fb"}
    comments = [c.body for c in kb.list_comments(conn, root_id)]
    assert any("FB EDITED by a reviewer" in c and "My hand-written FB post" in c for c in comments)
    assert not any("APPROVED" in c for c in comments)  # an edit is not an approval
    record = store.find_by_task_id(root_id)
    assert record["manual_edits"]["fb"]["text"] == "My hand-written FB post"
    assert record["manual_edits"]["fb"]["base_task_id"] == root_id


def test_edits_for_different_platforms_do_not_clobber_each_other(conn, ops, store, review_config, monkeypatch):
    root_id = _first_round(conn, ops, store, review_config, monkeypatch, "e-2")
    pipeline.handle_review_action(conn, ops, store, task_id=root_id, platform="fb", action="edited", comment="", edited_text="FB mine")
    pipeline.handle_review_action(conn, ops, store, task_id=root_id, platform="blog", action="edited", comment="", edited_text="Blog mine", edited_seo=SEO)
    record = store.find_by_task_id(root_id)
    assert record["manual_edits"]["fb"]["text"] == "FB mine"
    assert record["manual_edits"]["blog"]["text"] == "Blog mine"
    assert any("SEO title: My title" in c.body for c in kb.list_comments(conn, root_id))
    assert store.get_submission(root_id) is None  # still no shadow record keyed by task id


def test_refine_after_edit_redrafts_from_the_edited_text(conn, ops, store, review_config, monkeypatch):
    root_id = _first_round(conn, ops, store, review_config, monkeypatch, "e-3")
    pipeline.handle_review_action(conn, ops, store, task_id=root_id, platform="fb", action="edited", comment="", edited_text="MANUAL FB VERSION")
    result = pipeline.handle_review_action(conn, ops, store, task_id=root_id, platform="fb", action="refine", comment="shorter")
    body = kb.get_task(conn, result["task_id"]).body
    assert "MANUAL FB VERSION" in body
    assert "Stylus FB original" not in body


def test_refine_without_an_edit_is_unchanged_and_other_platforms_unaffected(conn, ops, store, review_config, monkeypatch):
    root_id = _first_round(conn, ops, store, review_config, monkeypatch, "e-4")
    pipeline.handle_review_action(conn, ops, store, task_id=root_id, platform="fb", action="edited", comment="", edited_text="MANUAL FB VERSION")
    result = pipeline.handle_review_action(conn, ops, store, task_id=root_id, platform="ig", action="refine", comment="warmer")
    body = kb.get_task(conn, result["task_id"]).body
    assert "Stylus IG original" in body and "MANUAL FB VERSION" not in body


def test_a_newer_stylus_round_supersedes_an_older_manual_edit(conn, ops, store, review_config, monkeypatch):
    root_id = _first_round(conn, ops, store, review_config, monkeypatch, "e-5")
    pipeline.handle_review_action(conn, ops, store, task_id=root_id, platform="fb", action="edited", comment="", edited_text="OLD MANUAL")
    r1 = pipeline.handle_review_action(conn, ops, store, task_id=root_id, platform="fb", action="refine", comment="one")
    _complete_as_stylus(conn, r1["task_id"], {**STYLUS_METADATA, "fb": "Stylus round-1 FB"})
    monkeypatch.setattr(pipeline, "update_review", lambda config, **kw: None)
    pipeline.handle_stylus_completion(conn, ops, store, review_config, r1["task_id"])
    r2 = pipeline.handle_review_action(conn, ops, store, task_id=root_id, platform="fb", action="refine", comment="two")
    body = kb.get_task(conn, r2["task_id"]).body
    assert "Stylus round-1 FB" in body and "OLD MANUAL" not in body


def test_approve_after_edit_behaves_exactly_like_a_plain_approve(conn, ops, store, review_config, monkeypatch):
    root_id = _first_round(conn, ops, store, review_config, monkeypatch, "e-6")
    pipeline.handle_review_action(conn, ops, store, task_id=root_id, platform="fb", action="edited", comment="", edited_text="mine")
    result = pipeline.handle_review_action(conn, ops, store, task_id=root_id, platform="fb", action="approved", comment="")
    assert result == {"action": "approved", "task_id": root_id, "platform": "fb"}
    assert any("FB APPROVED via review page" in c.body for c in kb.list_comments(conn, root_id))


def test_edit_without_text_is_refused(conn, ops, store, review_config, monkeypatch):
    root_id = _first_round(conn, ops, store, review_config, monkeypatch, "e-7")
    with pytest.raises(pipeline.PipelineError):
        pipeline.handle_review_action(conn, ops, store, task_id=root_id, platform="fb", action="edited", comment="", edited_text="  ")


def test_store_manual_edits_merge_one_level_deep(tmp_path):
    s = EventPostPipelineStore(tmp_path / "s.json")
    s.upsert_submission("sub", {"manual_edits": {"fb": {"text": "a"}}})
    s.upsert_submission("sub", {"manual_edits": {"ig": {"text": "b"}}, "rounds": {"fb": {"round": 1}}})
    rec = s.get_submission("sub")
    assert set(rec["manual_edits"]) == {"fb", "ig"} and rec["rounds"]["fb"]["round"] == 1
