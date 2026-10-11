"""Research step fail-open sweeper + dispatcher-tick / task-claimed hooks (WP6). Real throwaway Kanban board."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest
from plugins.platforms.event_post_pipeline import hooks, pipeline, research, root_config
from plugins.platforms.event_post_pipeline.site_client import SiteResult
from plugins.platforms.event_post_pipeline.store import EventPostPipelineStore

PACK = {
    "submissionId": "sub-w-1", "date": "2026-09-20", "description": "A day-long meditation retreat.", "quote": "Peace.",
    "first": {"finalUrl": "https://res.cloudinary.com/demo/first.jpg"}, "last": {"finalUrl": "https://res.cloudinary.com/demo/last.jpg"},
}
DATA = {"candidates": [{"url": "https://bmc.example/programs/meditate", "title": "Meditate", "kind": "program", "tier": "strong"}]}


class FakeClient:
    def health(self, **_):
        return SiteResult(available=True)

    def context_pack(self, pack, **_):
        return SiteResult(available=True, data=DATA, text="x", corpus_hash="h")


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
def events(monkeypatch):
    rec = []
    monkeypatch.setattr(pipeline, "_track_event_only", lambda url, tid, step, status, **kw: rec.append((tid, step, status, kw.get("detail"))))
    monkeypatch.setattr(pipeline, "_track_run", lambda url, tid, **kw: rec.append((tid, kw["step"], kw["status"], kw.get("detail"))))
    return rec


def _setup(conn, ops, store, *, handoff=True, sid="sub-w-1"):
    cfg = research.ResearchConfig(enabled=True, handoff=handoff)
    pl = research.ResearchPlanner(cfg, client_factory=lambda _t: FakeClient())
    root = pipeline.handle_intake(conn, ops, store, submission_id=sid, event_pack={**PACK, "submissionId": sid}, split=True, research=pl)
    tasks = {t.idempotency_key: t for t in ops.list_tasks(conn, tenant=pipeline.TENANT)}
    return root, tasks[f"{sid}-research"].id, tasks[f"{root}-blog-r1"].id


def _age(conn, task_id, seconds):
    conn.execute("UPDATE tasks SET created_at = ? WHERE id = ?", (int(time.time()) - seconds, task_id))
    conn.commit()


def _sweep(conn, ops, store, **kw):
    return research.sweep(conn, ops, store, now=time.time(), tenant=pipeline.TENANT, author=pipeline.CREATED_BY, **kw)


def _st(conn, tid):
    return kb.get_task(conn, tid).status


# --- sweeper ----------------------------------------------------------------------------------------

def test_fresh_research_is_left_alone(conn, ops, store, events):
    _, r, b = _setup(conn, ops, store)
    assert _sweep(conn, ops, store) == []
    assert _st(conn, r) == "ready" and _st(conn, b) == "todo" and kb.parent_ids(conn, b) == [r]


def test_blocked_research_is_unlinked_archived_and_blog_starts(conn, ops, store, events):
    root, r, b = _setup(conn, ops, store)
    kb.claim_task(conn, r, claimer="webmaster")
    assert kb.block_task(conn, r, reason="needs a human")
    out = _sweep(conn, ops, store)
    assert [(x["task_id"], x["reason"], x["event"]) for x in out] == [(r, "blocked", "research_failed")]
    assert _st(conn, r) == "archived" and kb.parent_ids(conn, b) == []
    assert _st(conn, b) == "ready"                                   # B starts without a brief
    assert kb.claim_task(conn, b, claimer="stylus") is not None
    assert any("continuing without a brief" in c.body for c in ops.list_comments(conn, r))
    assert (root, "research_failed", "error", "no brief (blocked); blog continues without it") in events
    assert store.get_submission("sub-w-1")["research"]["outcome"] == "blocked"


def test_overdue_running_is_a_timeout(conn, ops, store, events):
    root, r, b = _setup(conn, ops, store)
    kb.claim_task(conn, r, claimer="webmaster")
    _age(conn, r, 400)
    out = _sweep(conn, ops, store)
    assert out[0]["event"] == "research_timeout" and _st(conn, r) == "archived" and _st(conn, b) == "ready"
    assert (root, "research_timeout", "error", "no brief (timeout); blog continues without it") in events


def test_never_spawned_overdue_is_failed(conn, ops, store, events):
    root, r, b = _setup(conn, ops, store)
    _age(conn, r, 400)                                               # still ready: the dispatcher could not spawn it
    out = _sweep(conn, ops, store)
    assert out[0]["event"] == "research_failed" and out[0]["reason"] == "never started"
    assert _st(conn, b) == "ready" and kb.parent_ids(conn, b) == []


def test_deadline_comes_from_the_store_record_then_config_default(conn, ops, store, events):
    _, r, b = _setup(conn, ops, store)
    _age(conn, r, 100)
    assert _sweep(conn, ops, store) == []                            # deadline 360
    assert len(_sweep(conn, ops, store, default_deadline_s=60)) == 0  # store record (360) wins over the default
    rec = store.get_submission("sub-w-1")["research"]
    store.upsert_submission("sub-w-1", {"research": {**rec, "deadline_s": 50}})
    assert len(_sweep(conn, ops, store)) == 1


def test_sweep_is_idempotent(conn, ops, store, events):
    _, r, _ = _setup(conn, ops, store)
    _age(conn, r, 400)
    assert len(_sweep(conn, ops, store)) == 1
    n_events = len(events)
    assert _sweep(conn, ops, store) == [] and len(events) == n_events


def test_done_research_is_never_touched(conn, ops, store, events):
    _, r, b = _setup(conn, ops, store)
    kb.claim_task(conn, r, claimer="webmaster")
    kb.complete_task(conn, r, summary="research brief ready", metadata=None)
    _age(conn, r, 4000)
    assert _sweep(conn, ops, store) == [] and _st(conn, r) == "done"


def test_unlinked_research_is_retired_without_touching_b(conn, ops, store, events):
    _, r, b = _setup(conn, ops, store, handoff=False)
    assert _st(conn, b) == "ready"
    kb.claim_task(conn, r, claimer="webmaster")
    kb.block_task(conn, r, reason="x")
    out = _sweep(conn, ops, store)
    assert out[0]["unlinked"] == [] and _st(conn, r) == "archived" and _st(conn, b) == "ready"


def test_sweep_never_touches_other_tenants_or_non_research_tasks(conn, ops, store, events):
    root, r, b = _setup(conn, ops, store)
    other_tenant = kb.create_task(conn, title="Research: other", assignee="webmaster", idempotency_key="zzz-research", tenant="someone-else")
    plain = kb.create_task(conn, title="Some webmaster task", assignee="webmaster", idempotency_key="not-a-research-key", tenant=pipeline.TENANT)
    for tid in (other_tenant, plain, root):
        _age(conn, tid, 99999)
    kb.claim_task(conn, other_tenant, claimer="webmaster")
    kb.block_task(conn, other_tenant, reason="x")
    assert _sweep(conn, ops, store) == []
    assert _st(conn, other_tenant) == "blocked" and _st(conn, plain) == "ready" and _st(conn, root) == "ready"


def test_one_bad_task_does_not_stop_the_sweep(conn, ops, store, events):
    _, r1, _ = _setup(conn, ops, store, sid="sub-w-1")
    _, r2, b2 = _setup(conn, ops, store, sid="sub-w-2")
    _age(conn, r1, 400)
    _age(conn, r2, 400)

    class Flaky(pipeline.KanbanOps):
        def archive_task(self, c, task_id):
            if task_id == r1:
                raise RuntimeError("boom")
            return super().archive_task(c, task_id)

    out = research.sweep(conn, Flaky(kb), store, now=time.time(), tenant=pipeline.TENANT, author=pipeline.CREATED_BY)
    assert [x["task_id"] for x in out] == [r2] and _st(conn, b2) == "ready"


# --- hooks ------------------------------------------------------------------------------------------

@pytest.fixture
def cfg_patch(monkeypatch, store):
    extra = {"store_path": str(store.path), "board": None}      # NOTE: no blog_enrichment block at all (all flags off)
    monkeypatch.setattr(hooks, "_load_pipeline_extra", lambda: extra)
    monkeypatch.setattr(root_config, "load_pipeline_extra", lambda: extra)
    monkeypatch.setattr(hooks.db, "resolve_database_url", lambda e: "postgres://x")
    return extra


def test_tick_hook_sweeps_even_with_every_flag_off(conn, ops, store, events, cfg_patch):
    _, r, b = _setup(conn, ops, store)
    _age(conn, r, 400)
    hooks.on_kanban_dispatch_tick(board=None, profile_name="default", dry_run=False, outcome="ok", result=object())
    assert _st(conn, r) == "archived" and _st(conn, b) == "ready"


def test_tick_hook_runs_join_sweep_too_and_skips_dry_run(monkeypatch):
    calls = []
    monkeypatch.setattr(hooks, "run_research_sweep", lambda: calls.append("research"))
    monkeypatch.setattr(hooks, "run_join_sweep", lambda: calls.append("join"))
    hooks.on_kanban_dispatch_tick(dry_run=True)
    assert calls == []
    hooks.on_kanban_dispatch_tick(outcome="idle", extra_unknown_kwarg=1)
    assert calls == ["research", "join"]


def test_tick_hook_never_raises(monkeypatch):
    monkeypatch.setattr(hooks, "_load_pipeline_extra", lambda: (_ for _ in ()).throw(RuntimeError("config broke")))
    hooks.on_kanban_dispatch_tick()
    assert hooks.run_research_sweep() == []


def test_claim_hook_emits_events_only_for_this_tenant(conn, ops, store, events, cfg_patch, monkeypatch):
    monkeypatch.setattr(hooks, "_background", lambda fn: fn())
    root, r, b = _setup(conn, ops, store)
    other = kb.create_task(conn, title="Research: other", assignee="webmaster", idempotency_key="zzz-research", tenant="someone-else")
    for tid in (r, b, root, other):
        hooks.on_kanban_task_claimed(task_id=tid, assignee="x", board=None, run_id=1, profile_name="default")
    steps = [(tid, step) for tid, step, *_ in events if step in ("research_running", "blog_drafting")]
    assert steps == [(root, "research_running"), (root, "blog_drafting")]
    assert (root, "research_running", "info", "started") in events


def test_claim_hook_returns_at_once_and_never_raises(monkeypatch):
    started = []
    monkeypatch.setattr(hooks, "_background", lambda fn: started.append(fn))
    monkeypatch.setattr(hooks, "_load_pipeline_extra", lambda: {"x": 1})
    monkeypatch.setattr(hooks.db, "resolve_database_url", lambda e: "postgres://x")
    hooks.on_kanban_task_claimed(task_id="t_1")
    assert len(started) == 1                                         # nothing ran inline: the dispatch lock is held
    monkeypatch.setattr(hooks, "_load_pipeline_extra", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    hooks.on_kanban_task_claimed(task_id="t_1")


def test_register_adds_tick_and_claim_hooks():
    from plugins.platforms.event_post_pipeline import register
    mgr = PluginManager()
    register(PluginContext(PluginManifest(name="event_post_pipeline"), mgr))
    assert hooks.on_kanban_dispatch_tick in mgr._hooks.get("on_kanban_dispatch_tick", [])
    assert hooks.on_kanban_task_claimed in mgr._hooks.get("kanban_task_claimed", [])


def test_both_hook_names_are_real_core_hooks():
    from hermes_cli.plugins import VALID_HOOKS
    assert "on_kanban_dispatch_tick" in VALID_HOOKS and "kanban_task_claimed" in VALID_HOOKS
