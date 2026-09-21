#!/usr/bin/env python3
"""Social Post Pipeline v2 — one cron script, two independent timers.

Per ``extra/plans/socialpost/spp-v2-postgres-and-notifications-plan.md``
(VIDUOPS repo), §"One cron script, two independent timers, no new
scheduler": every tick does two unrelated checks against Postgres —

  (a) release any ``post_submissions`` review-claim lock older than
      ``--lock-ttl-seconds`` (default 1 hour) and notify the submission's
      publisher + every admin curator that it was released;
  (b) find every submission that still has a non-terminal platform draft
      (i.e. genuinely still needs review — not fully approved/rejected)
      whose 2-hour reminder is due (``last_reminder_at`` is null, or older
      than ``--reminder-interval-seconds``, default 2 hours) and send the
      "review is waiting" nudge with the actual review link to the shared
      group, then stamp ``last_reminder_at``.

This is a plain, deterministic script — **no LLM call, no agent turn** — the
"no_agent script job type" this plan doc's §6 points at
(``hermes_cli/cron.py``'s ``no_agent`` + ``script`` job type: it delivers a
script's stdout with zero LLM/agent turn). Intended registration (**not**
run by this build session — a live-system change is a human-triggered
action per VIDUOPS/CLAUDE.md, not something to script through):

    hermes cron create --no-agent --script spp_review_sweep.py \\
        --schedule "*/15 * * * *"

(every 15 minutes — tighter than the 2hr reminder cadence, since the 1hr
lock TTL needs a finer sweep interval to actually fire close to on-time, per
the plan doc). The script must live under ``HERMES_HOME/scripts/`` on the
live system for ``hermes_cli/cron.py``'s script-resolution rule to accept
it; this worktree keeps it at the equivalent repo-relative path,
``scripts/spp_review_sweep.py``, per this build's instructions.
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

from plugins.platforms.event_post_pipeline import db, whatsapp_notify  # noqa: E402

logger = logging.getLogger("spp_review_sweep")


def _load_extra() -> dict:
    from hermes_cli.config import load_config

    platforms = (load_config() or {}).get("platforms") or {}
    return dict((platforms.get("event_post_pipeline") or {}).get("extra") or {})


DEFAULT_REVIEW_BASE_URL = "https://spp.buddhameditationdc.org"


def _review_link(submission: dict, review_base_url: str) -> str:
    slug = submission.get("slug", "")
    return f"{review_base_url.rstrip('/')}/review/{slug}"


def _lock_timeout_message(submission: dict, review_base_url: str) -> str:
    return (
        f"Review claim on {_review_link(submission, review_base_url)} was released after "
        f"sitting locked for over an hour with no action taken. It's open for review again."
    )


def _reminder_message(submission: dict, review_base_url: str) -> str:
    return f"Reminder: the review for {_review_link(submission, review_base_url)} is still waiting."


def sweep_lock_timeouts(conn, *, lock_ttl_seconds: int, review_base_url: str = DEFAULT_REVIEW_BASE_URL) -> list[dict[str, Any]]:
    """Releases every expired lock and notifies the shared social-media group for each
    (decided 2026-09-21: every pipeline notification goes to the group, not an individual
    curator's DM — the team wants shared visibility into all activity, not fragmented
    per-person messages). Returns the released rows (for the caller's summary/tests)."""
    released = db.release_expired_locks(conn, ttl_seconds=lock_ttl_seconds)
    for submission in released:
        asyncio.run(whatsapp_notify.send_whatsapp_link(_lock_timeout_message(submission, review_base_url)))
    return released


def sweep_pending_reminders(conn, *, reminder_interval_seconds: int, review_base_url: str = DEFAULT_REVIEW_BASE_URL) -> list[dict[str, Any]]:
    """Sends the "review is waiting" nudge to the shared group for every submission whose
    reminder is due, then stamps ``last_reminder_at`` — only after a successful send attempt
    (a delivery *failure* still stamps, matching a cron script's "best effort, don't jam the
    queue on one bad number" expectations). ``db.find_due_reminders`` already restricts this
    to submissions that genuinely still have a non-terminal platform draft — see its
    docstring for the 2026-09-21 bug this fixes (it used to fire for every submission ever
    created, including fully approved ones, months old)."""
    due = db.find_due_reminders(conn, interval_seconds=reminder_interval_seconds)
    for submission in due:
        asyncio.run(whatsapp_notify.send_whatsapp_link(_reminder_message(submission, review_base_url)))
        db.stamp_reminder_sent(conn, submission["id"])
    return due


def run_sweep(conn, *, lock_ttl_seconds: int, reminder_interval_seconds: int, review_base_url: str = DEFAULT_REVIEW_BASE_URL) -> dict[str, int]:
    """Both independent checks, one tick. Returns a small summary dict for logging/tests."""
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
        print("spp_review_sweep: no SPP_DATABASE_URL configured; nothing to do.")
        return 0

    try:
        conn = db.get_connection(database_url)
    except db.DatabaseError as exc:
        print(f"spp_review_sweep: could not connect to Postgres: {exc}", file=sys.stderr)
        return 1
    review_base_url = str(extra.get("review_base_url") or DEFAULT_REVIEW_BASE_URL)
    try:
        summary = run_sweep(
            conn, lock_ttl_seconds=args.lock_ttl_seconds, reminder_interval_seconds=args.reminder_interval_seconds,
            review_base_url=review_base_url,
        )
    finally:
        conn.close()
    print(f"spp_review_sweep: released {summary['locks_released']} expired lock(s), "
          f"sent {summary['reminders_sent']} pending-review reminder(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
