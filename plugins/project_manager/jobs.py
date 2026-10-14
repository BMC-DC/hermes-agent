"""Command-line entry for the ProjectManager scheduled jobs and operator checks.

    python -m plugins.project_manager.jobs <command> [--dry-run] [--force] [--now ISO8601]

Commands:
  tick               every scheduled duty in one pass: approval executor, meeting
                     reminders (only inside the reminder slot), weekly brief (only
                     inside its slot). This is what the Hermes no-agent cron job runs
                     every minute (/opt/data/scripts/pm_tick.py).
  weekly-brief       build (and unless --dry-run, store + send) the weekly brief.
                     --force ignores the schedule slot.
  meeting-reminders  remind tomorrow's meetings. --force ignores the slot.
  execute-approvals  run approved changes; expire stale pending ones.
  sync-scope         write pm.yaml's spaces/lists into pm.clickup_scope (webapp).
  seed-people FILE   insert people from a YAML seed file (never overwrites rows).
  check              validate pm.yaml against live ClickUp, the DB and config.yaml.
                     Read-only. Prints no task names, numbers or secrets.

Schedules are decided here in America/New_York (pm.yaml), not by cron's own
timezone, so DST and the container's TZ can never shift a send. Output is
printed only when something happened (a no-agent cron job delivers stdout).
Exit status is non-zero if any part failed.
"""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import sys
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator, Optional

import yaml

from plugins.project_manager import config as pm_config
from plugins.project_manager import messaging, rules, service, store
from plugins.project_manager.clickup import ClickUpClient, ClickUpError, token_from_env

KNOWN_STATUS_TYPES = {"open", "custom", "done", "closed"}


def _now(value: Optional[str]) -> datetime:
    if not value:
        return service.utcnow()
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise SystemExit("--now must include a UTC offset, e.g. 2026-10-19T13:05:00+00:00")
    return parsed


@contextmanager
def _lock(cfg: pm_config.PMConfig) -> Iterator[bool]:
    path = cfg.path.parent / ".pm_tick.lock"
    with open(path, "a+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _print_outcomes(label: str, outcomes, verbose: bool) -> None:
    for o in outcomes:
        if verbose or o.action not in ("not_due", "no_meetings", "skipped_duplicate", "already_sent"):
            print(f"[pm {label}] {o.line()}")
        if o.text and verbose:
            print("----- message -----\n" + o.text + "\n-------------------")


async def _tick(cfg, now: datetime, dry_run: bool, verbose: bool) -> int:
    failures = 0
    with store.connection() as conn:
        for label, coro in (
            ("executor", lambda: service.run_executor(cfg, conn, now=now, dry_run=dry_run)),
            ("reminders", lambda: service.run_meeting_reminders(cfg, conn, now=now, dry_run=dry_run)),
            ("weekly-brief", lambda: service.run_weekly_brief(cfg, conn, now=now, dry_run=dry_run)),
        ):
            try:
                result = await coro()
                outcomes = result if isinstance(result, list) else [result]
                _print_outcomes(label, outcomes, verbose)
                failures += sum(1 for o in outcomes if o.action in ("failed", "gave_up"))
            except Exception as exc:
                failures += 1
                try:
                    conn.rollback()
                except Exception:
                    pass
                print(f"[pm {label}] ERROR {type(exc).__name__}: {exc}")
    return 1 if failures else 0


def cmd_check(cfg: pm_config.PMConfig) -> int:
    problems: list[str] = []
    ok: list[str] = []
    try:
        client = ClickUpClient(token_from_env())
    except ClickUpError as exc:
        print(f"FAIL clickup token: {exc}")
        return 1
    for space in cfg.spaces:
        try:
            data = client.get_space(space.id)
            ok.append(f"space {space.id} exists (live name {data.get('name')!r})")
        except ClickUpError as exc:
            problems.append(f"space {space.id}: {exc}")
    for lst in cfg.lists:
        try:
            data = client.get_list(lst.id)
        except ClickUpError as exc:
            problems.append(f"list {lst.id}: {exc}")
            continue
        space_id = str((data.get("space") or {}).get("id"))
        if space_id != lst.parent_id:
            problems.append(f"list {lst.id} is in space {space_id}, pm.yaml says {lst.parent_id}")
        types = {str(s.get("type")) for s in data.get("statuses") or []}
        if not types <= KNOWN_STATUS_TYPES:
            problems.append(f"list {lst.id} has unknown status types {sorted(types - KNOWN_STATUS_TYPES)}")
        ok.append(f"list {lst.id} ok, status types {sorted(types)}")
    try:
        tasks = client.get_team_tasks(cfg.workspace_id)
        report = rules.build_report(tasks, cfg.report_config(), now=service.utcnow())
        ok.append(f"read {len(tasks)} tasks; counts {report.counts}; out of scope {report.out_of_scope_count}; "
                  f"excluded {report.excluded_count}")
        if report.out_of_scope_count:
            problems.append(f"{report.out_of_scope_count} tasks are in lists not listed in pm.yaml")
        undated = sum(1 for t in tasks if t.get("due_date") and rules.interpret_due(
            rules.Task.from_api(t).due, cfg.report_config()).has_time)
        ok.append(f"{undated} due dates carry a real time (not 04:00 in a date_only_zone)")
    except Exception as exc:
        problems.append(f"task read/report: {type(exc).__name__}: {exc}")
    try:
        with store.connection() as conn:
            with conn, conn.cursor() as cur:
                cur.execute("SELECT count(*) AS n FROM information_schema.tables WHERE table_schema = 'pm'")
                n = cur.fetchone()["n"]
            ok.append(f"database reachable; schema pm has {n} tables")
            for label, uid in (("test recipient", cfg.test_recipient_clickup_user_id),
                               ("approver", cfg.approver_clickup_user_id)):
                person = store.person_by_clickup_id(conn, uid)
                if person is None:
                    problems.append(f"{label} (ClickUp {uid}) has no pm.people row")
                elif not person.get("whatsapp_number"):
                    problems.append(f"{label} ({person['display_name']}) has no WhatsApp number in pm.people")
                else:
                    ok.append(f"{label} {person['display_name']} has a WhatsApp number")
    except Exception as exc:
        problems.append(f"database: {type(exc).__name__}: {exc}")
    try:
        messaging.management_group_chat_id()
        ok.append("management group (home_channel) is a group JID")
    except Exception as exc:
        problems.append(f"management group: {exc}")
    for line in ok:
        print(f"ok   {line}")
    for line in problems:
        print(f"FAIL {line}")
    print(f"mode={cfg.whatsapp_mode}; {len(problems)} problem(s)")
    return 1 if problems else 0


def cmd_seed_people(path: Path) -> int:
    seed = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    people = seed.get("people")
    if not isinstance(people, list):
        raise SystemExit("seed file needs a top-level 'people' list")
    with store.connection() as conn:
        for p in people:
            tz = str(p.get("timezone") or "")
            if not pm_config.is_iana_zone(tz):
                raise SystemExit(f"{p.get('display_name')!r}: timezone {tz!r} is not an IANA zone name")
            if "whatsapp_number" in p:
                raise SystemExit("seed files must not carry phone numbers; enter them in the webapp")
            new_id = store.insert_person_if_absent(
                conn, display_name=str(p["display_name"]), clickup_user_id=(str(p["clickup_user_id"])
                                                                            if p.get("clickup_user_id") else None),
                timezone=tz, country=p.get("country"), role=p.get("role"), actor="pm-seed")
            print(f"{'created' if new_id else 'exists '} {p['display_name']}")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="pm-jobs")
    parser.add_argument("command", choices=["tick", "weekly-brief", "meeting-reminders", "execute-approvals",
                                            "sync-scope", "seed-people", "check"])
    parser.add_argument("file", nargs="?")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--now")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    cfg = pm_config.load_config()
    now = _now(args.now)
    verbose = args.verbose or args.dry_run

    if args.command == "check":
        return cmd_check(cfg)
    if args.command == "seed-people":
        if not args.file:
            raise SystemExit("seed-people needs a FILE")
        return cmd_seed_people(Path(args.file))
    if args.command == "sync-scope":
        entries = [("workspace", cfg.workspace_id, "Workspace", None)]
        entries += [("space", s.id, s.name, cfg.workspace_id) for s in cfg.spaces]
        entries += [("list", l.id, l.name, l.parent_id) for l in cfg.lists]
        if args.dry_run:
            print(f"[pm sync-scope] dry run: {len(entries)} entries")
            return 0
        with store.connection() as conn:
            print(f"[pm sync-scope] {store.sync_scope(conn, entries, 'pm-sync-scope')}")
        return 0
    if args.command == "tick":
        with _lock(cfg) as acquired:
            if not acquired:
                return 0   # previous tick still running
            return asyncio.run(_tick(cfg, now, args.dry_run, verbose))

    async def single() -> int:
        if args.command == "weekly-brief" and args.dry_run:
            # A dry-run brief needs only ClickUp, so it works before the migration is applied.
            outcomes = [await service.run_weekly_brief(cfg, None, now=now, dry_run=True, force=args.force)]
            _print_outcomes(args.command, outcomes, True)
            return 0
        with store.connection() as conn:
            if args.command == "weekly-brief":
                outcomes = [await service.run_weekly_brief(cfg, conn, now=now, dry_run=args.dry_run, force=args.force)]
            elif args.command == "meeting-reminders":
                outcomes = await service.run_meeting_reminders(cfg, conn, now=now, dry_run=args.dry_run, force=args.force)
            else:
                outcomes = await service.run_executor(cfg, conn, now=now, dry_run=args.dry_run)
        _print_outcomes(args.command, outcomes, True)
        return 1 if any(o.action in ("failed", "gave_up") for o in outcomes) else 0

    return asyncio.run(single())


if __name__ == "__main__":
    sys.exit(main())
