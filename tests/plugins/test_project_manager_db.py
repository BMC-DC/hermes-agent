"""Captain integration tests against a real Postgres (PM_PLAN §10).

Run against a THROWAWAY database only:
    PM_TEST_ADMIN_DSN=postgresql://postgres:...@127.0.0.1:55432/spp \
    PM_TEST_APP_DSN=postgresql://spp_app:...@127.0.0.1:55432/spp  pytest tests/plugins/test_project_manager_db.py
The admin DSN drops and recreates schema ``pm`` (migration applied twice);
every test then runs as ``spp_app`` so the migration's grants are exercised.
ClickUp and WhatsApp are fakes: no live writes, no real messages.
Skipped when the env vars are absent.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import yaml

ADMIN_DSN = os.environ.get("PM_TEST_ADMIN_DSN")
APP_DSN = os.environ.get("PM_TEST_APP_DSN")
pytestmark = pytest.mark.skipif(not (ADMIN_DSN and APP_DSN), reason="PM_TEST_ADMIN_DSN / PM_TEST_APP_DSN not set")

psycopg2 = pytest.importorskip("psycopg2")

from plugins.project_manager import config as pm_config  # noqa: E402
from plugins.project_manager import messaging, service, store  # noqa: E402
from plugins.project_manager.clickup import ClickUpError  # noqa: E402

NY = ZoneInfo("America/New_York")
FIXTURES = Path(__file__).parent / "fixtures" / "project_manager"
MIGRATION = Path(os.environ.get("PM_MIGRATION_SQL", "/home/bmc/VIDU/extra/plans/pm/migrations/0001_pm_schema.sql"))
GROUP = "120363000000000000@g.us"
KUSAL, YOHAN, DILEEPA, ADMIN = "270803640", "204258998", "94495142", "210143482"
KUSAL_NO, YOHAN_NO = "15550000001", "15550000002"      # fake numbers
LIST_SEO = "901417261752"


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(scope="module", autouse=True)
def schema():
    if not MIGRATION.exists():
        pytest.skip(f"migration not found at {MIGRATION}")
    conn = psycopg2.connect(ADMIN_DSN)
    conn.autocommit = True
    migrations = sorted(MIGRATION.parent.glob("[0-9][0-9][0-9][0-9]_*.sql"))   # 0001, 0002, ...
    with conn.cursor() as cur:
        cur.execute("DROP SCHEMA IF EXISTS pm CASCADE")
        for _ in range(2):           # idempotent: second run must be a no-op
            for path in migrations:
                cur.execute(path.read_text())
    conn.close()


@pytest.fixture
def conn():
    admin = psycopg2.connect(ADMIN_DSN)
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute("TRUNCATE pm.audit, pm.message_log, pm.approvals, pm.reports, pm.meeting_attendees, "
                    "pm.meetings, pm.people, pm.clickup_scope, pm.meeting_topics RESTART IDENTITY CASCADE")
    admin.close()
    c = store.connect(APP_DSN)
    for name, uid, number, tz in (("Kusal Darshana", KUSAL, KUSAL_NO, "Asia/Colombo"),
                                  ("Yohan Pathiranage", YOHAN, YOHAN_NO, "America/Vancouver"),
                                  ("Dileepa", DILEEPA, None, "America/New_York")):
        with c, c.cursor() as cur:
            cur.execute("INSERT INTO pm.people (display_name, clickup_user_id, whatsapp_number, timezone, updated_by) "
                        "VALUES (%s, %s, %s, %s, 'test')", (name, uid, number, tz))
    yield c
    c.close()


@pytest.fixture
def cfg(monkeypatch):
    raw = yaml.safe_load((FIXTURES / "pm.yaml").read_text())
    raw["whatsapp"]["mode"] = "production"
    raw["clickup"]["tags"]["meeting_topics"] = {"ai": ["ai", "vidu"], "web": ["website"]}
    monkeypatch.setattr(messaging, "management_group_chat_id", lambda root_config=None: GROUP)
    return pm_config.parse_config(raw, Path("/tmp/pm.yaml"))


class FakeClickUp:
    def __init__(self, tasks=None, fail_with=None):
        self.tasks = tasks or []
        self.fail_with = fail_with
        self.writes = []

    def get_team_tasks(self, team_id):
        return self.tasks

    def get_task(self, task_id, include_subtasks=False):
        self.subtask_reads = getattr(self, "subtask_reads", 0) + bool(include_subtasks)
        if task_id == "OUTSIDE":
            return {"id": task_id, "name": "Elsewhere", "list": {"id": "999"}}
        task = {"id": task_id, "name": f"Task {task_id}", "list": {"id": LIST_SEO},
                "status": {"status": "to do", "type": "open"}, "assignees": [{"id": 94495142, "username": "Dileepa"}]}
        if include_subtasks and task_id == "PARENT":
            task["subtasks"] = [{"id": "s1"}, {"id": "s2"}]
        return task

    def get_list(self, list_id):
        return {"statuses": [{"status": "to do", "type": "open"}, {"status": "in progress", "type": "custom"},
                             {"status": "complete", "type": "closed"}]}

    def _write(self, kind, *args):
        self.writes.append((kind, *args))
        if self.fail_with:
            raise self.fail_with
        return {"id": "new1", "url": "https://app.clickup.com/t/new1", "name": "x"}

    def create_task(self, list_id, body):
        return self._write("create", list_id, body)

    def update_task(self, task_id, body):
        return self._write("update", task_id, body)

    def create_comment(self, task_id, text):
        return self._write("comment", task_id, text)

    def delete_task(self, task_id):
        self._write("delete", task_id)
        return None


class FakeSender:
    def __init__(self, fail_times=0):
        self.sent = []
        self.fail_times = fail_times

    async def __call__(self, chat_id, body):
        if self.fail_times:
            self.fail_times -= 1
            raise RuntimeError("bridge down")
        self.sent.append((chat_id, body))


def aliases(x):
    return {x.split("@")[0].lstrip("+")}


def audit(conn, entity):
    with conn, conn.cursor() as cur:
        cur.execute("SELECT entity_id, action, actor FROM pm.audit WHERE entity = %s ORDER BY id", (entity,))
        return [tuple(r.values()) for r in cur.fetchall()]


NOW = datetime(2026, 10, 14, 16, 0, tzinfo=timezone.utc)


# ── migration / grants ───────────────────────────────────────────────────────
def test_app_role_cannot_rewrite_the_audit_trail(conn):
    with conn, conn.cursor() as cur:
        cur.execute("INSERT INTO pm.audit (entity, entity_id, action, actor) VALUES ('person', '1', 'x', 't')")
    with pytest.raises(psycopg2.errors.InsufficientPrivilege):
        with conn, conn.cursor() as cur:
            cur.execute("UPDATE pm.audit SET actor = 'evil'")
    with pytest.raises(psycopg2.errors.InsufficientPrivilege):
        with conn, conn.cursor() as cur:
            cur.execute("DELETE FROM pm.audit")


def test_people_constraints(conn):
    with pytest.raises(psycopg2.errors.CheckViolation):
        with conn, conn.cursor() as cur:
            cur.execute("INSERT INTO pm.people (display_name, whatsapp_number, timezone, updated_by) "
                        "VALUES ('x', '+1 555', 'America/New_York', 't')")


def test_sync_scope_is_idempotent_and_audited(conn):
    entries = [("workspace", "1", "W", None), ("list", "2", "L", "1")]
    assert store.sync_scope(conn, entries, "t") == {"changed": 2, "removed": 0, "total": 2}
    assert store.sync_scope(conn, entries, "t")["changed"] == 0
    assert store.sync_scope(conn, entries[:1], "t")["removed"] == 1
    assert [a[1] for a in audit(conn, "clickup_scope")] == ["upsert", "upsert", "delete"]


def test_seed_people_is_rerunnable_including_people_outside_clickup(conn):
    args = dict(timezone="America/Denver", country=None, role=None, actor="seed")
    assert store.insert_person_if_absent(conn, display_name="Udara", clickup_user_id=None, **args)
    assert store.insert_person_if_absent(conn, display_name="udara", clickup_user_id=None, **args) is None
    assert store.insert_person_if_absent(conn, display_name="Someone", clickup_user_id=YOHAN, **args) is None
    with conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM pm.people WHERE lower(display_name) = 'udara'")
        assert cur.fetchone()["n"] == 1


def test_sync_topics(conn):
    assert store.sync_topics(conn, {"ai": ["vidu", "ai"]}, "t")["changed"] == 1
    assert store.sync_topics(conn, {"ai": ["ai", "vidu"]}, "t")["changed"] == 0
    assert store.sync_topics(conn, {}, "t")["removed"] == 1
    assert [a[1] for a in audit(conn, "meeting_topic")] == ["upsert", "delete"]


# ── approval flow end to end ─────────────────────────────────────────────────
def propose(cfg, conn, client, sender, payload=None, kind="clickup_create_task", requested_by="Kusal (whatsapp:1)"):
    payload = payload or {"list_id": LIST_SEO, "name": "Write the SEO brief",
                          "assignee_clickup_user_ids": [DILEEPA], "due_date": "2026-10-20"}
    return run(service.propose_change(cfg, conn, client, kind=kind, payload=payload, reason="asked in chat",
                                      requested_by=requested_by, now=NOW, sender=sender))


def test_approval_happy_path_executes_exactly_once(cfg, conn):
    client, sender = FakeClickUp(), FakeSender()
    result = propose(cfg, conn, client, sender)
    aid = result["approval_id"]
    assert result["status"] == "pending"
    assert result["summary"] == ('Create task "Write the SEO brief" in list "SEO / Tasks", assigned to Dileepa, '
                                 'due Tue 20 Oct 2026.')
    assert sender.sent[0][0] == f"{YOHAN_NO}@s.whatsapp.net"
    assert f"YES {aid}" in sender.sent[0][1]

    stranger = service.handle_reply(cfg, conn, sender_aliases={"19999999999"}, text=f"yes {aid}", aliases_of=aliases)
    assert not stranger.handled
    assert store.get_approval(conn, aid)["status"] == "pending"

    out = service.handle_reply(cfg, conn, sender_aliases={YOHAN_NO}, text=f"YES {aid}", aliases_of=aliases)
    assert out.handled and out.decision == "approved"
    again = service.handle_reply(cfg, conn, sender_aliases={YOHAN_NO}, text=f"yes {aid}", aliases_of=aliases)
    assert again.handled and "already approved" in again.reply

    outcomes = run(service.run_executor(cfg, conn, now=NOW, dry_run=False, client=client, sender=sender))
    assert [o.action for o in outcomes] == ["executed"]
    assert len(client.writes) == 1
    kind, list_id, body = client.writes[0]
    assert (kind, list_id) == ("create", LIST_SEO)
    assert body["assignees"] == [int(DILEEPA)] and body["due_date_time"] is False
    assert datetime.fromtimestamp(body["due_date"] / 1000, NY) == datetime(2026, 10, 20, 4, 0, tzinfo=NY)

    run(service.run_executor(cfg, conn, now=NOW, dry_run=False, client=client, sender=sender))
    assert len(client.writes) == 1                       # never twice
    row = store.get_approval(conn, aid)
    assert row["status"] == "executed" and row["result"]["url"].endswith("/new1")
    assert "done" in sender.sent[-1][1]
    assert [a[1] for a in audit(conn, "approval")] == ["propose", "approved", "executed"]


def test_rejection_writes_nothing(cfg, conn):
    client, sender = FakeClickUp(), FakeSender()
    aid = propose(cfg, conn, client, sender)["approval_id"]
    out = service.handle_reply(cfg, conn, sender_aliases={YOHAN_NO}, text="no", aliases_of=aliases)
    assert out.decision == "rejected" and out.approval_id == aid
    run(service.run_executor(cfg, conn, now=NOW, dry_run=False, client=client, sender=sender))
    assert client.writes == [] and store.get_approval(conn, aid)["status"] == "rejected"


def test_bare_yes_with_several_pending_asks_for_the_number(cfg, conn):
    client, sender = FakeClickUp(), FakeSender()
    a = propose(cfg, conn, client, sender)["approval_id"]
    b = propose(cfg, conn, client, sender, payload={"task_id": "t9", "text": "Please update"},
                kind="clickup_comment")["approval_id"]
    out = service.handle_reply(cfg, conn, sender_aliases={YOHAN_NO}, text="yes", aliases_of=aliases)
    assert out.handled and out.decision is None and f"#{a}" in out.reply and f"#{b}" in out.reply


def test_plain_yes_with_nothing_pending_passes_through(cfg, conn):
    out = service.handle_reply(cfg, conn, sender_aliases={YOHAN_NO}, text="yes", aliases_of=aliases)
    assert not out.handled


def test_failed_write_is_marked_failed_and_never_retried(cfg, conn):
    client = FakeClickUp(fail_with=ClickUpError("POST -> HTTP 500", status=500, ambiguous=True))
    sender = FakeSender()
    aid = propose(cfg, conn, client, sender)["approval_id"]
    service.handle_reply(cfg, conn, sender_aliases={YOHAN_NO}, text=f"yes {aid}", aliases_of=aliases)
    outcomes = run(service.run_executor(cfg, conn, now=NOW, dry_run=False, client=client, sender=sender))
    assert outcomes[0].action == "failed"
    row = store.get_approval(conn, aid)
    assert row["status"] == "failed" and "may or may not have been applied" in row["error"]
    run(service.run_executor(cfg, conn, now=NOW, dry_run=False, client=client, sender=sender))
    assert len(client.writes) == 1
    assert "FAILED" in sender.sent[-1][1]


def test_repeated_proposal_is_idempotent(cfg, conn):
    client, sender = FakeClickUp(), FakeSender()
    first = propose(cfg, conn, client, sender)
    second = propose(cfg, conn, client, sender)
    assert second["approval_id"] == first["approval_id"] and "already proposed" in second["note"]
    assert len(sender.sent) == 1


@pytest.mark.parametrize("payload,kind,match", [
    ({"list_id": LIST_SEO, "name": "x", "assignee_clickup_user_ids": [ADMIN]}, "clickup_create_task", "never assigned"),
    ({"list_id": "123", "name": "x"}, "clickup_create_task", "lists in pm.yaml"),
    ({"list_id": LIST_SEO, "name": "x", "assignee_clickup_user_ids": ["5"]}, "clickup_create_task", "not in the PM people"),
    ({"list_id": LIST_SEO, "name": "x", "due_date": "2026-02-30"}, "clickup_create_task", "not a real date"),
    ({"task_id": "OUTSIDE", "text": "hi"}, "clickup_comment", "not in a list PM manages"),
    ({"task_id": "t1", "status": "done-ish"}, "clickup_update_task", "status must be one of"),
    ({"task_id": "t1"}, "clickup_update_task", "no change"),
    ({"title": "x", "starts_at_local": "2026-03-08 02:30"}, "meeting_create", "does not exist"),
    ({"title": "x", "starts_at_local": "2026-10-20 10:00", "topic_key": "zzz"}, "meeting_create", "topic_key"),
    ({}, "delete_everything", "unknown change kind"),
])
def test_invalid_proposals_are_refused(cfg, conn, payload, kind, match):
    with pytest.raises(service.ProposalError, match=match):
        propose(cfg, conn, FakeClickUp(), FakeSender(), payload=payload, kind=kind)


def test_test_mode_routes_requests_to_and_accepts_replies_from_the_test_recipient(cfg, conn):
    cfg = dataclasses.replace(cfg, whatsapp_mode="test")
    client, sender = FakeClickUp(), FakeSender()
    aid = propose(cfg, conn, client, sender)["approval_id"]
    assert sender.sent[0][0] == f"{KUSAL_NO}@s.whatsapp.net"
    assert not service.handle_reply(cfg, conn, sender_aliases={YOHAN_NO}, text=f"yes {aid}", aliases_of=aliases).handled
    assert service.handle_reply(cfg, conn, sender_aliases={KUSAL_NO}, text=f"yes {aid}", aliases_of=aliases).decision


def test_meeting_change_via_approval_applies_once_and_is_audited(cfg, conn):
    client, sender = FakeClickUp(), FakeSender()
    aid = propose(cfg, conn, client, sender, kind="meeting_create",
                  payload={"title": "AI sync", "starts_at_local": "2026-10-15 10:00", "topic_key": "ai"})["approval_id"]
    service.handle_reply(cfg, conn, sender_aliases={YOHAN_NO}, text=f"yes {aid}", aliases_of=aliases)
    run(service.run_executor(cfg, conn, now=NOW, dry_run=False, client=client, sender=sender))
    run(service.run_executor(cfg, conn, now=NOW, dry_run=False, client=client, sender=sender))
    meetings = store.meetings_between(conn, NOW, NOW + timedelta(days=3))
    assert len(meetings) == 1 and meetings[0]["starts_at"] == datetime(2026, 10, 15, 10, 0, tzinfo=NY)
    assert client.writes == []
    assert [a[1] for a in audit(conn, "meeting")] == ["create"]


def test_pending_approvals_expire(cfg, conn):
    client, sender = FakeClickUp(), FakeSender()
    aid = propose(cfg, conn, client, sender)["approval_id"]
    # created_at is the DB's real now(), so expiry is measured from the real clock, not NOW.
    later = datetime.now(timezone.utc) + timedelta(hours=cfg.approval_ttl_hours + 1)
    outcomes = run(service.run_executor(cfg, conn, now=later, dry_run=False, client=client, sender=sender))
    assert outcomes[0].action == "expired" and store.get_approval(conn, aid)["status"] == "expired"
    out = service.handle_reply(cfg, conn, sender_aliases={YOHAN_NO}, text=f"yes {aid}", aliases_of=aliases)
    assert "already expired" in out.reply


# ── meeting reminders ────────────────────────────────────────────────────────
def add_meeting(conn, title, starts_at, topic=None, status="scheduled"):
    with conn, conn.cursor() as cur:
        cur.execute("INSERT INTO pm.meetings (title, topic_key, starts_at, status, created_by, updated_by) "
                    "VALUES (%s, %s, %s, %s, 't', 't') RETURNING id", (title, topic, starts_at, status))
        return cur.fetchone()["id"]


def fixture_tasks():
    data = json.loads((FIXTURES / "team_tasks_2026-10-14.json").read_text())["tasks"]
    data.append({"id": "aitask", "name": "Train the AI model", "url": "https://app.clickup.com/t/aitask",
                 "list": {"id": LIST_SEO}, "status": {"status": "in progress", "type": "custom"},
                 "assignees": [], "tags": [{"name": "AI"}], "parent": None, "due_date": None,
                 "date_done": None, "date_closed": None})
    return data


SLOT = datetime(2026, 10, 14, 18, 5, tzinfo=NY)


def test_reminder_sends_once_per_meeting_with_topic_tasks(cfg, conn):
    add_meeting(conn, "AI meeting", datetime(2026, 10, 15, 10, 0, tzinfo=NY), "ai")
    add_meeting(conn, "No topic", datetime(2026, 10, 15, 23, 30, tzinfo=NY))
    add_meeting(conn, "Cancelled", datetime(2026, 10, 15, 11, 0, tzinfo=NY), status="cancelled")
    add_meeting(conn, "Day after", datetime(2026, 10, 16, 0, 0, tzinfo=NY))
    client, sender = FakeClickUp(fixture_tasks()), FakeSender()
    outcomes = run(service.run_meeting_reminders(cfg, conn, now=SLOT, dry_run=False, client=client, sender=sender))
    assert [o.action for o in outcomes] == ["sent", "sent"]
    assert all(chat == GROUP for chat, _ in sender.sent)
    ai_body = sender.sent[0][1]
    assert "AI meeting" in ai_body and "Train the AI model" in ai_body
    assert "no task brief" in sender.sent[1][1]
    again = run(service.run_meeting_reminders(cfg, conn, now=SLOT + timedelta(minutes=30), dry_run=False,
                                              client=client, sender=sender))
    assert [o.action for o in again] == ["skipped_duplicate", "skipped_duplicate"] and len(sender.sent) == 2


def test_rescheduled_meeting_gets_a_new_reminder(cfg, conn):
    mid = add_meeting(conn, "Moved", datetime(2026, 10, 15, 10, 0, tzinfo=NY))
    client, sender = FakeClickUp([]), FakeSender()
    run(service.run_meeting_reminders(cfg, conn, now=SLOT, dry_run=False, client=client, sender=sender))
    with conn, conn.cursor() as cur:
        cur.execute("UPDATE pm.meetings SET starts_at = %s WHERE id = %s", (datetime(2026, 10, 15, 15, 0, tzinfo=NY), mid))
    run(service.run_meeting_reminders(cfg, conn, now=SLOT, dry_run=False, client=client, sender=sender))
    assert len(sender.sent) == 2


def test_reminder_outside_slot_does_nothing(cfg, conn):
    add_meeting(conn, "AI meeting", datetime(2026, 10, 15, 10, 0, tzinfo=NY), "ai")
    sender = FakeSender()
    out = run(service.run_meeting_reminders(cfg, conn, now=SLOT.replace(hour=12), dry_run=False,
                                            client=FakeClickUp([]), sender=sender))
    assert out[0].action == "not_due" and sender.sent == []


def test_failed_send_is_retried_then_given_up(cfg, conn):
    add_meeting(conn, "Flaky", datetime(2026, 10, 15, 10, 0, tzinfo=NY))
    sender = FakeSender(fail_times=10)
    actions = [run(service.run_meeting_reminders(cfg, conn, now=SLOT, dry_run=False, client=FakeClickUp([]),
                                                 sender=sender))[0].action for _ in range(4)]
    assert actions == ["failed", "failed", "failed", "gave_up"]


def test_dry_run_sends_nothing_and_claims_nothing(cfg, conn):
    add_meeting(conn, "AI meeting", datetime(2026, 10, 15, 10, 0, tzinfo=NY), "ai")
    sender = FakeSender()
    out = run(service.run_meeting_reminders(cfg, conn, now=SLOT, dry_run=True, client=FakeClickUp(fixture_tasks()),
                                            sender=sender))
    assert out[0].action == "dry_run" and "AI meeting" in out[0].text and sender.sent == []
    real = run(service.run_meeting_reminders(cfg, conn, now=SLOT, dry_run=False, client=FakeClickUp(fixture_tasks()),
                                             sender=sender))
    assert real[0].action == "sent"


# ── weekly brief ─────────────────────────────────────────────────────────────
MONDAY_9 = datetime(2026, 10, 19, 9, 2, tzinfo=NY)


def test_weekly_brief_is_stored_and_sent_once(cfg, conn):
    client, sender = FakeClickUp(fixture_tasks()), FakeSender()
    out = run(service.run_weekly_brief(cfg, conn, now=MONDAY_9, dry_run=False, client=client, sender=sender,
                                       llm=lambda m: "NONE"))
    assert out.action == "sent" and sender.sent[0][0] == GROUP
    report = store.latest_report(conn, "weekly_brief")
    assert report["send_status"] == "sent" and report["period_start"] == datetime(2026, 10, 12, tzinfo=NY)
    assert f"/pm/reports/{report['id']}" in sender.sent[0][1]
    assert "+" in sender.sent[0][1] and "more" in sender.sent[0][1]             # WhatsApp copy truncated
    assert "more (full report" not in report["rendered_text"]                   # stored copy complete
    again = run(service.run_weekly_brief(cfg, conn, now=MONDAY_9 + timedelta(minutes=5), dry_run=False,
                                         client=client, sender=sender, llm=lambda m: "NONE"))
    assert again.action == "already_sent" and len(sender.sent) == 1


def test_weekly_brief_not_due_outside_slot(cfg, conn):
    out = run(service.run_weekly_brief(cfg, conn, now=MONDAY_9 - timedelta(hours=1), dry_run=False,
                                       client=FakeClickUp([]), sender=FakeSender()))
    assert out.action == "not_due"


def test_weekly_brief_agent_note_is_appended_last(cfg, conn):
    sender = FakeSender()
    run(service.run_weekly_brief(cfg, conn, now=MONDAY_9, dry_run=False, client=FakeClickUp(fixture_tasks()),
                                 sender=sender, llm=lambda m: "Most overdue work sits with one person."))
    assert sender.sent[0][1].rstrip().endswith("*Captain's note*\nMost overdue work sits with one person.")
    assert store.latest_report(conn, "weekly_brief")["agent_note"] == "Most overdue work sits with one person."


def test_stuck_claims_are_reported(conn):
    mid = store.claim_send(conn, dedupe_key="k1", purpose="test", chat_id="x", body="b")
    assert store.claim_send(conn, dedupe_key="k1", purpose="test", chat_id="x", body="b") is None
    assert [r["id"] for r in store.stuck_sends(conn, datetime.now(timezone.utc) + timedelta(seconds=1))] == [mid]


# ── deletes (Kusal, 2026-10-15) ──────────────────────────────────────────────
def kusal_aliases(x):
    """Kusal's DM arrives as a LID; the root bridge session maps it to his number."""
    x = x.split("@")[0].lstrip("+")
    return {x, KUSAL_NO} if x in ("777000111", KUSAL_NO) else {x}


def self_cfg(cfg):
    return dataclasses.replace(cfg, self_approved_delete_clickup_user_ids=frozenset({KUSAL}))


def propose_delete(cfg, conn, client, sender, task_id="t7", ids=None, kind="clickup_delete_task", payload=None):
    return run(service.propose_change(
        cfg, conn, client, kind=kind, payload=payload or {"task_id": task_id}, reason="outdated",
        requested_by="Kusal (whatsapp:777000111)", now=NOW, sender=sender,
        requester_whatsapp_ids=ids if ids is not None else ["777000111@lid"], aliases_of=kusal_aliases))


def test_delete_needs_yohan_by_default_and_runs_once(cfg, conn):
    client, sender = FakeClickUp(), FakeSender()
    result = propose_delete(cfg, conn, client, sender)
    assert result["status"] == "pending"
    assert result["summary"] == ('Delete task "Task t7" (list "SEO / Tasks", status "to do", assigned to Dileepa).')
    assert client.writes == [] and sender.sent[0][0] == f"{YOHAN_NO}@s.whatsapp.net"
    service.handle_reply(cfg, conn, sender_aliases={YOHAN_NO}, text=f"yes {result['approval_id']}", aliases_of=aliases)
    run(service.run_executor(cfg, conn, now=NOW, dry_run=False, client=client, sender=sender))
    run(service.run_executor(cfg, conn, now=NOW, dry_run=False, client=client, sender=sender))
    assert client.writes == [("delete", "t7")]
    assert store.get_approval(conn, result["approval_id"])["status"] == "executed"


def test_delete_summary_counts_subtasks(cfg, conn):
    result = propose_delete(cfg, conn, FakeClickUp(), FakeSender(), task_id="PARENT")
    assert result["summary"].endswith("and its 2 subtasks.")


def test_self_approved_delete_runs_now_without_messaging_yohan(cfg, conn):
    client, sender = FakeClickUp(), FakeSender()
    result = propose_delete(self_cfg(cfg), conn, client, sender)
    assert result["status"] == "executed" and result["self_approved_by"] == "Kusal Darshana"
    assert client.writes == [("delete", "t7")] and sender.sent == []
    row = store.get_approval(conn, result["approval_id"])
    assert row["status"] == "executed" and "self-approved" in row["decided_by"]
    assert [a[1] for a in audit(conn, "approval")] == ["propose", "approved", "executed"]
    again = propose_delete(self_cfg(cfg), conn, client, sender)          # same request, same day
    assert again["approval_id"] == result["approval_id"] and "not run again" in again["note"]
    assert client.writes == [("delete", "t7")]
    run(service.run_executor(cfg, conn, now=NOW, dry_run=False, client=client, sender=sender))
    assert client.writes == [("delete", "t7")]                          # the executor never re-runs it


@pytest.mark.parametrize("ids,kind,payload", [
    ([], "clickup_delete_task", None),                                   # no verified sender (CLI, cron)
    (["19999999999@s.whatsapp.net"], "clickup_delete_task", None),       # someone not listed
    (["777000111@lid"], "clickup_comment", {"task_id": "t7", "text": "x"}),   # listed, but not a delete
])
def test_everyone_else_still_needs_yohan(cfg, conn, ids, kind, payload):
    client, sender = FakeClickUp(), FakeSender()
    result = propose_delete(self_cfg(cfg), conn, client, sender, ids=ids, kind=kind, payload=payload)
    assert result["status"] == "pending" and client.writes == [] and len(sender.sent) == 1


def test_self_approved_delete_failure_is_reported_and_recorded(cfg, conn):
    client = FakeClickUp(fail_with=ClickUpError("DELETE -> HTTP 404 Task not found", status=404))
    result = propose_delete(self_cfg(cfg), conn, client, FakeSender())
    assert result["status"] == "failed" and "404" in result["error"]
    assert store.get_approval(conn, result["approval_id"])["status"] == "failed"


def test_delete_outside_managed_lists_is_refused(cfg, conn):
    with pytest.raises(service.ProposalError, match="not in a list PM manages"):
        propose_delete(self_cfg(cfg), conn, FakeClickUp(), FakeSender(), task_id="OUTSIDE")


def test_db_rejects_unknown_kind(conn):
    with pytest.raises(psycopg2.errors.CheckViolation):
        with conn, conn.cursor() as cur:
            cur.execute("INSERT INTO pm.approvals (kind, payload, summary, requested_by, approver_person_id, "
                        "idempotency_key) VALUES ('clickup_delete_space', '{}', 's', 'r', 1, 'k')")
