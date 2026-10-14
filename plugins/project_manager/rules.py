"""Deterministic report rules for the ProjectManager plugin (PM_PLAN §5).

Pure functions only: no I/O, no clock reads (``now`` is always passed in), no
LLM. Every classification uses ClickUp's ``status.type`` — never the status
name, which differs per list (PM_PLAN §3.2).

Input is the REST shape of ``GET /v2/team/{team_id}/task`` (not the MCP
shape, which drops ``status.type`` and ``date_done``). ``Task.from_api`` is
the single place that reads raw API fields.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Optional, Sequence
from zoneinfo import ZoneInfo

# Status types (PM_PLAN §5.1). Anything else lands in UNCLASSIFIED and is
# reported, never silently dropped or guessed into a bucket.
NOT_STARTED = "not_started"
IN_PROGRESS = "in_progress"
COMPLETED = "completed"
UNCLASSIFIED = "unclassified"

_STATUS_TYPE_TO_BUCKET = {
    "open": NOT_STARTED,
    "custom": IN_PROGRESS,
    "done": COMPLETED,
    "closed": COMPLETED,
}

# Overdue age buckets (PM_PLAN §5.2), by whole calendar days overdue in the
# standard timezone. A task due earlier today counts as 0 days and falls in
# the first bucket.
OVERDUE_BUCKETS: tuple[tuple[str, int, Optional[int]], ...] = (
    ("overdue_upto_7", 0, 7),
    ("overdue_8_30", 8, 30),
    ("overdue_over_30", 31, None),
)


def _ms_to_utc(value: Any) -> Optional[datetime]:
    """ClickUp sends Unix milliseconds as a string (or int); null/'' means unset."""
    if value is None or value == "":
        return None
    try:
        ms = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"not a Unix-ms timestamp: {value!r}") from exc
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


@dataclass(frozen=True)
class Assignee:
    id: str
    name: str


@dataclass(frozen=True)
class Task:
    id: str
    name: str
    url: str
    list_id: str
    status_name: str
    status_type: str
    assignees: tuple[Assignee, ...]
    tags: frozenset[str]          # lower-cased tag names
    parent_id: Optional[str]
    due: Optional[datetime]       # UTC instant as stored; interpret with interpret_due()
    done_at: Optional[datetime]   # date_done, else date_closed (UTC)

    @classmethod
    def from_api(cls, raw: Mapping[str, Any]) -> "Task":
        status = raw.get("status") or {}
        if not isinstance(status, Mapping) or "type" not in status:
            raise ValueError(f"task {raw.get('id')!r}: status has no 'type' (MCP shape, not REST?)")
        lst = raw.get("list") or {}
        assignees = tuple(
            Assignee(id=str(a.get("id")), name=str(a.get("username") or a.get("email") or a.get("id")))
            for a in (raw.get("assignees") or [])
        )
        tags = frozenset(str(t.get("name", "")).strip().lower() for t in (raw.get("tags") or []) if t.get("name"))
        done_at = _ms_to_utc(raw.get("date_done")) or _ms_to_utc(raw.get("date_closed"))
        return cls(
            id=str(raw["id"]),
            name=str(raw.get("name") or ""),
            url=str(raw.get("url") or ""),
            list_id=str(lst.get("id") or ""),
            status_name=str(status.get("status") or ""),
            status_type=str(status.get("type") or "").lower(),
            assignees=assignees,
            tags=tags,
            parent_id=str(raw["parent"]) if raw.get("parent") else None,
            due=_ms_to_utc(raw.get("due_date")),
            done_at=done_at,
        )

    @property
    def bucket(self) -> str:
        return _STATUS_TYPE_TO_BUCKET.get(self.status_type, UNCLASSIFIED)

    @property
    def is_completed(self) -> bool:
        return self.bucket == COMPLETED


@dataclass(frozen=True)
class ReportConfig:
    """The subset of pm.yaml the rules need. Built by the config loader."""
    tz: ZoneInfo
    list_ids: frozenset[str]
    exclude_tags: frozenset[str]   # lower-cased
    admin_account_id: str
    upcoming_days: int
    # Zones whose local 04:00 marks a date-only due date (see interpret_due).
    date_only_zones: tuple[ZoneInfo, ...] = ()


@dataclass(frozen=True)
class Due:
    day: date                      # the due calendar date
    at: Optional[datetime]         # UTC instant when the due date carries a real time, else None

    @property
    def has_time(self) -> bool:
        return self.at is not None


@dataclass(frozen=True)
class Row:
    task: Task
    parent_name: Optional[str]
    bucket: str
    due: Optional[Due]
    due_local: Optional[str]       # rendered in the standard tz, or None
    days_overdue: Optional[int]


@dataclass
class Report:
    generated_at: datetime         # UTC
    window_start: Optional[datetime]
    window_end: Optional[datetime]
    upcoming_days: int
    counts: dict[str, int] = field(default_factory=dict)
    sections: dict[str, list[Row]] = field(default_factory=dict)
    out_of_scope_count: int = 0    # tasks in lists not in pm.yaml (warned, not reported)
    excluded_count: int = 0        # tasks carrying an exclude tag


DATE_ONLY_LOCAL_TIME = (4, 0)   # ClickUp stores a date-only due date at 04:00 in the setter's zone


def interpret_due(due_utc: Optional[datetime], cfg: ReportConfig) -> Optional[Due]:
    """Decide what calendar date (and time, if any) a stored due instant means.

    ClickUp's task list API has no ``due_date_time`` flag (verified live
    2026-10-14: absent on every task, list and single-task endpoints), and a
    date-only due date is stored as 04:00 local time in the zone of whoever set
    it. Rule agreed with Kusal 2026-10-14:

    - If the instant is exactly 04:00:00 local in one of ``cfg.date_only_zones``
      (the team's zones), it is a date-only due date; its date is that zone's
      local date. Two zones that both read 04:00 at the same instant have the
      same UTC offset, so the date is never ambiguous.
    - Otherwise it carries a real time; the date is the standard-zone date.
    """
    if due_utc is None:
        return None
    for zone in cfg.date_only_zones:
        local = due_utc.astimezone(zone)
        if (local.hour, local.minute, local.second, local.microsecond) == (*DATE_ONLY_LOCAL_TIME, 0, 0):
            return Due(day=local.date(), at=None)
    return Due(day=due_utc.astimezone(cfg.tz).date(), at=due_utc)


def days_overdue(task: Task, due: Optional[Due], now: datetime, tz: ZoneInfo) -> Optional[int]:
    """Whole calendar days overdue in ``tz``, or None if not overdue.

    Timed due date: overdue once ``due.at < now``. Date-only due date: overdue
    once its date is before today in ``tz`` (the whole due day is allowed).
    """
    if due is None or task.is_completed:
        return None
    today = now.astimezone(tz).date()
    if due.has_time:
        if due.at >= now:
            return None
    elif due.day >= today:
        return None
    return (today - due.day).days


def is_due_within(task: Task, due: Optional[Due], now: datetime, tz: ZoneInfo, days: int) -> bool:
    """Not completed and due between now and ``days`` days ahead (PM_PLAN §5.2).
    Timed: ``now <= at <= now + days``. Date-only: ``today <= day <= today + days``."""
    if due is None or task.is_completed:
        return False
    if due.has_time:
        return now <= due.at <= now + timedelta(days=days)
    today = now.astimezone(tz).date()
    return today <= due.day <= today + timedelta(days=days)


def overdue_bucket(days: int) -> str:
    for key, low, high in OVERDUE_BUCKETS:
        if days >= low and (high is None or days <= high):
            return key
    raise ValueError(f"negative days overdue: {days}")


def format_due(due: Optional[Due], tz: ZoneInfo) -> Optional[str]:
    if due is None:
        return None
    if due.has_time:
        return due.at.astimezone(tz).strftime("%a %d %b %Y %H:%M")
    return due.day.strftime("%a %d %b %Y")


SECTION_KEYS: tuple[str, ...] = (
    "completed_in_window",
    "completed_date_unknown",
    "in_progress",
    "not_started",
    "overdue_upto_7",
    "overdue_8_30",
    "overdue_over_30",
    "due_soon",
    "no_due_date",
    "unassigned",
    "admin_assigned",
    "unclassified",
)


def build_report(
    raw_tasks: Iterable[Mapping[str, Any]],
    cfg: ReportConfig,
    *,
    now: datetime,
    window_start: Optional[datetime] = None,
    window_end: Optional[datetime] = None,
) -> Report:
    """Classify every task into the PM_PLAN §5 sections.

    ``now``/``window_*`` must be timezone-aware. The completed-in-window
    section is only built when a window is given (the weekly brief); ad-hoc
    queries call this without one.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    if (window_start is None) != (window_end is None):
        raise ValueError("window_start and window_end go together")
    if window_start is not None and (window_start.tzinfo is None or window_end.tzinfo is None):
        raise ValueError("window bounds must be timezone-aware")

    all_tasks = [Task.from_api(t) for t in raw_tasks]
    names_by_id = {t.id: t.name for t in all_tasks}

    report = Report(generated_at=now.astimezone(timezone.utc), window_start=window_start,
                    window_end=window_end, upcoming_days=cfg.upcoming_days)
    sections: dict[str, list[Row]] = {k: [] for k in SECTION_KEYS}

    for task in all_tasks:
        if task.list_id not in cfg.list_ids:
            report.out_of_scope_count += 1
            continue
        if task.tags & cfg.exclude_tags:
            report.excluded_count += 1
            continue

        parent_name = None
        if task.parent_id:
            parent_name = names_by_id.get(task.parent_id) or f"(parent task {task.parent_id})"
        due = interpret_due(task.due, cfg)
        overdue_days = days_overdue(task, due, now, cfg.tz)
        row = Row(task=task, parent_name=parent_name, bucket=task.bucket, due=due,
                  due_local=format_due(due, cfg.tz), days_overdue=overdue_days)

        if any(a.id == cfg.admin_account_id for a in task.assignees):
            sections["admin_assigned"].append(row)

        if task.bucket == UNCLASSIFIED:
            sections["unclassified"].append(row)
            continue

        if task.is_completed:
            if window_start is not None:
                if task.done_at is None:
                    sections["completed_date_unknown"].append(row)
                elif window_start <= task.done_at < window_end:
                    sections["completed_in_window"].append(row)
            continue

        sections[task.bucket].append(row)
        if overdue_days is not None:
            sections[overdue_bucket(overdue_days)].append(row)
        elif is_due_within(task, due, now, cfg.tz, cfg.upcoming_days):
            sections["due_soon"].append(row)
        if task.due is None:
            sections["no_due_date"].append(row)
        if not task.assignees:
            sections["unassigned"].append(row)

    for key, rows in sections.items():
        rows.sort(key=_sort_key(key))
    report.sections = sections
    report.counts = {k: len(v) for k, v in sections.items()}
    report.counts["overdue_total"] = sum(report.counts[k] for k, _, _ in OVERDUE_BUCKETS)
    return report


def _sort_key(section: str):
    far = datetime.max.replace(tzinfo=timezone.utc)
    if section.startswith("overdue"):
        return lambda r: (-(r.days_overdue or 0), r.task.name.lower(), r.task.id)
    if section.startswith("completed"):
        return lambda r: (r.task.done_at or far, r.task.name.lower(), r.task.id)
    return lambda r: (r.due.day if r.due else date.max, r.task.due or far, r.task.name.lower(), r.task.id)


def weekly_window(now: datetime, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """The 7 full local days before the current local week: Monday 00:00 of
    last week (inclusive) to Monday 00:00 of this week (exclusive), in ``tz``,
    returned as aware datetimes. DST-safe: each bound is built from a local
    date, not by subtracting 168 hours."""
    local_today = now.astimezone(tz).date()
    this_monday = local_today - timedelta(days=local_today.weekday())
    last_monday = this_monday - timedelta(days=7)
    start = datetime.combine(last_monday, datetime.min.time(), tzinfo=tz)
    end = datetime.combine(this_monday, datetime.min.time(), tzinfo=tz)
    return start, end


def tasks_with_any_tag(rows: Sequence[Row], tags: frozenset[str]) -> list[Row]:
    return [r for r in rows if r.task.tags & tags]
