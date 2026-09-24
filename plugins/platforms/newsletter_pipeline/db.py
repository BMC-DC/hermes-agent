"""Postgres client for the newsletter pipeline's curator resolution.

Scope, deliberately narrower than ``event_post_pipeline.db`` for this v1: only
``social_media_curators`` upsert/lookup (the same table, same identity
mechanism — a newsletter curator and an event-post curator are the same
person pool). Newsletter issue/draft rows are created and updated exclusively
through ``review_client.py``'s HTTP contract with social-post-portal, never
written here — social-post-portal owns that side of the same Postgres
instance.

Deliberately NOT here (see newsletter-pipeline-plan.md's "Open items"):
pipeline_runs/pipeline_events visualizer tracking. event_post_pipeline's
pipeline_runs.submission_id has a hard FK to post_submissions, which a
newsletter issue is not a row of — wiring newsletter issues into that same
visualizer table is a real design decision (a new nullable/polymorphic
column, or a parallel table) deferred to a later pass rather than bolted on
here.

Driver: ``psycopg2`` (sync), imported lazily — same convention as
``event_post_pipeline.db``.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

from plugins.platforms.newsletter_pipeline import security

logger = logging.getLogger("plugins.platforms.newsletter_pipeline")

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
