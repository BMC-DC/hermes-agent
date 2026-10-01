"""Manual reviewer edits for the newsletter pipeline: Hermes validates the edited copy,
re-renders the HTML with the same deterministic template as a Stylus draft, keeps the
draft it would send to Brevo in step, tells Stylus (Kanban comment + redraft base), and
refuses to approve a manually-edited draft whose HTML doesn't match what the reviewer
approved. Real throwaway Kanban board."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from plugins.platforms.newsletter_pipeline import models, pipeline
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
    "programsList": [{"label": "Sunday Meditation", "detail": "Every Sunday 9am"}],
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

EDITED = {
    **STYLUS_METADATA,
    "subject_line": "My Edited Subject",
    "recap_paragraph": "Hand-written recap with <b>markup</b> & ampersand.",
}


def _complete_as_stylus(conn, task_id: str, metadata: dict) -> None:
    if kb.get_task(conn, task_id).status == "ready":
        kb.claim_task(conn, task_id, claimer="stylus")
    assert kb.complete_task(conn, task_id, summary="drafted newsletter", metadata=metadata)


def _first_round(conn, ops, store, review_config, monkeypatch, sid):
    root_id = pipeline.handle_intake(conn, ops, store, submission_id=sid, intake={**INTAKE, "submissionId": sid})
    _complete_as_stylus(conn, root_id, STYLUS_METADATA)
    created = {}
    monkeypatch.setattr(pipeline, "create_review", lambda config, **kw: created.update(kw) or "https://bmcposts.example/review/newsletter/xyz")
    pipeline.handle_stylus_completion(conn, ops, store, review_config, root_id)
    return root_id, created


def _sha(html: str) -> str:
    return hashlib.sha256(html.encode("utf-8")).hexdigest()


# --- models -----------------------------------------------------------------------------


def test_parse_edited_requires_all_seven_fields_and_single_line_rules():
    base = {"taskId": "t_1", "action": "edited", "copy": dict(EDITED)}
    assert models.parse_review_action(base).copy == EDITED
    for key in models.EDITED_COPY_FIELDS:
        assert models.parse_review_action({**base, "copy": {k: v for k, v in EDITED.items() if k != key}}) is None
        assert models.parse_review_action({**base, "copy": {**EDITED, key: "  "}}) is None
    assert models.parse_review_action({**base, "copy": {**EDITED, "subject_line": "two\nlines"}}) is None
    assert models.parse_review_action({**base, "copy": {**EDITED, "subject_line": "x" * 251}}) is None
    assert models.parse_review_action({**base, "copy": {**EDITED, "recap_paragraph": "x" * 6001}}) is None
    assert models.parse_review_action({**base, "copy": None}) is None
    # multi-line paragraphs are fine
    assert models.parse_review_action({**base, "copy": {**EDITED, "recap_paragraph": "a\n\nb"}}) is not None


def test_parse_draft_sha256_only_on_approved_and_must_be_hex():
    ok = models.parse_review_action({"taskId": "t_1", "action": "approved", "draftSha256": "a" * 64})
    assert ok is not None and ok.draft_sha256 == "a" * 64
    assert models.parse_review_action({"taskId": "t_1", "action": "approved", "draftSha256": "nothex"}) is None
    assert models.parse_review_action({"taskId": "t_1", "action": "rejected", "comment": "x", "draftSha256": "a" * 64}) is None
    plain = models.parse_review_action({"taskId": "t_1", "action": "approved"})
    assert plain is not None and plain.draft_sha256 is None


# --- pipeline -------------------------------------------------------------------------


def test_stylus_drafts_now_forward_copy_fields_to_the_portal(conn, ops, store, review_config, monkeypatch):
    _root, created = _first_round(conn, ops, store, review_config, monkeypatch, "n-0")
    assert created["draft"]["copyFields"] == STYLUS_METADATA


def test_edit_rerenders_stores_draft_and_acknowledges_stylus(conn, ops, store, review_config, monkeypatch):
    root_id, created = _first_round(conn, ops, store, review_config, monkeypatch, "n-1")
    result = pipeline.handle_review_action(conn, ops, store, review_config, task_id=root_id, action="edited", comment="", copy=EDITED)

    assert result["action"] == "edited"
    draft = result["draft"]
    assert draft["subjectLine"] == "My Edited Subject"
    assert draft["copyFields"] == EDITED
    # Rendered by the same template, with the markup in the copy safely escaped:
    assert "&lt;b&gt;markup&lt;/b&gt; &amp; ampersand." in draft["assembledHtml"]
    assert "<b>markup</b>" not in draft["assembledHtml"]
    # Intake-owned pieces (CTA, recap image, programs) survive the re-render:
    assert "https://example.org/retreat" in draft["assembledHtml"]
    assert "recap.jpg" in draft["assembledHtml"]
    assert "Sunday Meditation" in draft["assembledHtml"]
    # What a later approve sends is the edited draft:
    record = store.find_by_task_id(root_id)
    assert record["latest_draft"] == draft
    assert any("EDITED by a reviewer" in c.body and "My Edited Subject" in c.body for c in kb.list_comments(conn, root_id))
    assert not any("APPROVED" in c.body for c in kb.list_comments(conn, root_id))


def test_edit_render_is_deterministic_and_idempotent(conn, ops, store, review_config, monkeypatch):
    root_id, _ = _first_round(conn, ops, store, review_config, monkeypatch, "n-2")
    a = pipeline.handle_review_action(conn, ops, store, review_config, task_id=root_id, action="edited", comment="", copy=EDITED)
    b = pipeline.handle_review_action(conn, ops, store, review_config, task_id=root_id, action="edited", comment="", copy=EDITED)
    assert a["draft"] == b["draft"]


def test_unedited_copy_renders_identically_to_the_stylus_draft(conn, ops, store, review_config, monkeypatch):
    root_id, created = _first_round(conn, ops, store, review_config, monkeypatch, "n-3")
    result = pipeline.handle_review_action(conn, ops, store, review_config, task_id=root_id, action="edited", comment="", copy=dict(STYLUS_METADATA))
    assert result["draft"]["assembledHtml"] == created["draft"]["assembledHtml"]


def test_edit_refused_without_stored_intake(conn, ops, store, review_config):
    task_id = kb.create_task(conn, title="Draft newsletter: x", body="", assignee="stylus", idempotency_key="orphan", tenant=pipeline.TENANT)
    with pytest.raises(pipeline.PipelineError):
        pipeline.handle_review_action(conn, ops, store, review_config, task_id=task_id, action="edited", comment="", copy=EDITED)


def test_refine_after_edit_redrafts_from_edited_copy_and_a_new_round_supersedes_it(conn, ops, store, review_config, monkeypatch):
    root_id, _ = _first_round(conn, ops, store, review_config, monkeypatch, "n-4")
    pipeline.handle_review_action(conn, ops, store, review_config, task_id=root_id, action="edited", comment="", copy=EDITED)
    r1 = pipeline.handle_review_action(conn, ops, store, review_config, task_id=root_id, action="refine", comment="shorter")
    body = kb.get_task(conn, r1["task_id"]).body
    prev = body.split("Previous draft:")[1].split("Requested change")[0]
    assert "subject_line: My Edited Subject" in prev
    assert "Hand-written recap" in prev
    assert "We gathered for a wonderful evening of practice." not in prev  # Stylus's superseded original

    _complete_as_stylus(conn, r1["task_id"], {**STYLUS_METADATA, "recap_paragraph": "Stylus round-1 recap"})
    monkeypatch.setattr(pipeline, "update_review", lambda config, **kw: None)
    pipeline.handle_stylus_completion(conn, ops, store, review_config, r1["task_id"])
    assert store.find_by_task_id(root_id)["edited_copy"] is None  # cleared by the newer Stylus round

    r2 = pipeline.handle_review_action(conn, ops, store, review_config, task_id=root_id, action="refine", comment="again")
    body2 = kb.get_task(conn, r2["task_id"]).body
    assert "My Edited Subject" not in body2


def test_refine_without_any_edit_is_unchanged(conn, ops, store, review_config, monkeypatch):
    root_id, _ = _first_round(conn, ops, store, review_config, monkeypatch, "n-5")
    r = pipeline.handle_review_action(conn, ops, store, review_config, task_id=root_id, action="refine", comment="warmer")
    prev = kb.get_task(conn, r["task_id"]).body.split("Previous draft:")[1]
    assert "subject_line: A Mindful September" in prev


# --- approve integrity guard ------------------------------------------------------------


def test_approve_with_matching_hash_proceeds(conn, ops, store, review_config, monkeypatch):
    root_id, _ = _first_round(conn, ops, store, review_config, monkeypatch, "n-6")
    draft = pipeline.handle_review_action(conn, ops, store, review_config, task_id=root_id, action="edited", comment="", copy=EDITED)["draft"]
    result = pipeline.handle_review_action(
        conn, ops, store, review_config, task_id=root_id, action="approved", comment="", draft_sha256=_sha(draft["assembledHtml"]),
    )
    assert result["action"] == "approved"


def test_approve_with_mismatching_hash_refuses_and_leaves_no_approved_trace(conn, ops, store, review_config, monkeypatch):
    root_id, created = _first_round(conn, ops, store, review_config, monkeypatch, "n-7")
    # Hermes still holds the ORIGINAL Stylus draft; the portal claims the reviewer approved an edited one.
    edited_html = created["draft"]["assembledHtml"] + "<!-- edited -->"
    with pytest.raises(pipeline.PipelineError, match="refusing to approve"):
        pipeline.handle_review_action(
            conn, ops, store, review_config, task_id=root_id, action="approved", comment="", draft_sha256=_sha(edited_html),
        )
    assert not any("APPROVED" in c.body for c in kb.list_comments(conn, root_id))


def test_edited_approve_sends_the_edited_html_to_brevo(conn, ops, store, review_config, monkeypatch):
    root_id, _ = _first_round(conn, ops, store, review_config, monkeypatch, "n-8")
    draft = pipeline.handle_review_action(conn, ops, store, review_config, task_id=root_id, action="edited", comment="", copy=EDITED)["draft"]
    sent = {}

    def fake_send(config, *, subject, html_content, campaign_name):
        sent.update(subject=subject, html=html_content)
        return 4242

    monkeypatch.setattr(pipeline.brevo_client, "create_and_send_campaign", fake_send)
    monkeypatch.setattr(pipeline, "record_send", lambda config, **kw: None)
    result = pipeline.handle_review_action(
        conn, ops, store, review_config, task_id=root_id, action="approved", comment="",
        brevo_config=object(), draft_sha256=_sha(draft["assembledHtml"]),
    )
    assert result["brevo_campaign_id"] == 4242
    assert sent["subject"] == "My Edited Subject"
    assert sent["html"] == draft["assembledHtml"]
