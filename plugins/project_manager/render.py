"""Deterministic text templates: weekly brief, meeting reminder, approval
messages (PM_PLAN §5.3, §5.4, §7). Pure functions of their inputs; every
number comes from ``rules.Report.counts``. The only LLM text that can appear
is the optional agent note, appended last and labelled.

Formatting is WhatsApp-flavoured (``*bold*``). ``max_rows`` truncates long
sections for WhatsApp; the stored copy is rendered with ``max_rows=None``.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Optional, Sequence
from zoneinfo import ZoneInfo

from plugins.project_manager.rules import Report, Row

BRIEF_SECTIONS: tuple[tuple[str, str], ...] = (
    ("overdue_over_30", "Overdue: more than 30 days"),
    ("overdue_8_30", "Overdue: 8 to 30 days"),
    ("overdue_upto_7", "Overdue: up to 7 days"),
    ("due_soon", "Due in the next {days} days"),
    ("in_progress", "In progress"),
    ("not_started", "Not started"),
    ("completed_in_window", "Completed in the period"),
    ("completed_date_unknown", "Completed, date unknown"),
    ("no_due_date", "No due date (not completed)"),
    ("unassigned", "Unassigned (not completed)"),
    ("admin_assigned", "Assigned to the shared admin account (should be reassigned)"),
    ("unclassified", "Unknown status type (check ClickUp)"),
)

COUNT_LABELS: tuple[tuple[str, str], ...] = (
    ("completed_in_window", "Completed in the period"),
    ("in_progress", "In progress"),
    ("not_started", "Not started"),
    ("overdue_total", "Overdue, total"),
    ("overdue_upto_7", "  up to 7 days"),
    ("overdue_8_30", "  8 to 30 days"),
    ("overdue_over_30", "  more than 30 days"),
    ("due_soon", "Due in the next {days} days"),
    ("no_due_date", "No due date"),
    ("unassigned", "Unassigned"),
    ("admin_assigned", "Assigned to the shared admin account"),
)


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def fmt_dt(value: datetime, tz: ZoneInfo, with_time: bool = True) -> str:
    local = value.astimezone(tz)
    return local.strftime("%a %d %b %Y %H:%M %Z") if with_time else local.strftime("%a %d %b %Y")


def assignee_text(row: Row, admin_account_id: str) -> str:
    if not row.task.assignees:
        return "unassigned"
    names = []
    for a in row.task.assignees:
        names.append(f"{a.name} (shared admin account)" if a.id == admin_account_id else a.name)
    return ", ".join(names)


def row_line(index: int, row: Row, admin_account_id: str) -> str:
    title = row.task.name.strip() or "(untitled task)"
    if row.parent_name:
        title = f"{title} (subtask of: {row.parent_name})"
    parts = [f"{index}. {title}", assignee_text(row, admin_account_id)]
    if row.task.is_completed and row.task.done_at is not None:
        parts.append(f"done {row.task.done_at.strftime('%d %b')}")
    elif row.due_local:
        parts.append(f"due {row.due_local}")
    else:
        parts.append("no due date")
    if row.days_overdue is not None:
        parts.append(f"{_plural(row.days_overdue, 'day')} overdue")
    if row.task.bucket in ("in_progress", "not_started"):
        parts.append(row.task.status_name)
    line = " | ".join(parts)
    if row.task.url:
        line += f"\n   {row.task.url}"
    return line


def tldr_lines(report: Report) -> list[str]:
    c = report.counts
    lines = []
    if report.window_start is not None:
        lines.append(f"{c['completed_in_window']} completed in the period, {c['in_progress']} in progress, "
                     f"{c['not_started']} not started.")
    else:
        lines.append(f"{c['in_progress']} in progress, {c['not_started']} not started.")
    lines.append(f"{c['overdue_total']} overdue ({c['overdue_over_30']} by more than 30 days); "
                 f"{c['due_soon']} due in the next {report.upcoming_days} days.")
    attention = []
    if c["admin_assigned"]:
        attention.append(f"{_plural(c['admin_assigned'], 'task')} on the shared admin account")
    if c["unassigned"]:
        attention.append(f"{c['unassigned']} unassigned")
    if c["no_due_date"]:
        attention.append(f"{c['no_due_date']} with no due date")
    if attention:
        lines.append("Needs attention: " + ", ".join(attention) + ".")
    return lines[:3]


def render_section(title: str, rows: Sequence[Row], admin_account_id: str, max_rows: Optional[int],
                   more_link: Optional[str]) -> list[str]:
    out = [f"*{title} ({len(rows)})*"]
    shown = rows if max_rows is None else rows[:max_rows]
    out.extend(row_line(i, r, admin_account_id) for i, r in enumerate(shown, 1))
    hidden = len(rows) - len(shown)
    if hidden > 0:
        out.append(f"+{hidden} more" + (f" (full report: {more_link})" if more_link else ""))
    return out


def render_weekly_brief(report: Report, *, tz: ZoneInfo, admin_account_id: str,
                        max_rows: Optional[int] = None, full_report_url: Optional[str] = None,
                        agent_note: Optional[str] = None, out_of_scope_note: bool = True) -> str:
    days = report.upcoming_days
    lines: list[str] = ["*Weekly project brief*"]
    if report.window_start is not None and report.window_end is not None:
        # window_end is exclusive (local midnight); the last day shown is the day before it.
        last_day = report.window_end.astimezone(tz) - timedelta(days=1)
        lines.append(f"Period: {fmt_dt(report.window_start, tz, False)} to {fmt_dt(last_day, tz, False)} "
                     f"({tz.key} time)")
    lines.append(f"Generated: {fmt_dt(report.generated_at, tz)}")
    lines += ["", "*TLDR*"] + [f"- {l}" for l in tldr_lines(report)]

    lines += ["", "*Counts*"]
    for key, label in COUNT_LABELS:
        if key == "completed_in_window" and report.window_start is None:
            continue
        lines.append(f"{label.format(days=days)}: {report.counts[key]}")

    for key, title in BRIEF_SECTIONS:
        rows = report.sections.get(key) or []
        if not rows:
            continue
        lines.append("")
        lines += render_section(title.format(days=days), rows, admin_account_id, max_rows, full_report_url)

    if out_of_scope_note and (report.out_of_scope_count or report.excluded_count):
        lines.append("")
        notes = []
        if report.excluded_count:
            notes.append(f"{_plural(report.excluded_count, 'task')} left out by the exclude tags in pm.yaml")
        if report.out_of_scope_count:
            notes.append(f"{_plural(report.out_of_scope_count, 'task')} in ClickUp lists that pm.yaml does not list "
                         "(add the list to pm.yaml to include them)")
        lines.append("Note: " + "; ".join(notes) + ".")

    if agent_note:
        lines += ["", "*Agent note*", agent_note.strip()]
    return "\n".join(lines)


def report_payload(report: Report) -> dict[str, Any]:
    """JSON-safe structured copy of a report for ``pm.reports.payload``."""
    def row(r: Row) -> dict[str, Any]:
        return {
            "id": r.task.id, "name": r.task.name, "url": r.task.url, "list_id": r.task.list_id,
            "status": r.task.status_name, "status_type": r.task.status_type, "bucket": r.bucket,
            "parent_name": r.parent_name, "assignees": [{"id": a.id, "name": a.name} for a in r.task.assignees],
            "tags": sorted(r.task.tags), "due": r.due_local, "due_day": r.due.day.isoformat() if r.due else None,
            "days_overdue": r.days_overdue,
            "done_at": r.task.done_at.isoformat() if r.task.done_at else None,
        }
    return {
        "generated_at": report.generated_at.isoformat(),
        "window_start": report.window_start.isoformat() if report.window_start else None,
        "window_end": report.window_end.isoformat() if report.window_end else None,
        "upcoming_days": report.upcoming_days,
        "counts": dict(report.counts),
        "out_of_scope_count": report.out_of_scope_count,
        "excluded_count": report.excluded_count,
        "sections": {k: [row(r) for r in v] for k, v in report.sections.items()},
    }


def render_meeting_reminder(meeting: dict, rows: Optional[Sequence[Row]], *, tz: ZoneInfo, admin_account_id: str,
                            topic_label: Optional[str], max_rows: Optional[int]) -> str:
    starts = meeting["starts_at"]
    lines = [f"*Meeting reminder: {meeting['title']}*",
             f"When: {fmt_dt(starts, tz)} ({tz.key} time)"]
    if meeting.get("duration_minutes"):
        lines.append(f"Length: {meeting['duration_minutes']} minutes")
    if meeting.get("attendees"):
        lines.append("Attendees: " + ", ".join(meeting["attendees"]))
    if meeting.get("notes"):
        lines += ["Notes:", str(meeting["notes"]).strip()]
    lines.append("")
    if rows is None:
        lines.append("No topic is set for this meeting, so no task brief is attached.")
    elif not rows:
        lines.append(f"No open tasks carry the {topic_label} topic tags.")
    else:
        lines += render_section(f"Open tasks for {topic_label}", rows, admin_account_id, max_rows, None)
    return "\n".join(lines)


def render_approval_request(approval_id: int, summary: str, reason: Optional[str], requested_by: str) -> str:
    lines = [f"*Approval needed (#{approval_id})*", summary.strip()]
    if reason:
        lines.append(f"Why: {reason.strip()}")
    lines.append(f"Asked by: {requested_by}")
    lines += ["", f"Reply *YES {approval_id}* to approve or *NO {approval_id}* to reject."]
    return "\n".join(lines)


def render_approval_ack(approval_id: int, decision: str) -> str:
    if decision == "approved":
        return f"Approval #{approval_id}: approved. Running it now; I will confirm when it is done."
    return f"Approval #{approval_id}: rejected. Nothing was changed."


def render_approval_result(approval_id: int, summary: str, ok: bool, detail: Optional[str]) -> str:
    if ok:
        return f"Approval #{approval_id} done: {summary.strip()}" + (f"\n{detail}" if detail else "")
    return (f"Approval #{approval_id} FAILED: {summary.strip()}\nError: {detail or 'unknown'}\n"
            "It will not be retried automatically. Ask again if it should be redone.")
