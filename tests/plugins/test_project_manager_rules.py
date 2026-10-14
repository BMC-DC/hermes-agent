"""Deterministic report rules (PM_PLAN §5, §10 unit list). No network, no DB."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from plugins.project_manager import rules

NY = ZoneInfo("America/New_York")
ZONES = tuple(ZoneInfo(z) for z in ("Asia/Colombo", "America/Vancouver", "America/Denver", "America/New_York"))
FIXTURE = Path(__file__).parent / "fixtures" / "project_manager" / "team_tasks_2026-10-14.json"
LISTS = frozenset({"901416409229", "901417429756", "901417261749", "901417261750", "901417261751",
                   "901417261752", "901417261753"})
ADMIN = "210143482"


def cfg(**over) -> rules.ReportConfig:
    base = dict(tz=NY, list_ids=frozenset({"L1"}), exclude_tags=frozenset({"stylus"}), admin_account_id=ADMIN,
                upcoming_days=14, date_only_zones=ZONES)
    base.update(over)
    return rules.ReportConfig(**base)


def ms(dt: datetime) -> str:
    return str(int(dt.timestamp() * 1000))


def task(tid="t1", *, status_type="open", status="to do", list_id="L1", due=None, done=None, closed=None,
         assignees=(), tags=(), parent=None, name=None) -> dict:
    return {
        "id": tid, "name": name or f"Task {tid}", "url": f"https://app.clickup.com/t/{tid}",
        "list": {"id": list_id}, "status": {"status": status, "type": status_type},
        "assignees": [{"id": a, "username": f"user{a}"} for a in assignees],
        "tags": [{"name": t} for t in tags], "parent": parent,
        "due_date": ms(due) if due else None, "date_done": ms(done) if done else None,
        "date_closed": ms(closed) if closed else None,
    }


def local(y, m, d, h=0, mi=0, tz=NY) -> datetime:
    return datetime(y, m, d, h, mi, tzinfo=tz)


# ── status buckets ───────────────────────────────────────────────────────────
@pytest.mark.parametrize("status_type,bucket", [
    ("open", rules.NOT_STARTED), ("custom", rules.IN_PROGRESS), ("done", rules.COMPLETED),
    ("closed", rules.COMPLETED), ("weird", rules.UNCLASSIFIED),
])
def test_bucket_is_driven_by_status_type_only(status_type, bucket):
    assert rules.Task.from_api(task(status_type=status_type, status="anything")).bucket == bucket


def test_in_review_counts_as_in_progress():
    report = rules.build_report([task(status_type="custom", status="in review")], cfg(), now=local(2026, 10, 14, 12))
    assert [r.task.id for r in report.sections["in_progress"]] == ["t1"]


def test_mcp_shape_without_status_type_is_rejected():
    raw = task()
    raw["status"] = "to do"
    with pytest.raises(ValueError, match="status has no 'type'"):
        rules.Task.from_api(raw)


def test_unknown_status_type_is_reported_not_guessed():
    report = rules.build_report([task(status_type="archived_x")], cfg(), now=local(2026, 10, 14, 12))
    assert report.counts["unclassified"] == 1
    assert report.counts["in_progress"] == report.counts["not_started"] == 0


# ── due-date interpretation (rule agreed 2026-10-14) ─────────────────────────
@pytest.mark.parametrize("instant_utc,expected_day,has_time", [
    # 04:00 Colombo on 20 Oct = 22:30 UTC on 19 Oct: a date-only due date of 20 Oct (not 19 Oct NY).
    (datetime(2026, 10, 19, 22, 30, tzinfo=timezone.utc), "2026-10-20", False),
    # 04:00 Vancouver (PDT) = 11:00 UTC.
    (datetime(2026, 10, 20, 11, 0, tzinfo=timezone.utc), "2026-10-20", False),
    # 11:00 UTC in winter is 04:00 Denver (MST) and, under newer tzdata, 04:00 Vancouver: date-only
    # whichever tzdata version is installed.
    (datetime(2026, 12, 20, 11, 0, tzinfo=timezone.utc), "2026-12-20", False),
    # 04:00 New York (EDT) = 08:00 UTC.
    (datetime(2026, 10, 20, 8, 0, tzinfo=timezone.utc), "2026-10-20", False),
    # 17:00 UTC is 04:00 in none of the zones: a real time, 13:00 New York.
    (datetime(2026, 10, 20, 17, 0, tzinfo=timezone.utc), "2026-10-20", True),
    # 23:59 New York (03:59 UTC next day) is a real time on the New York date.
    (datetime(2026, 10, 21, 3, 59, tzinfo=timezone.utc), "2026-10-20", True),
])
def test_interpret_due(instant_utc, expected_day, has_time):
    due = rules.interpret_due(instant_utc, cfg())
    assert due.day.isoformat() == expected_day
    assert due.has_time is has_time


def test_interpret_due_none():
    assert rules.interpret_due(None, cfg()) is None


def test_date_only_due_is_not_overdue_on_its_own_day_even_late_evening():
    t = task(due=datetime(2026, 10, 19, 22, 30, tzinfo=timezone.utc))   # due 20 Oct (Colombo date-only)
    late_on_due_day = local(2026, 10, 20, 23, 59)
    report = rules.build_report([t], cfg(), now=late_on_due_day)
    assert report.counts["overdue_total"] == 0
    assert report.counts["due_soon"] == 1


def test_date_only_due_becomes_overdue_at_new_york_midnight():
    t = task(due=datetime(2026, 10, 19, 22, 30, tzinfo=timezone.utc))   # due 20 Oct
    just_after = local(2026, 10, 21, 0, 0)
    report = rules.build_report([t], cfg(), now=just_after)
    assert report.sections["overdue_upto_7"][0].days_overdue == 1


def test_timed_due_is_overdue_right_after_the_time():
    due = local(2026, 10, 20, 13, 0)
    report = rules.build_report([task(due=due.astimezone(timezone.utc))], cfg(), now=due + timedelta(minutes=1))
    row = report.sections["overdue_upto_7"][0]
    assert row.days_overdue == 0


@pytest.mark.parametrize("days,bucket", [
    (1, "overdue_upto_7"), (7, "overdue_upto_7"), (8, "overdue_8_30"), (30, "overdue_8_30"), (31, "overdue_over_30"),
])
def test_overdue_age_buckets(days, bucket):
    now = local(2026, 10, 14, 12)
    due_day = (now - timedelta(days=days)).date()
    due_instant = datetime(due_day.year, due_day.month, due_day.day, 4, 0, tzinfo=NY)   # NY date-only
    report = rules.build_report([task(due=due_instant)], cfg(), now=now)
    assert [r.days_overdue for r in report.sections[bucket]] == [days]
    assert report.counts["overdue_total"] == 1


def test_due_within_window_edges():
    now = local(2026, 10, 14, 12)
    inside = datetime(2026, 10, 28, 4, 0, tzinfo=NY)      # today + 14, date-only
    outside = datetime(2026, 10, 29, 4, 0, tzinfo=NY)     # today + 15
    report = rules.build_report([task("a", due=inside), task("b", due=outside)], cfg(), now=now)
    assert [r.task.id for r in report.sections["due_soon"]] == ["a"]


def test_completed_task_is_never_overdue_or_due_soon():
    now = local(2026, 10, 14, 12)
    t = task(status_type="closed", due=now - timedelta(days=40), done=now - timedelta(days=1))
    report = rules.build_report([t], cfg(), now=now)
    assert report.counts["overdue_total"] == 0 and report.counts["due_soon"] == 0


# ── completed window ─────────────────────────────────────────────────────────
def test_completed_in_window_uses_date_done_then_date_closed():
    start, end = local(2026, 10, 5), local(2026, 10, 12)
    tasks = [
        task("done_in", status_type="done", done=local(2026, 10, 6, 9)),
        task("closed_in", status_type="closed", closed=local(2026, 10, 11, 23, 59)),
        task("at_end", status_type="closed", done=end),                      # end is exclusive
        task("before", status_type="closed", done=local(2026, 10, 4, 23, 59)),
        task("no_date", status_type="closed"),
    ]
    report = rules.build_report(tasks, cfg(), now=local(2026, 10, 12, 9), window_start=start, window_end=end)
    assert sorted(r.task.id for r in report.sections["completed_in_window"]) == ["closed_in", "done_in"]
    assert [r.task.id for r in report.sections["completed_date_unknown"]] == ["no_date"]


def test_window_bounds_must_be_given_together():
    with pytest.raises(ValueError):
        rules.build_report([], cfg(), now=local(2026, 10, 14), window_start=local(2026, 10, 5))


def test_naive_now_is_rejected():
    with pytest.raises(ValueError):
        rules.build_report([], cfg(), now=datetime(2026, 10, 14))


# ── scope, exclusions, people ────────────────────────────────────────────────
def test_subtasks_count_as_tasks_and_show_parent_name():
    tasks = [task("p", name="Parent", status_type="custom"), task("c", name="Child", parent="p")]
    report = rules.build_report(tasks, cfg(), now=local(2026, 10, 14))
    child = report.sections["not_started"][0]
    assert child.task.id == "c" and child.parent_name == "Parent"


def test_subtask_with_parent_outside_the_set_keeps_an_id_label():
    report = rules.build_report([task("c", parent="zzz")], cfg(), now=local(2026, 10, 14))
    assert report.sections["not_started"][0].parent_name == "(parent task zzz)"


def test_excluded_tags_are_left_out_case_insensitively():
    report = rules.build_report([task("a", tags=("Stylus",)), task("b")], cfg(), now=local(2026, 10, 14))
    assert report.excluded_count == 1
    assert [r.task.id for r in report.sections["not_started"]] == ["b"]


def test_tasks_outside_configured_lists_are_counted_not_reported():
    report = rules.build_report([task("a", list_id="OTHER")], cfg(), now=local(2026, 10, 14))
    assert report.out_of_scope_count == 1 and report.counts["not_started"] == 0


def test_admin_account_assignment_is_flagged_even_when_completed():
    tasks = [task("a", assignees=(ADMIN,)), task("b", status_type="closed", assignees=(ADMIN, "1"))]
    report = rules.build_report(tasks, cfg(), now=local(2026, 10, 14))
    assert sorted(r.task.id for r in report.sections["admin_assigned"]) == ["a", "b"]


def test_unassigned_and_no_due_date_cover_only_open_tasks():
    tasks = [task("open"), task("done", status_type="closed")]
    report = rules.build_report(tasks, cfg(), now=local(2026, 10, 14))
    assert [r.task.id for r in report.sections["unassigned"]] == ["open"]
    assert [r.task.id for r in report.sections["no_due_date"]] == ["open"]


# ── weekly window ────────────────────────────────────────────────────────────
def test_weekly_window_is_previous_monday_to_monday_in_new_york():
    start, end = rules.weekly_window(local(2026, 10, 19, 9, 0), NY)    # Monday 19 Oct
    assert (start, end) == (local(2026, 10, 12), local(2026, 10, 19))


def test_weekly_window_across_dst_end_is_eight_days_of_hours_but_seven_local_days():
    # US DST ends Sun 1 Nov 2026: the week Mon 26 Oct .. Mon 2 Nov has 169 hours.
    start, end = rules.weekly_window(local(2026, 11, 2, 9, 0), NY)
    assert start == local(2026, 10, 26) and end == local(2026, 11, 2)
    assert end.astimezone(timezone.utc) - start.astimezone(timezone.utc) == timedelta(hours=169)


def test_weekly_window_from_utc_instant_uses_local_date():
    # 02:00 UTC Monday = 22:00 Sunday New York: still the previous local week.
    start, _ = rules.weekly_window(datetime(2026, 10, 19, 2, 0, tzinfo=timezone.utc), NY)
    assert start == local(2026, 10, 5)


# ── real data (scrubbed live capture, 2026-10-14) ────────────────────────────
def test_fixture_report_counts_are_stable():
    data = json.loads(FIXTURE.read_text())["tasks"]
    now = datetime(2026, 10, 14, 0, 9, tzinfo=timezone.utc)
    start, end = rules.weekly_window(now, NY)
    report = rules.build_report(data, cfg(list_ids=LISTS, exclude_tags=frozenset({"stylus", "event-post-pipeline"})),
                                now=now, window_start=start, window_end=end)
    assert len(data) == 131
    assert report.out_of_scope_count == 0
    assert report.counts["in_progress"] == 34
    assert report.counts["not_started"] == 13
    assert report.counts["admin_assigned"] == 4
    assert report.counts["completed_date_unknown"] == 0      # every completed task has date_done
    total = sum(report.counts[k] for k in ("in_progress", "not_started", "unclassified"))
    completed = sum(1 for t in data if t["status"]["type"] in ("done", "closed")
                    and not ({g["name"] for g in t["tags"]} & {"stylus", "event-post-pipeline"}))
    assert total + completed + report.excluded_count == 131
