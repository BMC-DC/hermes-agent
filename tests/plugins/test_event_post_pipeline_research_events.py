"""Research step observability (WP7): visualizer events, shadow records, shadow report. Real throwaway Kanban board."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from plugins.platforms.event_post_pipeline import hooks, pipeline, research, root_config, shadow, shadow_report
from plugins.platforms.event_post_pipeline.review_client import ReviewClientConfig
from plugins.platforms.event_post_pipeline.shadow import ShadowConfig, ShadowRecorder
from plugins.platforms.event_post_pipeline.site_client import SiteResult
from plugins.platforms.event_post_pipeline.store import EventPostPipelineStore

P1 = "https://bmc.example/programs/meditate"
P2 = "https://bmc.example/blog/earlier-retreat"
PACK = {
    "submissionId": "sub-e-1", "date": "2026-09-20", "description": "A day-long meditation retreat.", "quote": "Peace.",
    "first": {"finalUrl": "https://res.cloudinary.com/demo/first.jpg"}, "last": {"finalUrl": "https://res.cloudinary.com/demo/last.jpg"},
}
DATA = {"candidates": [{"url": P1, "title": "Meditate", "kind": "program", "tier": "strong"}, {"url": P2, "title": "Earlier", "kind": "post", "tier": "strong"}]}
GOOD = {"schema": 1, "research_brief": {
    "status": "ok", "program_standing_description": "A weekly sitting practice.",
    "related_posts": [{"title": "An earlier retreat", "url": P2, "why": "same program"}],
    "suggested_links": [{"url": P1, "anchor_hint": "weekly sitting"}],
    "teaching_background": [], "cautions": ["Do not reuse another session's details."],
    "web_request": {"reason": "more background would help", "topics": ["x"]}}}
NOTHING = {"schema": 1, "research_brief": {"status": "nothing_useful", "program_standing_description": None, "related_posts": [],
                                           "suggested_links": [], "teaching_background": [], "cautions": []}}
BLOG = {"body": f"We sat together. See [the program]({P1}) and https://elsewhere.example/x.", "focus_keyword": "k", "supporting_keywords": [],
        "seo_title": "t", "meta_description": "m", "url_slug": "s"}
URL = "https://bmcposts.example/review/abc123"


class FakeSite:
    def __init__(self, up=True):
        self.up = up

    def health(self, **_):
        return SiteResult(available=self.up, reason="" if self.up else "ConnectionRefusedError")

    def context_pack(self, pack, **_):
        return SiteResult(available=True, data=DATA, text="x", corpus_hash="hash123")

    def verify_links(self, draft, **_):
        return SiteResult(available=False, reason="disabled")


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
def seen(monkeypatch):
    """Every visualizer call: ('event', root, step, status, detail) and ('run', root, step, state)."""
    rec = []
    monkeypatch.setattr(pipeline, "_track_event_only", lambda url, tid, step, status, **kw: rec.append(("event", tid, step, status, kw.get("detail"))))
    monkeypatch.setattr(pipeline, "_track_run", lambda url, tid, **kw: rec.append(("run", tid, kw["step"], kw["state"])))
    monkeypatch.setattr(pipeline, "create_review", lambda config, **kw: URL)
    monkeypatch.setattr(pipeline, "update_review", lambda config, **kw: None)
    return rec


@pytest.fixture
def rec_shadow(tmp_path):
    return ShadowRecorder(ShadowConfig(lint=True, research=True, background_intake=False), tmp_path / "blog_shadow.jsonl", FakeSite())


def _intake(conn, ops, store, *, up=True, handoff=True, shadow_rec=None, sid="sub-e-1"):
    pl = research.ResearchPlanner(research.ResearchConfig(enabled=True, handoff=handoff), client_factory=lambda _t: FakeSite(up))
    root = pipeline.handle_intake(conn, ops, store, submission_id=sid, event_pack={**PACK, "submissionId": sid}, split=True, research=pl, shadow=shadow_rec)
    tasks = {t.idempotency_key: t for t in ops.list_tasks(conn, tenant=pipeline.TENANT)}
    return root, (tasks.get(f"{sid}-research").id if f"{sid}-research" in tasks else None), tasks[f"{root}-blog-r1"].id


def _steps(seen, root=None):
    return [(e[2], e[3]) for e in seen if e[0] == "event" and (root is None or e[1] == root)]


def _complete(conn, tid, metadata, claimer="x"):
    if kb.get_task(conn, tid).status == "ready":
        kb.claim_task(conn, tid, claimer=claimer)
    assert kb.complete_task(conn, tid, summary="research brief ready", metadata=metadata)


@pytest.fixture
def cfg_patch(monkeypatch, store):
    extra = {"store_path": str(store.path)}
    monkeypatch.setattr(hooks, "_load_pipeline_extra", lambda: extra)
    monkeypatch.setattr(root_config, "load_pipeline_extra", lambda: extra)
    monkeypatch.setattr(hooks.db, "resolve_database_url", lambda e=None: "postgres://x")
    monkeypatch.setattr(hooks, "_background", lambda fn: fn())
    return extra


def _run_hook(task_id):
    hooks.on_kanban_task_completed(task_id=task_id)


# --- event sequences ----------------------------------------------------------------------------

def test_sequence_done_handed_to_blog(conn, ops, store, seen, cfg_patch):
    root, r, b = _intake(conn, ops, store)
    assert [e[2] for e in seen] == ["intake_received", "stylus_drafting", "research_queued"]
    hooks.on_kanban_task_claimed(task_id=r)
    _complete(conn, r, GOOD)
    _run_hook(r)
    hooks.on_kanban_task_claimed(task_id=b)
    steps = _steps(seen)
    assert steps == [("research_queued", "info"), ("research_running", "info"), ("research_done", "ok"), ("blog_drafting", "info")]
    done = next(e for e in seen if e[2] == "research_done")
    assert "handed to blog" in done[4] and "An earlier retreat" in done[4] and "2 links" in done[4] and "1 caution" in done[4] and len(done[4]) <= 500
    assert all(e[1] == root for e in seen)                      # always under the root S id


def test_sequence_done_shadow_only_when_handoff_off(conn, ops, store, seen, cfg_patch):
    root, r, b = _intake(conn, ops, store, handoff=False)
    _complete(conn, r, GOOD)
    _run_hook(r)
    done = next(e for e in seen if e[2] == "research_done")
    assert "shadow only" in done[4]


def test_sequence_skipped_when_service_down(conn, ops, store, seen):
    root, r, b = _intake(conn, ops, store, up=False)
    assert r is None
    assert _steps(seen)[-1] == ("research_skipped", "info") and "service down" in [e for e in seen if e[2] == "research_skipped"][0][4]
    assert "research_queued" not in [s for s, _ in _steps(seen)]


def test_sequence_nothing_useful_is_done_not_failed(conn, ops, store, seen, cfg_patch):
    root, r, b = _intake(conn, ops, store)
    _complete(conn, r, NOTHING)
    _run_hook(r)
    e = next(e for e in seen if e[2] == "research_done")
    assert "nothing useful" in e[4] and e[3] == "ok"


def test_sequence_invalid_is_failed(conn, ops, store, seen, cfg_patch):
    root, r, b = _intake(conn, ops, store)
    _complete(conn, r, None)
    _run_hook(r)
    assert _steps(seen)[-1] == ("research_failed", "error")
    assert "invalid" in [e for e in seen if e[2] == "research_failed"][0][4]


@pytest.mark.parametrize("how,step", [("blocked", "research_failed"), ("timeout", "research_timeout"), ("never", "research_failed")])
def test_sequence_sweeper_events(how, step, conn, ops, store, seen, cfg_patch):
    root, r, b = _intake(conn, ops, store)
    if how == "blocked":
        kb.claim_task(conn, r, claimer="w")
        kb.block_task(conn, r, reason="x")
    else:
        if how == "timeout":
            kb.claim_task(conn, r, claimer="w")
        conn.execute("UPDATE tasks SET created_at = ? WHERE id = ?", (int(time.time()) - 500, r))
        conn.commit()
    hooks.on_kanban_dispatch_tick()
    evs = [e for e in seen if e[0] == "event" and e[2] in ("research_failed", "research_timeout")]
    assert [(e[2], e[3]) for e in evs] == [(step, "error")] and "blog continues without it" in evs[0][4]


def test_research_never_sets_state_blocked_or_touches_runs_after_intake(conn, ops, store, seen, cfg_patch):
    root, r, b = _intake(conn, ops, store)
    n_runs_at_intake = len([e for e in seen if e[0] == "run"])
    hooks.on_kanban_task_claimed(task_id=r)
    kb.claim_task(conn, r, claimer="w")
    kb.block_task(conn, r, reason="x")
    hooks.on_kanban_dispatch_tick()
    _complete(conn, b, None)
    runs = [e for e in seen if e[0] == "run"]
    assert len(runs) == n_runs_at_intake and all(e[3] == "running" for e in runs)
    assert not any(e[0] == "run" and e[3] == "blocked" for e in seen)


# --- shadow records -----------------------------------------------------------------------------

def test_flags_all_off_recorder_is_none_and_research_alone_enables_it(tmp_path):
    assert ShadowRecorder.from_extra({}, tmp_path / "s.json") is None
    assert ShadowRecorder.from_extra({"blog_enrichment": {"split": True, "research": {"handoff": True}}}, tmp_path / "s.json") is None
    for bad in ("true", 1, None):
        assert ShadowRecorder.from_extra({"blog_enrichment": {"research": {"enabled": bad}}}, tmp_path / "s.json") is None
    rec = ShadowRecorder.from_extra({"blog_enrichment": {"research": {"enabled": True}}}, tmp_path / "s.json")
    assert rec is not None and rec.config.research and not rec.config.site_context and not rec.config.lint
    both = ShadowConfig.from_extra({"blog_enrichment": {"research": {"enabled": True}, "shadow": {"lint": True}}})
    assert both.research and both.lint and not both.site_context


def test_research_only_recorder_does_not_record_intake_or_completion(tmp_path):
    rec = ShadowRecorder.from_extra({"blog_enrichment": {"research": {"enabled": True}}}, tmp_path / "s.json", FakeSite())
    rec.record_intake(submission_id="s", task_id="t", event_pack=PACK)
    rec.record_completion(task_id="t", platform="blog", round_no=1, blog=BLOG, pack={})
    rec.record_review_action(task_id="t", platform="blog", action="approved", comment="", final_text="x", edited=False)
    assert [r["kind"] for r in shadow.read_records(rec.log_path)] == ["review_action"]


def test_recording_a_full_done_run(conn, ops, store, seen, cfg_patch, rec_shadow, monkeypatch):
    monkeypatch.setattr(hooks, "_shadow_recorder", lambda extra, st: rec_shadow)
    root, r, b = _intake(conn, ops, store, shadow_rec=rec_shadow)
    _complete(conn, r, GOOD)
    _run_hook(r)
    recs = shadow.read_records(rec_shadow.log_path)
    start = next(x for x in recs if x["kind"] == "research")
    assert start["task_id"] == root and start["research_task_id"] == r and start["service_ok"] is True and start["handoff"] is True
    assert start["corpus_hash"] == "hash123" and start["allowed_urls_n"] == 2
    res = next(x for x in recs if x["kind"] == "research_result")
    assert (res["outcome"], res["delivered"], res["task_id"], res["research_task_id"]) == ("done", True, root, r)
    assert res["items"] == {"related_posts": 1, "suggested_links": 1, "teaching_background": 0, "cautions": 1}
    assert len(res["brief_sha"]) == 8 and res["web_wanted"] is True and res["delivered_before_claim"] is True and res["guard_rejections"] == 0
    assert res["dropped"] == [] and res["duration_s"] is not None


def test_recording_sweeper_and_skipped_results(conn, ops, store, seen, cfg_patch, rec_shadow, monkeypatch):
    monkeypatch.setattr(hooks, "_shadow_recorder", lambda extra, st: rec_shadow)
    _intake(conn, ops, store, up=False, shadow_rec=rec_shadow, sid="sub-skip")
    root, r, b = _intake(conn, ops, store, shadow_rec=rec_shadow, sid="sub-to")
    kb.claim_task(conn, r, claimer="w")
    kb.block_task(conn, r, reason="x")
    hooks.on_kanban_dispatch_tick()
    results = [x for x in shadow.read_records(rec_shadow.log_path) if x["kind"] == "research_result"]
    assert [(x["outcome"], x.get("reason")) for x in results] == [("skipped", "service down: ConnectionRefusedError"), ("blocked", "blocked")]
    starts = [x for x in shadow.read_records(rec_shadow.log_path) if x["kind"] == "research"]
    assert starts[0]["research_task_id"] is None and starts[0]["service_ok"] is False


def test_dropped_items_are_recorded_by_section_and_reason_only(conn, ops, store, seen, cfg_patch, rec_shadow, monkeypatch):
    monkeypatch.setattr(hooks, "_shadow_recorder", lambda extra, st: rec_shadow)
    root, r, b = _intake(conn, ops, store, shadow_rec=rec_shadow)
    bad = {"schema": 1, "research_brief": {**GOOD["research_brief"], "suggested_links": [{"url": "https://evil.example/q", "anchor_hint": "x"}]}}
    _complete(conn, r, bad)
    _run_hook(r)
    res = [x for x in shadow.read_records(rec_shadow.log_path) if x["kind"] == "research_result"][-1]
    assert res["dropped"] and res["dropped"][0]["reason"] == "url_not_allowed" and "evil.example" not in json.dumps(res)


def test_completion_record_carries_brief_info(conn, ops, store, seen, cfg_patch, rec_shadow, monkeypatch):
    monkeypatch.setattr(hooks, "_shadow_recorder", lambda extra, st: rec_shadow)
    root, r, b = _intake(conn, ops, store, shadow_rec=rec_shadow)
    _complete(conn, r, GOOD)
    _run_hook(r)
    s = ops.get_task(conn, root)
    _complete(conn, root, {"event_title": "T", "fb": "f", "ig": "i"})
    cfg = ReviewClientConfig(base_url="https://bmcposts.example", create_secret="x")
    pipeline.handle_stylus_completion(conn, ops, EventPostPipelineStore(store.path), cfg, root, shadow=rec_shadow, now=1.0)
    _complete(conn, b, {"event_title": "T", "blog": BLOG})
    pipeline.handle_stylus_completion(conn, ops, EventPostPipelineStore(store.path), cfg, b, shadow=rec_shadow, now=2.0)
    comp = [x for x in shadow.read_records(rec_shadow.log_path) if x["kind"] == "completion"]
    assert len(comp) == 1 and comp[0]["task_id"] == root
    assert comp[0]["brief"]["delivered"] is True and len(comp[0]["brief"]["sha"]) == 8
    assert comp[0]["brief"]["links_offered"] >= 2 and comp[0]["brief"]["links_used"] == 1      # the draft used P1 only


def test_completion_record_has_no_brief_key_without_research(conn, ops, store, seen, rec_shadow):
    root = pipeline.handle_intake(conn, ops, store, submission_id="plain", event_pack={**PACK, "submissionId": "plain"}, shadow=rec_shadow)
    _complete(conn, root, {"event_title": "T", "fb": "f", "ig": "i", "blog": BLOG})
    cfg = ReviewClientConfig(base_url="https://bmcposts.example", create_secret="x")
    pipeline.handle_stylus_completion(conn, ops, store, cfg, root, shadow=rec_shadow, now=1.0)
    comp = [x for x in shadow.read_records(rec_shadow.log_path) if x["kind"] == "completion"]
    assert len(comp) == 1 and "brief" not in comp[0]


def test_research_records_are_bounded_and_control_free(tmp_path):
    rec = ShadowRecorder(ShadowConfig(research=True), tmp_path / "s.jsonl")
    hostile = "x\x00y\x1b[31m " + "z" * 5000
    rec.record_research_start(submission_id=hostile, task_id=hostile, research_task_id=hostile, service_ok=True, corpus_hash=hostile,
                              allowed_urls_n=3, handoff=True)
    rec.record_research_result(task_id=hostile, research_task_id=hostile, outcome=hostile, reason=hostile, duration_s=1.23456,
                               items={"a" * 100: 5}, dropped=[{"section": hostile, "reason": hostile}] * 500, brief_sha=hostile)
    raw = (tmp_path / "s.jsonl").read_bytes()
    assert all(len(line) <= shadow.MAX_LINE_BYTES for line in raw.splitlines())
    text = raw.decode()
    assert "\x00" not in text and "\x1b" not in text and " " not in text
    recs = shadow.read_records(tmp_path / "s.jsonl")
    assert [r["kind"] for r in recs] == ["research", "research_result"] and recs[1]["duration_s"] == 1.2


def test_recorder_methods_never_raise_and_noop_when_flag_off(tmp_path):
    off = ShadowRecorder(ShadowConfig(lint=True), tmp_path / "s.jsonl")
    off.record_research_start(submission_id="s", task_id="t", research_task_id="r", service_ok=True, corpus_hash="", allowed_urls_n=0, handoff=False)
    off.record_research_result(task_id="t", research_task_id="r", outcome="done")
    assert not (tmp_path / "s.jsonl").exists()
    on = ShadowRecorder(ShadowConfig(research=True), tmp_path / "ro" / "x" / "s.jsonl")
    (tmp_path / "ro").write_text("a file where a directory is needed")
    on.record_research_result(task_id="t", research_task_id="r", outcome="done")        # unwritable: swallowed


# --- the report -----------------------------------------------------------------------------------

def _write(path, records):
    with open(path, "w") as fh:
        for r in records:
            fh.write(json.dumps({"schema": 1, "ts": "2026-10-11T00:00:00Z", **r}) + "\n")


def test_report_prints_research_lines_and_before_after_summary(tmp_path, capsys):
    log = tmp_path / "l.jsonl"
    comp = lambda tid, words, brief: {"kind": "completion", "task_id": tid, "platform": "blog", "round": 1, "body_words": words, "draft_links": [],  # noqa: E731
                                      "lint": {"ok": True, "hard": 0, "warn": 1}, "findings": [], **({"brief": brief} if brief else {})}
    _write(log, [
        {"kind": "research", "task_id": "t_a", "submission_id": "a", "research_task_id": "t_r1", "service_ok": True, "corpus_hash": "h", "allowed_urls_n": 4, "handoff": True, "latency_ms": 120},
        {"kind": "research_result", "task_id": "t_a", "research_task_id": "t_r1", "outcome": "done", "duration_s": 74.0, "queue_s": 12.0, "run_s": 62.0,
         "items": {"suggested_links": 2}, "dropped": [{"section": "suggested_links[1].url", "reason": "url_not_allowed"}], "brief_sha": "abcd1234",
         "delivered": True, "delivered_before_claim": True, "guard_rejections": 1, "web_wanted": False},
        comp("t_a", 450, {"delivered": True, "sha": "abcd1234", "links_offered": 3, "links_used": 2}),
        {"kind": "review_action", "task_id": "t_a", "action": "approved", "final_links": [], "text_is_manual_edit": False},
        {"kind": "research_result", "task_id": "t_b", "outcome": "skipped", "reason": "service down: timeout"},
        comp("t_b", 300, None),
    ])
    assert shadow_report.main(["--log", str(log)]) == 0
    out = capsys.readouterr().out
    assert "research: task t_r1 service_ok=True handoff=True allowed_urls=4" in out
    assert "result: done; took 74 s (queued 12 s, ran 62 s)" in out and "suggested_links=2" in out and "brief abcd1234 delivered=True before_claim=True" in out
    assert "dropped suggested_links[1].url: url_not_allowed" in out
    assert "result: skipped (service down: timeout)" in out
    assert "brief: delivered=True sha abcd1234; links used from the brief 2/3" in out
    assert "RESEARCH BRIEF (before/after" in out and "research outcomes: done=1, skipped=1" in out
    assert "with brief: 1 submission(s); median blog words 450; lint hard 0, warn 1; links used from the brief 2/3; reviewer: approved 1" in out
    assert "without brief: 1 submission(s); median blog words 300" in out
    assert shadow_report.main(["--log", str(log), "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["t_a"]["research"]["result"]["outcome"] == "done"


def test_report_without_research_records_is_unchanged(tmp_path, capsys):
    log = tmp_path / "l.jsonl"
    _write(log, [{"kind": "completion", "task_id": "t_a", "platform": "blog", "round": 1, "body_words": 10, "draft_links": [], "lint": {"ok": True, "hard": 0, "warn": 0}}])
    assert shadow_report.main(["--log", str(log)]) == 0
    out = capsys.readouterr().out
    assert "SUMMARY: 1 submission(s)" in out and "RESEARCH" not in out and "research:" not in out
