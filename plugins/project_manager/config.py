"""Loads and validates ``pm.yaml`` — the single source of every ClickUp id,
account id, tag and schedule the Captain plugin uses (PM_PLAN §4.1).

Location: ``<root hermes home>/pm/pm.yaml`` (``/opt/data/pm/pm.yaml`` in the
container), overridable with ``PM_CONFIG_PATH`` for tests and dry runs.

Strict by design: a missing or malformed required value raises
``PMConfigError`` naming the key. There are no silent defaults for ids,
timezones or recipients. Names in the file are display-only; logic matches by
id. Tokens are never read from this file.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import time
from pathlib import Path
from typing import Any, Mapping, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

from plugins.project_manager.rules import ReportConfig

_ID_RE = re.compile(r"^[0-9]+$")
_TAG_RE = re.compile(r"^[^\s].{0,99}$")
_TOPIC_KEY_RE = re.compile(r"^[a-z][a-z0-9_-]{0,39}$")
_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


class PMConfigError(ValueError):
    """pm.yaml is missing, unreadable, or fails validation."""


@dataclass(frozen=True)
class ScopeEntry:
    id: str
    name: str
    parent_id: Optional[str]


@dataclass(frozen=True)
class Schedule:
    weekday: int          # 0 = Monday
    at: time              # local time in the standard timezone
    catch_up_hours: int   # a missed run still sends within this many hours


@dataclass(frozen=True)
class PMConfig:
    path: Path
    workspace_id: str
    spaces: tuple[ScopeEntry, ...]
    lists: tuple[ScopeEntry, ...]
    admin_account_id: str
    bhanthe_account_id: str
    exclude_tags: frozenset[str]
    meeting_topics: Mapping[str, frozenset[str]]   # topic_key -> lower-cased tag names
    tz: ZoneInfo
    date_only_zones: tuple[ZoneInfo, ...]
    upcoming_days: int
    weekly_brief: Schedule
    meeting_reminder_at: time
    meeting_reminder_catch_up_hours: int
    agent_note_enabled: bool
    whatsapp_mode: str                 # "test" | "production"
    test_recipient_clickup_user_id: str
    approver_clickup_user_id: str
    approval_ttl_hours: int
    # People whose OWN delete requests run without the approver (Kusal, 2026-10-15). Optional.
    self_approved_delete_clickup_user_ids: frozenset[str]
    max_rows_per_section: int
    webapp_base_url: str

    @property
    def list_ids(self) -> frozenset[str]:
        return frozenset(e.id for e in self.lists)

    def report_config(self) -> ReportConfig:
        return ReportConfig(
            tz=self.tz, list_ids=self.list_ids, exclude_tags=self.exclude_tags,
            admin_account_id=self.admin_account_id, upcoming_days=self.upcoming_days,
            date_only_zones=self.date_only_zones,
        )


def default_config_path() -> Path:
    override = os.environ.get("PM_CONFIG_PATH")
    if override:
        return Path(override)
    from hermes_constants import get_default_hermes_root
    return get_default_hermes_root() / "pm" / "pm.yaml"


def _req(mapping: Mapping[str, Any], key: str, where: str) -> Any:
    if not isinstance(mapping, Mapping) or key not in mapping or mapping[key] in (None, ""):
        raise PMConfigError(f"pm.yaml: missing required key {where}.{key}")
    return mapping[key]


def _id(value: Any, where: str) -> str:
    text = str(value).strip()
    if not _ID_RE.match(text):
        raise PMConfigError(f"pm.yaml: {where} must be a numeric ClickUp id string, got {value!r}")
    return text


def _int(value: Any, where: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not (low <= value <= high):
        raise PMConfigError(f"pm.yaml: {where} must be an integer in [{low}, {high}], got {value!r}")
    return value


def _hhmm(value: Any, where: str) -> time:
    text = str(value).strip()
    m = re.fullmatch(r"([01][0-9]|2[0-3]):([0-5][0-9])", text)
    if not m:
        raise PMConfigError(f"pm.yaml: {where} must be HH:MM (24h), got {value!r}")
    return time(int(m.group(1)), int(m.group(2)))


def is_iana_zone(name: Any) -> bool:
    """True for a real IANA zone name like America/New_York (not "EST", not "UTC+5")."""
    text = str(name or "").strip()
    if "/" not in text or text.startswith("Etc/"):
        return False
    try:
        ZoneInfo(text)
    except (ZoneInfoNotFoundError, ValueError):
        return False
    return True


def _zone(value: Any, where: str) -> ZoneInfo:
    if not is_iana_zone(value):
        raise PMConfigError(f"pm.yaml: {where} must be an IANA zone name like America/New_York, got {value!r}")
    return ZoneInfo(str(value).strip())


def _tags(value: Any, where: str) -> frozenset[str]:
    if not isinstance(value, list):
        raise PMConfigError(f"pm.yaml: {where} must be a list of tag names")
    out = set()
    for tag in value:
        text = str(tag).strip()
        if not text or not _TAG_RE.match(text):
            raise PMConfigError(f"pm.yaml: {where} has an invalid tag {tag!r}")
        out.add(text.lower())
    return frozenset(out)


def _scope_entries(value: Any, where: str, parent_key: Optional[str], parent_ids: frozenset[str]) -> tuple[ScopeEntry, ...]:
    if not isinstance(value, list) or not value:
        raise PMConfigError(f"pm.yaml: {where} must be a non-empty list")
    entries: list[ScopeEntry] = []
    seen: set[str] = set()
    for i, item in enumerate(value):
        here = f"{where}[{i}]"
        entry_id = _id(_req(item, "id", here), f"{here}.id")
        if entry_id in seen:
            raise PMConfigError(f"pm.yaml: duplicate id {entry_id} in {where}")
        seen.add(entry_id)
        parent = None
        if parent_key:
            parent = _id(_req(item, parent_key, here), f"{here}.{parent_key}")
            if parent not in parent_ids:
                raise PMConfigError(f"pm.yaml: {here}.{parent_key}={parent} is not one of the configured ids")
        entries.append(ScopeEntry(id=entry_id, name=str(_req(item, "name", here)), parent_id=parent))
    return tuple(entries)


def parse_config(raw: Any, path: Path) -> PMConfig:
    if not isinstance(raw, Mapping):
        raise PMConfigError("pm.yaml: top level must be a mapping")
    clickup = _req(raw, "clickup", "")
    workspace_id = _id(_req(clickup, "workspace_id", "clickup"), "clickup.workspace_id")
    spaces = _scope_entries(_req(clickup, "spaces", "clickup"), "clickup.spaces", None, frozenset())
    lists = _scope_entries(_req(clickup, "lists", "clickup"), "clickup.lists", "space_id",
                           frozenset(s.id for s in spaces))
    accounts = _req(clickup, "accounts", "clickup")
    admin = _id(_req(accounts, "admin_account_id", "clickup.accounts"), "clickup.accounts.admin_account_id")
    bhanthe = _id(_req(accounts, "bhanthe_account_id", "clickup.accounts"), "clickup.accounts.bhanthe_account_id")
    if admin == bhanthe:
        raise PMConfigError("pm.yaml: admin_account_id and bhanthe_account_id must differ")

    tags = _req(clickup, "tags", "clickup")
    exclude = _tags(tags.get("exclude_from_reports", []), "clickup.tags.exclude_from_reports")
    topics_raw = tags.get("meeting_topics") or {}
    if not isinstance(topics_raw, Mapping):
        raise PMConfigError("pm.yaml: clickup.tags.meeting_topics must be a mapping of topic_key -> [tag names]")
    topics: dict[str, frozenset[str]] = {}
    for key, value in topics_raw.items():
        if not _TOPIC_KEY_RE.match(str(key)):
            raise PMConfigError(f"pm.yaml: meeting topic key {key!r} must match {_TOPIC_KEY_RE.pattern}")
        topic_tags = _tags(value, f"clickup.tags.meeting_topics.{key}")
        if not topic_tags:
            raise PMConfigError(f"pm.yaml: meeting topic {key!r} has no tags")
        topics[str(key)] = topic_tags

    time_cfg = _req(raw, "time", "")
    tz = _zone(_req(time_cfg, "standard_timezone", "time"), "time.standard_timezone")
    zones_raw = _req(time_cfg, "date_only_zones", "time")
    if not isinstance(zones_raw, list) or not zones_raw:
        raise PMConfigError("pm.yaml: time.date_only_zones must be a non-empty list of IANA zone names")
    date_only_zones = tuple(_zone(z, "time.date_only_zones") for z in zones_raw)

    reports = _req(raw, "reports", "")
    upcoming = _int(_req(reports, "upcoming_days", "reports"), "reports.upcoming_days", 1, 90)
    wb = _req(reports, "weekly_brief", "reports")
    weekday_name = str(_req(wb, "weekday", "reports.weekly_brief")).strip().lower()
    if weekday_name not in _WEEKDAYS:
        raise PMConfigError(f"pm.yaml: reports.weekly_brief.weekday must be one of {_WEEKDAYS}")
    weekly = Schedule(
        weekday=_WEEKDAYS.index(weekday_name),
        at=_hhmm(_req(wb, "at", "reports.weekly_brief"), "reports.weekly_brief.at"),
        catch_up_hours=_int(_req(wb, "catch_up_hours", "reports.weekly_brief"), "reports.weekly_brief.catch_up_hours", 0, 72),
    )
    mr = _req(reports, "meeting_reminder", "reports")
    reminder_at = _hhmm(_req(mr, "at", "reports.meeting_reminder"), "reports.meeting_reminder.at")
    reminder_catch_up = _int(_req(mr, "catch_up_hours", "reports.meeting_reminder"),
                             "reports.meeting_reminder.catch_up_hours", 0, 23)
    agent_note = reports.get("agent_note_enabled", False)
    if not isinstance(agent_note, bool):
        raise PMConfigError("pm.yaml: reports.agent_note_enabled must be true or false")
    max_rows = _int(_req(reports, "max_rows_per_section", "reports"), "reports.max_rows_per_section", 1, 200)
    webapp = str(_req(reports, "webapp_base_url", "reports")).rstrip("/")
    if not webapp.startswith("https://"):
        raise PMConfigError("pm.yaml: reports.webapp_base_url must be an https:// URL")

    wa = _req(raw, "whatsapp", "")
    mode = str(_req(wa, "mode", "whatsapp")).strip().lower()
    if mode not in ("test", "production"):
        raise PMConfigError("pm.yaml: whatsapp.mode must be 'test' or 'production'")
    test_recipient = _id(_req(wa, "test_recipient_clickup_user_id", "whatsapp"), "whatsapp.test_recipient_clickup_user_id")

    approvals = _req(raw, "approvals", "")
    approver = _id(_req(approvals, "approver_clickup_user_id", "approvals"), "approvals.approver_clickup_user_id")
    ttl = _int(_req(approvals, "ttl_hours", "approvals"), "approvals.ttl_hours", 1, 24 * 14)
    self_deleters_raw = approvals.get("self_approved_delete_clickup_user_ids", [])
    if not isinstance(self_deleters_raw, list):
        raise PMConfigError("pm.yaml: approvals.self_approved_delete_clickup_user_ids must be a list")
    self_deleters = frozenset(_id(u, "approvals.self_approved_delete_clickup_user_ids[]") for u in self_deleters_raw)
    if admin in self_deleters:
        raise PMConfigError("pm.yaml: the shared admin account cannot be in self_approved_delete_clickup_user_ids")

    return PMConfig(
        path=path, workspace_id=workspace_id, spaces=spaces, lists=lists,
        admin_account_id=admin, bhanthe_account_id=bhanthe, exclude_tags=exclude,
        meeting_topics=topics, tz=tz, date_only_zones=date_only_zones, upcoming_days=upcoming, weekly_brief=weekly,
        meeting_reminder_at=reminder_at, meeting_reminder_catch_up_hours=reminder_catch_up,
        agent_note_enabled=agent_note, whatsapp_mode=mode,
        test_recipient_clickup_user_id=test_recipient, approver_clickup_user_id=approver,
        approval_ttl_hours=ttl, self_approved_delete_clickup_user_ids=self_deleters, max_rows_per_section=max_rows, webapp_base_url=webapp,
    )


def load_config(path: Optional[Path] = None) -> PMConfig:
    path = path or default_config_path()
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise PMConfigError(f"pm.yaml not found at {path}") from exc
    except (OSError, yaml.YAMLError) as exc:
        raise PMConfigError(f"pm.yaml at {path} could not be read: {exc}") from exc
    return parse_config(raw, path)


def is_configured() -> bool:
    """Cheap gate for tool/hook visibility: the file exists. Validation errors
    surface on first real use with the key name, not as a hidden tool."""
    try:
        return default_config_path().is_file()
    except Exception:
        return False
