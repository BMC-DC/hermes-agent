"""Refine carry-over (WP8): a blog refine round gets the stored research brief block; nothing else changes."""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from plugins.platforms.event_post_pipeline import hooks, pipeline, research, root_config
from plugins.platforms.event_post_pipeline.review_client import ReviewClientConfig
from plugins.platforms.event_post_pipeline.site_client import SiteResult
from plugins.platforms.event_post_pipeline.store import EventPostPipelineStore

P1 = "https://bmc.example/programs/meditate"
PACK = {
    "submissionId": "sub-f-1", "date": "2026-09-20", "description": "A day-long meditation retreat.", "quote": "Peace.",
    "first": {"finalUrl": "https://res.cloudinary.com/demo/first.jpg"}, "last": {"finalUrl": "https://res.cloudinary.com/demo/last.jpg"},
}
GOOD = {"schema": 1, "research_brief": {"status": "ok", "program_standing_description": "A weekly sitting practice.", "related_posts": [],
                                        "suggested_links": [{"url": P1, "anchor_hint": "weekly sitting"}], "teaching_background": [], "cautions": []}}
BLOG = {"body": "We sat.", "focus_keyword": "k", "supporting_keywords": [], "seo_title": "t", "meta_description": "m", "url_slug": "s"}


class FakeSite:
    def health(self, **_):
        return SiteResult(available=True)

    def context_pack(self, pack, **_):
        return SiteResult(available=True, data={"candidates": [{"url": P1, "title": "Meditate", "kind": "program", "tier": "strong"}]}, text="x", corpus_hash="h")


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
    c = kbc.connect()
    yield c
    c.close()


@pytest.fixture
def ops():
    return pipeline.KanbanOps(kb)


@pytest.fixture
def store(tmp_path):
    return EventPostPipelineStore(tmp_path / "store.json")


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    monkeypatch.setattr(pipeline, "_track_event_only", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "_track_run", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "create_review", lambda config, **kw: "https://bmcposts.example/review/abc123")
    monkeypatch.setattr(pipeline, "update_review", lambda config, **kw: None)


def _complete(conn, tid, metadata):
    if kb.get_task(conn, tid).status == "ready":
        kb.claim_task(conn, tid, claimer="x")
    assert kb.complete_task(conn, tid, summary="done", metadata=metadata)


def _flow(conn, ops, store, monkeypatch, *, handoff):
    extra = {"store_path": str(store.path)}
    monkeypatch.setattr(hooks, "_load_pipeline_extra", lambda: extra)
    monkeypatch.setattr(hooks.db, "resolve_database_url", lambda e=None: "")
    pl = research.ResearchPlanner(research.ResearchConfig(enabled=True, handoff=handoff), client_factory=lambda _t: FakeSite())
    root = pipeline.handle_intake(conn, ops, store, submission_id="sub-f-1", event_pack=PACK, split=True, research=pl)
    tasks = {t.idempotency_key: t for t in ops.list_tasks(conn, tenant=pipeline.TENANT)}
    r, b = tasks["sub-f-1-research"].id, tasks[f"{root}-blog-r1"].id
    _complete(conn, r, GOOD)
    hooks.on_kanban_task_completed(task_id=r)
    cfg = ReviewClientConfig(base_url="https://bmcposts.example", create_secret="x")
    fresh = lambda: EventPostPipelineStore(store.path)  # noqa: E731
    _complete(conn, root, {"event_title": "T", "fb": "f", "ig": "i"})
    pipeline.handle_stylus_completion(conn, ops, fresh(), cfg, root, now=1.0)
    _complete(conn, b, {"event_title": "T", "blog": BLOG})
    pipeline.handle_stylus_completion(conn, ops, fresh(), cfg, b, now=2.0)
    return root, r, b, fresh


def _refine(conn, ops, st, root, platform, comment="make it warmer"):
    return pipeline.handle_review_action(conn, ops, st, task_id=root, platform=platform, action="refine", comment=comment)


def test_blog_refine_body_carries_the_stored_brief_when_handoff_was_on(conn, ops, store, monkeypatch):
    root, r, b, fresh = _flow(conn, ops, store, monkeypatch, handoff=True)
    rendered = fresh().get_submission("sub-f-1")["research"]["rendered"]
    assert rendered and "<site_context" in rendered[0]
    before = {t.id for t in ops.list_tasks(conn, tenant=pipeline.TENANT, include_archived=True)}
    out = _refine(conn, ops, fresh(), root, "blog")
    body = kb.get_task(conn, out["task_id"]).body
    for part in rendered:
        assert part in body                                              # verbatim, NOT defused (the tag is the point)
    assert body.index("</untrusted_submission>") < body.index('<site_context kind="research_brief"') < body.index("Previous BLOG draft:")
    # no new research task, numbering unaffected (first refine is r2, B being r1)
    new = [t for t in ops.list_tasks(conn, tenant=pipeline.TENANT, include_archived=True) if t.id not in before]
    assert [t.idempotency_key for t in new] == [f"{root}-blog-r2"] and out["round"] == 2
    assert [t.id for t in ops.list_tasks(conn, tenant=pipeline.TENANT, include_archived=True) if (t.idempotency_key or "").endswith("-research")] == [r]


def test_second_blog_refine_still_carries_it_and_is_r3(conn, ops, store, monkeypatch):
    root, r, b, fresh = _flow(conn, ops, store, monkeypatch, handoff=True)
    first = _refine(conn, ops, fresh(), root, "blog")
    _complete(conn, first["task_id"], {"blog": BLOG})
    second = _refine(conn, ops, fresh(), root, "blog", "again")
    assert second["round"] == 3 and '<site_context kind="research_brief"' in kb.get_task(conn, second["task_id"]).body


def test_fb_and_ig_refines_never_get_the_brief(conn, ops, store, monkeypatch):
    root, r, b, fresh = _flow(conn, ops, store, monkeypatch, handoff=True)
    for platform in ("fb", "ig"):
        out = _refine(conn, ops, fresh(), root, platform)
        assert "site_context" not in kb.get_task(conn, out["task_id"]).body


def test_handoff_off_blog_refine_has_no_brief(conn, ops, store, monkeypatch):
    root, r, b, fresh = _flow(conn, ops, store, monkeypatch, handoff=False)
    rec = fresh().get_submission("sub-f-1")["research"]
    assert rec["handoff"] is False and "rendered" not in rec
    out = _refine(conn, ops, fresh(), root, "blog")
    assert "site_context" not in kb.get_task(conn, out["task_id"]).body


def test_handoff_flag_false_in_the_store_wins_even_if_rendered_is_present(conn, ops, store, monkeypatch):
    root, r, b, fresh = _flow(conn, ops, store, monkeypatch, handoff=True)
    rec = fresh().get_submission("sub-f-1")["research"]
    st = fresh()
    st.upsert_submission("sub-f-1", {"research": {**rec, "handoff": False}})
    out = _refine(conn, ops, fresh(), root, "blog")
    assert "site_context" not in kb.get_task(conn, out["task_id"]).body


def test_body_is_byte_identical_when_there_is_no_stored_research():
    record = {"date": "2026-09-20", "description": "d", "quote": "q"}
    body = pipeline._refine_task_body(record=record, platform="blog", previous_draft="old draft", comment="shorter")
    assert body == "\n\n".join([
        "<untrusted_submission>", "Date: 2026-09-20", "Description: d", "Quote: q", "</untrusted_submission>",
        "Previous BLOG draft:\nold draft", "Requested change (reviewer's own words):\nshorter",
        "Only the BLOG content needs to be redrafted — the other platforms are unaffected and already handled separately.",
    ])
    for junk in ({"research": {}}, {"research": {"handoff": True}}, {"research": {"handoff": True, "rendered": []}},
                 {"research": {"handoff": True, "rendered": [5, ""]}}, {"research": "x"}):
        assert pipeline._refine_task_body(record={**record, **junk}, platform="blog", previous_draft="old draft", comment="shorter") == body
