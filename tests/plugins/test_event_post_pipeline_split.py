"""Split intake (``blog_enrichment.split``): S (fb+ig) and B (blog) joined into one review page.

Real throwaway Kanban SQLite board; only the review-page HTTP client is faked."""

from __future__ import annotations

import types
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from plugins.platforms.event_post_pipeline import hooks, pipeline
from plugins.platforms.event_post_pipeline.review_client import ReviewClientConfig, ReviewClientError
from plugins.platforms.event_post_pipeline.store import EventPostPipelineStore

PACK = {
    "submissionId": "sub-split-1", "date": "2026-09-20",
    "description": "A day-long meditation retreat with a visiting monk.", "quote": "Peace comes from within.",
    "first": {"finalUrl": "https://res.cloudinary.com/demo/first.jpg"},
    "last": {"finalUrl": "https://res.cloudinary.com/demo/last.jpg"},
    "rest": [{"finalUrl": "https://res.cloudinary.com/demo/rest1.jpg"}],
}
SOCIAL_MD = {"event_title": "Retreat Day", "fb": "FB copy", "ig": "IG copy"}
BLOG = {
    "body": "Blog body", "focus_keyword": "retreat", "supporting_keywords": ["a"], "seo_title": "T",
    "meta_description": "M", "url_slug": "retreat-day",
}
BLOG_MD = {"event_title": "Retreat Day", "blog": BLOG}
URL = "https://bmcposts.example/review/abc123"


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


@pytest.fixture
def cfg():
    return ReviewClientConfig(base_url="https://bmcposts.example", create_secret="test-secret")


@pytest.fixture
def calls(monkeypatch):
    rec = {"create": [], "update": [], "events": []}

    def fake_create(config, **kw):
        rec["create"].append(kw)
        if rec.get("fail"):
            raise ReviewClientError("boom")
        return URL

    def fake_update(config, **kw):
        rec["update"].append(kw)

    monkeypatch.setattr(pipeline, "create_review", fake_create)
    monkeypatch.setattr(pipeline, "update_review", fake_update)
    monkeypatch.setattr(
        pipeline, "_track_event_only", lambda url, tid, step, status, **kw: rec["events"].append(step))
    monkeypatch.setattr(pipeline, "_track_run", lambda url, tid, **kw: rec["events"].append(kw["step"]))
    return rec


def _complete(conn, task_id, metadata):
    if kb.get_task(conn, task_id).status == "ready":
        kb.claim_task(conn, task_id, claimer="stylus")
    assert kb.complete_task(conn, task_id, summary="done", metadata=metadata)


def _intake(conn, ops, store, **kw):
    s = pipeline.handle_intake(conn, ops, store, submission_id=PACK["submissionId"], event_pack=PACK, split=True, **kw)
    b = kb.get_task(conn, s).id and ops.list_tasks(conn, tenant=pipeline.TENANT, assignee="stylus")
    blog = next(t for t in b if t.idempotency_key == f"{s}-blog-r1")
    return s, blog.id


def _handle(conn, ops, store, cfg, tid, now=1000.0):
    return pipeline.handle_stylus_completion(conn, ops, store, cfg, tid, now=now)


# --- flags off / intake facts ---------------------------------------------------------------------

def test_flags_off_intake_is_one_unchanged_task(conn, ops, store):
    tid = pipeline.handle_intake(conn, ops, store, submission_id="off-1", event_pack={**PACK, "submissionId": "off-1"})
    tasks = ops.list_tasks(conn, tenant=pipeline.TENANT)
    assert [t.id for t in tasks] == [tid]
    t = kb.get_task(conn, tid)
    assert t.body == pipeline._wrap_untrusted(PACK) and "task-kind" not in t.body
    assert t.title.startswith("Draft social copy: ") and t.idempotency_key == "off-1"
    assert "join" not in store.get_submission("off-1")


def test_flags_off_args_identical(conn, ops, store):
    seen = []

    class Spy(pipeline.KanbanOps):
        def create_task(self, c, **kw):
            seen.append(kw)
            return super().create_task(c, **kw)

    pipeline.handle_intake(conn, Spy(kb), store, submission_id="off-2", event_pack={**PACK, "submissionId": "off-2"})
    assert len(seen) == 1 and set(seen[0]) == {"title", "body", "assignee", "idempotency_key", "tenant", "created_by"}


def test_flag_helpers():
    assert pipeline.split_enabled({"blog_enrichment": {"split": True}})
    assert not pipeline.split_enabled({"blog_enrichment": {"split": "true"}})
    assert not pipeline.split_enabled({}) and not pipeline.split_enabled(None)
    assert pipeline.blog_wait_seconds({}) == 1200.0
    assert pipeline.blog_wait_seconds({"blog_enrichment": {"blog_wait_seconds": 60}}) == 60.0
    assert pipeline.blog_wait_seconds({"blog_enrichment": {"blog_wait_seconds": -1}}) == 1200.0


def test_split_intake_keys_markers_no_parent(conn, ops, store):
    s, b = _intake(conn, ops, store)
    S, B = kb.get_task(conn, s), kb.get_task(conn, b)
    assert S.idempotency_key == PACK["submissionId"] and S.assignee == "stylus" and S.tenant == pipeline.TENANT
    assert S.body.startswith("[task-kind: social]\n<untrusted_submission>")
    assert B.idempotency_key == f"{s}-blog-r1" and B.assignee == "stylus" and B.tenant == pipeline.TENANT
    assert B.body.startswith("[task-kind: blog]\n<untrusted_submission>") and B.title.startswith("Draft blog: ")
    assert B.status == "ready"  # no parent: B does not wait for S
    assert store.get_submission(PACK["submissionId"])["join"]["blog_task_id"] == b
    assert pipeline.root_task_id_for_idempotency_key(B.idempotency_key) == s


def test_blog_parents_hook(conn, ops, store):
    r = kb.create_task(conn, title="R", body="", assignee="webmaster")
    s = pipeline.handle_intake(conn, ops, store, submission_id="p-1", event_pack={**PACK, "submissionId": "p-1"},
                               split=True, blog_parents=[r])
    B = next(t for t in ops.list_tasks(conn, tenant=pipeline.TENANT) if t.idempotency_key == f"{s}-blog-r1")
    assert B.status == "todo"


def test_intake_retry_creates_nothing_new(conn, ops, store):
    s, b = _intake(conn, ops, store)
    s2, b2 = _intake(conn, ops, store)
    assert (s, b) == (s2, b2) and len(ops.list_tasks(conn, tenant=pipeline.TENANT)) == 2


# --- join ------------------------------------------------------------------------------------------

def test_social_then_blog_one_page(conn, ops, store, cfg, calls):
    s, b = _intake(conn, ops, store)
    _complete(conn, s, SOCIAL_MD)
    r1 = _handle(conn, ops, store, cfg, s)
    assert r1["action"] == "join_waiting" and calls["create"] == [] and "join_waiting" in calls["events"]
    _complete(conn, b, BLOG_MD)
    r2 = _handle(conn, ops, store, cfg, b)
    assert r2 == {"action": "created_review_page", "task_id": s, "root_task_id": s, "review_url": URL}
    assert len(calls["create"]) == 1
    kw = calls["create"][0]
    assert kw["task_id"] == s and kw["fb"] == "FB copy" and kw["ig"] == "IG copy" and kw["blog_body"] == "Blog body"
    assert "blog_state" not in kw and kw["blog_seo"]["urlSlug"] == "retreat-day"
    assert "join_done" in calls["events"]
    assert hooks._notification_message(r2) == f"New event post ready for review: {URL}"
    assert any(c.body == f"Review page: {URL}" for c in ops.list_comments(conn, s))


def test_blog_then_social_one_page(conn, ops, store, cfg, calls):
    s, b = _intake(conn, ops, store)
    _complete(conn, b, BLOG_MD)
    assert _handle(conn, ops, store, cfg, b)["action"] == "join_waiting"
    assert calls["create"] == []
    _complete(conn, s, SOCIAL_MD)
    assert _handle(conn, ops, store, cfg, s)["action"] == "created_review_page"
    assert len(calls["create"]) == 1


def test_duplicate_completion_events(conn, ops, store, cfg, calls):
    s, b = _intake(conn, ops, store)
    _complete(conn, s, SOCIAL_MD)
    _complete(conn, b, BLOG_MD)
    assert _handle(conn, ops, store, cfg, s)["action"] == "join_waiting"
    assert _handle(conn, ops, store, cfg, s)["action"] == "join_waiting"
    assert _handle(conn, ops, store, cfg, b)["action"] == "created_review_page"
    for tid in (b, s, b):
        assert hooks._notification_message(_handle(conn, ops, store, cfg, tid) or {"action": "x"}) is None
    assert len(calls["create"]) == 1 and calls["update"] == []


def test_restart_between_halves(conn, ops, tmp_path, cfg, calls):
    path = tmp_path / "restart.json"
    st = EventPostPipelineStore(path)
    s, b = _intake(conn, ops, st)
    _complete(conn, s, SOCIAL_MD)
    _handle(conn, ops, st, cfg, s)
    st2 = EventPostPipelineStore(path)  # gateway restart
    _complete(conn, b, BLOG_MD)
    assert _handle(conn, ops, st2, cfg, b)["action"] == "created_review_page"
    assert len(calls["create"]) == 1
    assert EventPostPipelineStore(path).get_submission(PACK["submissionId"])["join"]["state"] == "done"


def test_stale_long_lived_store_does_not_clobber_join(conn, ops, tmp_path, cfg, calls):
    path = tmp_path / "shared.json"
    gateway = EventPostPipelineStore(path)  # long-lived instance, like the adapter's
    s, b = _intake(conn, ops, gateway)
    _complete(conn, s, SOCIAL_MD)
    _handle(conn, ops, EventPostPipelineStore(path), cfg, s)  # worker process
    gateway.upsert_submission(PACK["submissionId"], {"x": 1})  # adapter writes with its stale view
    assert EventPostPipelineStore(path).get_submission(PACK["submissionId"])["join"]["social"]["fb"] == "FB copy"


def test_stray_keys_warned_and_ignored(conn, ops, store, cfg, calls, caplog):
    s, b = _intake(conn, ops, store)
    _complete(conn, s, {**SOCIAL_MD, "blog": BLOG, "junk": 1})
    with caplog.at_level("WARNING"):
        _handle(conn, ops, store, cfg, s)
    assert "unexpected metadata keys" in caplog.text and "junk" in caplog.text
    assert "blog" not in store.get_submission(PACK["submissionId"])["join"]
    _complete(conn, b, {**BLOG_MD, "fb": "stray"})
    _handle(conn, ops, store, cfg, b)
    assert calls["create"][0]["fb"] == "FB copy" and calls["create"][0]["blog_body"] == "Blog body"


def test_missing_required_parts_rejected(conn, ops, store, cfg, calls):
    s, b = _intake(conn, ops, store)
    _complete(conn, s, {"event_title": "x", "fb": "only fb"})
    with pytest.raises(pipeline.PipelineError):
        _handle(conn, ops, store, cfg, s)
    _complete(conn, b, {"event_title": "x", "fb": "a", "ig": "b"})
    with pytest.raises(pipeline.PipelineError):
        _handle(conn, ops, store, cfg, b)
    assert store.get_submission(PACK["submissionId"])["join"].get("social") is None


def test_page_creation_failure_then_retry(conn, ops, store, cfg, calls):
    s, b = _intake(conn, ops, store)
    _complete(conn, s, SOCIAL_MD)
    _complete(conn, b, BLOG_MD)
    _handle(conn, ops, store, cfg, s)
    calls["fail"] = True
    with pytest.raises(pipeline.PipelineError):
        _handle(conn, ops, store, cfg, b)
    j = store.get_submission(PACK["submissionId"])["join"]
    assert j["state"] == "waiting" and j["social"] and j["blog"]
    calls["fail"] = False
    out = pipeline.sweep_join(conn, ops, store, cfg, now=2000.0)  # sweep retries a ready join
    assert [r["action"] for r in out] == ["created_review_page"] and "fallback" not in out[0]
    assert pipeline.sweep_join(conn, ops, store, cfg, now=3000.0) == []
    assert calls["create"][-1]["blog_body"] == "Blog body"


def test_stale_claim_taken_over_fresh_claim_not(store):
    store.upsert_submission("x", {"root_task_id": "t"})
    store.join_init("x", None)
    assert store.join_claim("x", 100.0) is True
    assert store.join_claim("x", 150.0) is False
    assert store.join_claim("x", 500.0) is True


# --- fallback / late blog ---------------------------------------------------------------------------

def test_fallback_after_wait_then_late_blog(conn, ops, store, cfg, calls):
    s, b = _intake(conn, ops, store)
    _complete(conn, s, SOCIAL_MD)
    _handle(conn, ops, store, cfg, s, now=1000.0)
    assert pipeline.sweep_join(conn, ops, store, cfg, now=2199.0, blog_wait_seconds=1200) == []
    out = pipeline.sweep_join(conn, ops, store, cfg, now=2200.0, blog_wait_seconds=1200)
    assert len(out) == 1 and out[0]["fallback"] is True
    kw = calls["create"][0]
    assert kw["blog_state"] == "in_progress" and kw["blog_body"] == "" and kw["fb"] == "FB copy"
    assert "blog_late_fallback" in calls["events"] and "join_done" in calls["events"]
    assert hooks._notification_message(out[0]) == f"New event post ready for review: {URL}"
    assert pipeline.sweep_join(conn, ops, store, cfg, now=9999.0) == []  # idempotent
    _complete(conn, b, BLOG_MD)
    late = _handle(conn, ops, store, cfg, b, now=3000.0)
    assert late["action"] == "updated_review_page" and late["late_blog"] is True
    assert calls["update"][0]["platform"] == "blog" and calls["update"][0]["text"] == "Blog body"
    assert hooks._notification_message(late) == f"Blog draft ready for review: {URL}"
    assert len(calls["create"]) == 1
    assert hooks._notification_message(_handle(conn, ops, store, cfg, b) or {"action": "x"}) is None  # duplicate
    assert len(calls["update"]) == 1


def test_only_blog_done_sweep_waits(conn, ops, store, cfg, calls):
    s, b = _intake(conn, ops, store)
    _complete(conn, b, BLOG_MD)
    _handle(conn, ops, store, cfg, b)
    assert pipeline.sweep_join(conn, ops, store, cfg, now=10**9) == [] and calls["create"] == []


def test_refine_after_fallback_tolerates_missing_blog_draft(conn, ops, store, cfg, calls):
    s, b = _intake(conn, ops, store)
    _complete(conn, s, SOCIAL_MD)
    _handle(conn, ops, store, cfg, s, now=1000.0)
    pipeline.sweep_join(conn, ops, store, cfg, now=5000.0)
    # B never completed; the reviewer presses Refine on the empty blog row.
    res = pipeline.handle_review_action(
        conn, ops, store, task_id=s, platform="blog", action="refine", comment="please write it")
    assert res["round"] == 2
    task = kb.get_task(conn, res["task_id"])
    assert task.idempotency_key == f"{s}-blog-r2"
    assert task.body.startswith("[task-kind: blog]\n") and "first draft was not produced" in task.body
    _complete(conn, res["task_id"], {"blog": BLOG})
    done = _handle(conn, ops, store, cfg, res["task_id"])
    assert done["action"] == "updated_review_page" and not done.get("late_blog")
    assert calls["update"][0]["text"] == "Blog body"


def test_flags_off_refine_body_unchanged(conn, ops, store):
    body = pipeline._refine_task_body(record={"date": "d"}, platform="blog", previous_draft="old", comment="c")
    assert "[task-kind" not in body and "Previous BLOG draft:\nold" in body


# --- hooks -------------------------------------------------------------------------------------------

def test_blocked_blog_task_still_resolves_root(monkeypatch):
    calls = []
    monkeypatch.setattr(hooks, "_load_pipeline_extra", lambda: {"board": None})
    monkeypatch.setattr(hooks.db, "resolve_database_url", lambda e: "postgres://x")
    task = types.SimpleNamespace(tenant=pipeline.TENANT, idempotency_key="t_root-blog-r1", title="Draft blog: X")
    monkeypatch.setattr(kb, "get_task", lambda c, tid: task)
    monkeypatch.setattr(kbc, "connect", lambda board=None: types.SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(pipeline, "track_stylus_blocked", lambda url, tid, **kw: calls.append(tid))
    hooks.on_kanban_task_blocked(task_id="t_b", assignee="stylus", reason="x")
    assert calls == ["t_root"]


def test_run_join_sweep_sends_single_message(monkeypatch):
    sent = []
    monkeypatch.setattr(hooks, "_load_pipeline_extra", lambda: {"store_path": "/nonexistent/s.json", "blog_enrichment": {"blog_wait_seconds": 5}})
    monkeypatch.setattr(hooks.db, "resolve_database_url", lambda e: "")
    monkeypatch.setattr(hooks.security, "resolve_review_create_secret", lambda e: "s")
    monkeypatch.setattr(hooks, "_shadow_recorder", lambda e, s: None)
    monkeypatch.setattr(kbc, "connect", lambda board=None: types.SimpleNamespace(close=lambda: None))
    seen = {}
    monkeypatch.setattr(pipeline, "sweep_join", lambda *a, **kw: (seen.update(kw), [{"action": "created_review_page", "task_id": "t", "root_task_id": "t", "review_url": URL}])[1])
    monkeypatch.setattr(hooks, "_run_async", lambda f: sent.append("sent"))
    monkeypatch.setattr(pipeline, "track_notification", lambda *a, **kw: None)
    assert len(hooks.run_join_sweep(now=1.0)) == 1 and sent == ["sent"] and seen["blog_wait_seconds"] == 5.0


def test_review_client_blog_state_only_when_set(monkeypatch):
    from plugins.platforms.event_post_pipeline import review_client as rc
    sent = []
    monkeypatch.setattr(rc, "_post_json", lambda url, payload, secret, timeout: (sent.append(payload), {"url": URL})[1])
    base = dict(task_id="t", event_title="e", date="d", images=[], fb="f", ig="i", blog_body="", blog_seo={})
    rc.create_review(ReviewClientConfig("https://x", "s"), **base)
    rc.create_review(ReviewClientConfig("https://x", "s"), blog_state="in_progress", **base)
    assert "blogState" not in sent[0] and sent[1]["blogState"] == "in_progress"
