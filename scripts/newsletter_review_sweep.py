#!/usr/bin/env python3
"""Newsletter pipeline — one cron script, two independent timers.

Mirrors ``scripts/spp_review_sweep.py`` exactly in shape, against
``newsletter_issues``/``newsletter_drafts`` instead of
``post_submissions``/``post_platform_drafts``. Every tick does two
unrelated checks against Postgres —

  (a) release any ``newsletter_issues`` review-claim lock older than
      ``--lock-ttl-seconds`` (default 24 hours — see
      ``plugins/platforms/newsletter_pipeline/db.py``'s
      ``DEFAULT_LOCK_TTL_SECONDS`` docstring for why this is longer than
      the event-post pipeline's 1 hour) and notify the shared group it's
      open for review again;
  (b) find every issue whose latest draft round is still non-terminal
      (genuinely still needs review) whose 2-hour reminder is due and send
      the "review is waiting" nudge with the actual review link, then
      stamp ``last_reminder_at``.

Plain, deterministic script — no LLM call, no agent turn (the ``no_agent``
script job type, same as ``spp_review_sweep.py``). Intended registration
(**not** run by this build session — a live-system change is a
human-triggered action per VIDUOPS/CLAUDE.md):

    hermes cron create --no-agent --script newsletter_review_sweep.py \\
        --schedule "*/15 * * * *"

The script must live under ``HERMES_HOME/scripts/`` on the live system for
``hermes_cli/cron.py``'s script-resolution rule to accept it; this worktree
keeps it at the equivalent repo-relative path, matching
``spp_review_sweep.py``'s own convention.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from plugins.platforms.newsletter_pipeline import db, whatsapp_notify  # noqa: E402

logger = logging.getLogger("newsletter_review_sweep")


def _load_extra() -> dict:
    from hermes_cli.config import load_config

    platforms = (load_config() or {}).get("platforms") or {}
    return dict((platforms.get("newsletter_pipeline") or {}).get("extra") or {})


DEFAULT_REVIEW_BASE_URL = "https://spp.buddhameditationdc.org"


def _review_link(issue: dict, review_base_url: str) -> str:
    slug = issue.get("slug", "")
    return f"{review_base_url.rstrip('/')}/review/newsletter/{slug}"


def _lock_timeout_message(issue: dict, review_base_url: str) -> str:
    return (
        f"Review claim on {_review_link(issue, review_base_url)} was released after "
        f"sitting locked for over 24 hours with no action taken. It's open for review again."
    )


def _reminder_message(issue: dict, review_base_url: str) -> str:
    return f"Reminder: the newsletter review for {_review_link(issue, review_base_url)} is still waiting."


def sweep_lock_timeouts(conn, *, lock_ttl_seconds: int, review_base_url: str = DEFAULT_REVIEW_BASE_URL) -> list[dict[str, Any]]:
    """Releases every expired lock and notifies the shared group for each — same
    "shared visibility, not per-person DMs" convention as spp_review_sweep.py."""
    released = db.release_expired_locks(conn, ttl_seconds=lock_ttl_seconds)
    for issue in released:
        asyncio.run(whatsapp_notify.send_whatsapp_link(_lock_timeout_message(issue, review_base_url)))
    return released


def sweep_pending_reminders(conn, *, reminder_interval_seconds: int, review_base_url: str = DEFAULT_REVIEW_BASE_URL) -> list[dict[str, Any]]:
    """Sends the "review is waiting" nudge for every issue whose reminder is due, then
    stamps ``last_reminder_at`` — a delivery failure still stamps, matching a cron
    script's "best effort, don't jam the queue on one bad send" expectations."""
    due = db.find_due_reminders(conn, interval_seconds=reminder_interval_seconds)
    for issue in due:
        asyncio.run(whatsapp_notify.send_whatsapp_link(_reminder_message(issue, review_base_url)))
        db.stamp_reminder_sent(conn, issue["id"])
    return due


def run_sweep(conn, *, lock_ttl_seconds: int, reminder_interval_seconds: int, review_base_url: str = DEFAULT_REVIEW_BASE_URL) -> dict[str, int]:
    released = sweep_lock_timeouts(conn, lock_ttl_seconds=lock_ttl_seconds, review_base_url=review_base_url)
    reminded = sweep_pending_reminders(conn, reminder_interval_seconds=reminder_interval_seconds, review_base_url=review_base_url)
    return {"locks_released": len(released), "reminders_sent": len(reminded)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock-ttl-seconds", type=int, default=db.DEFAULT_LOCK_TTL_SECONDS)
    parser.add_argument("--reminder-interval-seconds", type=int, default=db.DEFAULT_REMINDER_INTERVAL_SECONDS)
    args = parser.parse_args(argv)

    extra = _load_extra()
    database_url = db.resolve_database_url(extra)
    if not database_url:
        print("newsletter_review_sweep: no SPP_DATABASE_URL configured; nothing to do.")
        return 0

    try:
        conn = db.get_connection(database_url)
    except db.DatabaseError as exc:
        print(f"newsletter_review_sweep: could not connect to Postgres: {exc}", file=sys.stderr)
        return 1
    review_base_url = str(extra.get("review_base_url") or DEFAULT_REVIEW_BASE_URL)
    try:
        summary = run_sweep(
            conn, lock_ttl_seconds=args.lock_ttl_seconds, reminder_interval_seconds=args.reminder_interval_seconds,
            review_base_url=review_base_url,
        )
    finally:
        conn.close()
    print(f"newsletter_review_sweep: released {summary['locks_released']} expired lock(s), "
          f"sent {summary['reminders_sent']} pending-review reminder(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
