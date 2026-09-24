"""Postgres client for the newsletter pipeline's curator resolution and
pipeline-visualizer tracking.

Scope: ``social_media_curators`` upsert/lookup (the same table, same
identity mechanism as event_post_pipeline — a newsletter curator and an
event-post curator are the same person pool), plus writes to
``pipeline_runs``/``pipeline_events`` (the same visualizer tables
event_post_pipeline uses — see
extra/plans/newsletter/migrations/0002_newsletter_visualizer.sql, which
added a ``newsletter_issue_id`` column alongside the existing
``submission_id`` since a newsletter issue is not a row of
``post_submissions``). Newsletter issue/draft rows themselves are still
created and updated exclusively through ``review_client.py``'s HTTP
contract with social-post-portal, never written here — social-post-portal
owns that side of the same Postgres instance, same ownership boundary
event_post_pipeline.db documents for pipeline_runs/pipeline_events.

Driver: ``psycopg2`` (sync), imported lazily — same convention as
``event_post_pipeline.db``.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

from plugins.platforms.newsletter_pipeline import security

logger = logging.getLogger("plugins.platforms.newsletter_pipeline")

# Mirrors social-post-portal's lib/newsletter.ts LOCK_TTL (24hr, deliberately
# longer than event_post_pipeline's 1hr — a newsletter review is lower-
# frequency/higher-stakes, see newsletter-pipeline-plan.md's "Open items").
DEFAULT_LOCK_TTL_SECONDS = 24 * 60 * 60
DEFAULT_REMINDER_INTERVAL_SECONDS = 2 * 60 * 60

try:
    import psycopg2
    import psycopg2.extras
    PSYCOPG2_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only when the dep is truly missing
    psycopg2 = None  # type: ignore[assignment]
    PSYCOPG2_AVAILABLE = False


class DatabaseError(RuntimeError):
    """Raised for a missing/misconfigured connection or a failed query."""


def check_db_requirements() -> bool:
    return PSYCOPG2_AVAILABLE


def resolve_database_url(extra: Optional[dict] = None) -> str:
    """``extra.database_url`` (``${VAR}``-resolved) if explicitly configured,
    else ``SPP_DATABASE_URL`` — the same env var event_post_pipeline uses,
    since both plugins talk to the same ``spp`` Postgres database."""
    explicit = (extra or {}).get("database_url")
    if explicit:
        return security.resolve_env_secret(explicit)
    return os.environ.get("SPP_DATABASE_URL", "")


def get_connection(database_url: Optional[str] = None):
    if not PSYCOPG2_AVAILABLE:
        raise DatabaseError("psycopg2 is not installed; add 'psycopg2-binary' to run the newsletter Postgres path")
    url = database_url or resolve_database_url()
    if not url:
        raise DatabaseError("no SPP_DATABASE_URL configured (env var or extra.database_url in config.yaml)")
    try:
        conn = psycopg2.connect(url, cursor_factory=psycopg2.extras.RealDictCursor)
    except Exception as exc:
        raise DatabaseError(f"could not connect to Postgres: {exc}") from exc
    conn.autocommit = True
    return conn


def _row_to_dict(row: Optional[dict]) -> Optional[dict[str, Any]]:
    return dict(row) if row is not None else None


def get_curator(conn, curator_id: int) -> Optional[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM social_media_curators WHERE id = %s", (curator_id,))
        return _row_to_dict(cur.fetchone())


def upsert_curator(
    conn, phone_number: str, name: str, *, is_publisher: Optional[bool] = None, is_reviewer: Optional[bool] = None,
) -> dict[str, Any]:
    """Insert-or-update, keyed on ``phone_number`` — mirrors
    ``event_post_pipeline.db.upsert_curator``, same table, same
    OR-not-overwrite role-flag semantics."""
    if not phone_number or not str(phone_number).strip():
        raise DatabaseError("upsert_curator requires a non-empty phone_number")
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO social_media_curators (phone_number, name, is_publisher, is_reviewer)
            VALUES (%(phone)s, %(name)s, COALESCE(%(is_publisher)s, FALSE), COALESCE(%(is_reviewer)s, FALSE))
            ON CONFLICT (phone_number) DO UPDATE SET
                name = EXCLUDED.name,
                is_publisher = social_media_curators.is_publisher OR COALESCE(%(is_publisher)s, FALSE),
                is_reviewer = social_media_curators.is_reviewer OR COALESCE(%(is_reviewer)s, FALSE),
                updated_at = now()
            RETURNING *
            """,
            {"phone": phone_number, "name": name, "is_publisher": is_publisher, "is_reviewer": is_reviewer},
        )
        return _row_to_dict(cur.fetchone())


# --- pipeline_runs / pipeline_events: visualizer tracking --------------------------------
#
# Ownership boundary (mirrors event_post_pipeline.db's docstring): this plugin only ever
# INSERTs/UPSERTs into these two tables — it never deletes rows and never sets state='done'.
# Marking a run fully complete is social-post-portal's job (lib/pipeline-runs.ts's
# retireCompletedRun), since only it knows when the newsletter issue has reached a terminal
# status. pipeline_events has a FK to pipeline_runs.task_id, so every record_pipeline_event
# call site must have already called upsert_pipeline_run for that task_id.


def upsert_pipeline_run(conn, task_id: str, *, title: str, state: str, current_step: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO pipeline_runs (task_id, title, state, current_step)
            VALUES (%(task_id)s, %(title)s, %(state)s, %(current_step)s)
            ON CONFLICT (task_id) DO UPDATE SET
                title = EXCLUDED.title,
                state = EXCLUDED.state,
                current_step = EXCLUDED.current_step,
                updated_at = now()
            """,
            {"task_id": task_id, "title": title, "state": state, "current_step": current_step},
        )


def record_pipeline_event(
    conn, task_id: str, step: str, status: str, *, detail: Optional[str] = None, actor: Optional[str] = None,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO pipeline_events (task_id, step, status, detail, actor) VALUES (%s, %s, %s, %s, %s)",
            (task_id, step, status, detail, actor),
        )


def get_newsletter_issue_id_by_slug(conn, slug: str) -> Optional[int]:
    """Used only to backfill ``pipeline_runs.newsletter_issue_id`` once
    social-post-portal's own ``newsletter_issues`` row exists (it doesn't yet
    at intake time — mirrors event_post_pipeline.db.get_submission_id_by_slug)."""
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM newsletter_issues WHERE slug = %s", (slug,))
        row = cur.fetchone()
        return row["id"] if row else None


def link_newsletter_issue_id(conn, task_id: str, newsletter_issue_id: int) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE pipeline_runs SET newsletter_issue_id = %s WHERE task_id = %s",
            (newsletter_issue_id, task_id),
        )


# --- newsletter_issues: review-claim lock + reminder sweep -------------------------------
#
# Mirrors event_post_pipeline.db's post_submissions lock/reminder functions, same shapes,
# against newsletter_issues instead. Read-only into newsletter_drafts for reminder
# eligibility — drafts are still only ever written through review_client.py's HTTP contract.


def release_expired_locks(conn, ttl_seconds: int = DEFAULT_LOCK_TTL_SECONDS) -> list[dict[str, Any]]:
    """Releases every newsletter_issues review-claim lock older than ``ttl_seconds`` and
    returns the affected rows *before* release — same CAS-flavored single
    ``UPDATE ... RETURNING *`` as event_post_pipeline.db.release_expired_locks."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE newsletter_issues SET locked_by = NULL, locked_at = NULL, updated_at = now() "
            "WHERE locked_by IS NOT NULL AND locked_at < now() - %s::interval "
            "RETURNING *",
            (f"{int(ttl_seconds)} seconds",),
        )
        return [dict(row) for row in cur.fetchall()]


def find_due_reminders(conn, interval_seconds: int = DEFAULT_REMINDER_INTERVAL_SECONDS) -> list[dict[str, Any]]:
    """Issues whose latest draft round is still non-terminal (pending or
    refine_requested — not yet approved/rejected) and whose reminder is due:
    never reminded, or last reminded more than ``interval_seconds`` ago."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT i.* FROM newsletter_issues i
            WHERE EXISTS (
                SELECT 1 FROM newsletter_drafts d
                WHERE d.issue_id = i.id
                  AND d.status IN ('pending', 'refine_requested')
                  AND d.round = (SELECT MAX(round) FROM newsletter_drafts WHERE issue_id = i.id)
            )
            AND (i.last_reminder_at IS NULL OR i.last_reminder_at < now() - %s::interval)
            """,
            (f"{int(interval_seconds)} seconds",),
        )
        return [dict(row) for row in cur.fetchall()]


def stamp_reminder_sent(conn, issue_id: int) -> None:
    with conn.cursor() as cur:
        cur.execute("UPDATE newsletter_issues SET last_reminder_at = now(), updated_at = now() WHERE id = %s", (issue_id,))
