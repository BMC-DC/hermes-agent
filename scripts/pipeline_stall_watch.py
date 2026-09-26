#!/usr/bin/env python3
"""Personal stall/pause alert for in-flight event-post + newsletter pipeline runs.

One cron script, one check, covering **both** pipelines at once: ``pipeline_runs``/
``pipeline_events`` is a single shared Postgres table (event_post_pipeline rows carry
``submission_id``, newsletter_pipeline rows carry ``newsletter_issue_id`` — see
``extra/plans/newsletter/migrations/0002_newsletter_visualizer.sql``), so one query
against ``db.find_stalled_runs`` (``plugins/platforms/event_post_pipeline/db.py``)
already sees every non-terminal run from either pipeline, with no per-pipeline branch
needed.

Unlike ``spp_review_sweep.py``/``newsletter_review_sweep.py`` (shared-group nudges for
*normal* "review still pending" waiting), this is for a run that has gone quiet
mid-flight — no lifecycle event and no state change for ``--stall-seconds`` — which is
a signal something broke, not that a human just hasn't reviewed yet. That's why this
goes to a **personal** WhatsApp DM (Kusal), not the shared group: see this build's
2026-09-26 change commenting out the group's lifecycle/status messages — the group is
now review-links/reminders only, and this is deliberately the one message type that
still needs a named human to see it fast.

Deterministic, no LLM call, no agent turn — same "no_agent script job type" as the two
existing sweep scripts (``hermes_cli/cron.py``). The "job is only active while a
pipeline is running" requirement needs no separate enable/disable mechanism: it falls
directly out of ``find_stalled_runs``'s ``WHERE state <> 'done'`` filter (see that
function's docstring) — every tick is a no-op scan when nothing is stalled, this script
does not need to know whether zero, one, or many pipelines are currently in flight.

Intended registration (**not** run by this build session — a live-system change is a
human-triggered action per VIDUOPS/CLAUDE.md, not something to script through):

    hermes cron create --no-agent --script pipeline_stall_watch.py \\
        --schedule "*/10 * * * *"

The script must live under ``HERMES_HOME/scripts/`` on the live system for
``hermes_cli/cron.py``'s script-resolution rule to accept it; this worktree keeps it at
the equivalent repo-relative path, ``scripts/pipeline_stall_watch.py``.
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

logger = logging.getLogger("pipeline_stall_watch")

DEFAULT_REVIEW_BASE_URL = "https://spp.buddhameditationdc.org"

# Kusal's personal WhatsApp number (bare phone form — whatsapp_notify's
# send_whatsapp_message -> to_whatsapp_jid normalizes this to a JID itself, same as any
# other chat_id passed through this codebase). Overridable via
# platforms.event_post_pipeline.extra.personal_alert_chat_id in config.yaml without a
# code change, but this default keeps the watch working out of the box.
DEFAULT_PERSONAL_ALERT_CHAT_ID = "+94706575754"


def _load_extra() -> dict:
    from hermes_cli.config import load_config

    platforms = (load_config() or {}).get("platforms") or {}
    return dict((platforms.get("event_post_pipeline") or {}).get("extra") or {})


def _pipeline_label(run: dict) -> str:
    if run.get("submission_id") is not None:
        return "social post"
    if run.get("newsletter_issue_id") is not None:
        return "newsletter"
    return "pipeline"  # shouldn't happen — every row sets exactly one, per the schema comment


def _visualizer_link(run: dict, review_base_url: str) -> str:
    return f"{review_base_url.rstrip('/')}/visualizer?taskId={run['task_id']}"


def _stall_message(run: dict, *, stall_seconds: int, review_base_url: str) -> str:
    minutes = stall_seconds // 60
    return (
        f"⏸️ {_pipeline_label(run)} pipeline stalled: \"{run['title']}\" has had no "
        f"activity for over {minutes} minutes (stuck at step: {run['current_step']}). "
        f"Check it here: {_visualizer_link(run, review_base_url)}"
    )


def sweep_stalled_runs(
    conn, *, stall_seconds: int, personal_chat_id: str, review_base_url: str = DEFAULT_REVIEW_BASE_URL,
) -> list[dict[str, Any]]:
    """Sends the personal stall alert for every run ``find_stalled_runs`` returns, then
    records a ``stall_alert`` pipeline_event for each (this is what spaces out repeat
    alerts by ``stall_seconds`` — see ``find_stalled_runs``'s docstring). A send failure
    for one run must not skip recording the event or block the remaining runs — matches
    the existing sweep scripts' "best effort, don't jam the queue on one bad send"
    convention."""
    stalled = db.find_stalled_runs(conn, stall_seconds=stall_seconds)
    for run in stalled:
        message = _stall_message(run, stall_seconds=stall_seconds, review_base_url=review_base_url)
        try:
            asyncio.run(whatsapp_notify.send_whatsapp_message(personal_chat_id, message))
        except whatsapp_notify.WhatsAppNotifyError as exc:
            logger.error("pipeline_stall_watch: alert send failed for task=%s: %s", run["task_id"], exc)
        db.record_pipeline_event(conn, run["task_id"], "stall_alert", "info", detail=message)
    return stalled


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stall-seconds", type=int, default=db.DEFAULT_STALL_SECONDS)
    args = parser.parse_args(argv)

    extra = _load_extra()
    database_url = db.resolve_database_url(extra)
    if not database_url:
        print("pipeline_stall_watch: no SPP_DATABASE_URL configured; nothing to do.")
        return 0

    try:
        conn = db.get_connection(database_url)
    except db.DatabaseError as exc:
        print(f"pipeline_stall_watch: could not connect to Postgres: {exc}", file=sys.stderr)
        return 1
    review_base_url = str(extra.get("review_base_url") or DEFAULT_REVIEW_BASE_URL)
    personal_chat_id = str(extra.get("personal_alert_chat_id") or DEFAULT_PERSONAL_ALERT_CHAT_ID)
    try:
        stalled = sweep_stalled_runs(
            conn, stall_seconds=args.stall_seconds, personal_chat_id=personal_chat_id, review_base_url=review_base_url,
        )
    finally:
        conn.close()
    print(f"pipeline_stall_watch: {len(stalled)} stalled run(s) alerted.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
