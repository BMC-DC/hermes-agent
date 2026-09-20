"""Postgres client for the v2 curator registry + review-claim lock fields.

Scope, per ``extra/plans/socialpost/spp-v2-postgres-and-notifications-plan.md``
(VIDUOPS repo) and ``extra/plans/socialpost/migrations/0001_init.sql``: this
plugin talks to Postgres **directly only for** ``social_media_curators`` (name/
phone/role-flag resolution) and the lock/reminder columns on
``post_submissions`` (``locked_by``, ``locked_at``, ``last_reminder_at``), plus
read-only access to ``post_platform_drafts`` for reminder/notification
messages. Submission/draft *creation* and the approve/reject/refine HTTP
contract stay exactly as they are today, routed through
``review_client.py``'s existing calls to ``social-post-portal`` (unchanged
per the plan doc's explicit recommendation) — ``social-post-portal`` is the
one that writes those rows on its own side of the same Postgres instance.

Driver: ``psycopg2`` (sync) — the existing convention already used elsewhere
in this repo for Postgres access (``plugins/memory/mem0/_backend.py``,
``_setup.py``), and consistent with this plugin's otherwise-sync style
(``review_client.py`` is plain ``urllib``, not async). Imported lazily, same
pattern as ``adapter.py``'s optional ``aiohttp`` import, so importing this
module never fails in a profile that doesn't use the plugin at all.

Connection target: the ``SPP_DATABASE_URL`` env var, resolved the same way
``security.py`` resolves other env-backed secrets (``${VAR}`` in
``config.yaml``, or the env var read directly when nothing is configured).

NOTE — deliberately out of scope here (see the plan doc's "Open items"):
the secure network path between Vercel (social-post-portal) and this VPS's
Postgres is *not decided yet*. That's irrelevant to this module: Hermes and
Postgres both run on the VPS, so this plugin's own connection is a local/
VPS-internal one and isn't blocked by the still-open Vercel-side question.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Optional

from plugins.platforms.event_post_pipeline import security

logger = logging.getLogger("plugins.platforms.event_post_pipeline")

DEFAULT_LOCK_TTL_SECONDS = 60 * 60          # 1 hour, per plan doc
DEFAULT_REMINDER_INTERVAL_SECONDS = 2 * 60 * 60  # 2 hours, per plan doc

try:
    import psycopg2
    import psycopg2.extras
    PSYCOPG2_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only when the dep is truly missing
    psycopg2 = None  # type: ignore[assignment]
    PSYCOPG2_AVAILABLE = False


class DatabaseError(RuntimeError):
    """Raised for a missing/misconfigured connection or a failed query — never
    silently swallowed, matching this plugin's ``PipelineError`` convention of
    surfacing problems rather than guessing past them."""


def check_db_requirements() -> bool:
    return PSYCOPG2_AVAILABLE


def resolve_database_url(extra: Optional[dict] = None) -> str:
    """``extra.database_url`` (``${VAR}``-resolved) if explicitly configured in
    ``config.yaml``, else ``SPP_DATABASE_URL`` read straight from the process
    environment — mirrors ``security.resolve_review_create_secret``'s
    "explicit config wins, else a well-known fallback" shape."""
    explicit = (extra or {}).get("database_url")
    if explicit:
        return security.resolve_env_secret(explicit)
    return os.environ.get("SPP_DATABASE_URL", "")


def get_connection(database_url: Optional[str] = None):
    """A new psycopg2 connection with dict-row cursors as the default factory.

    Callers are responsible for closing it (``with closing(get_connection()) as
    conn:`` or a plain ``try/finally`` — matching how ``adapter.py`` already
    manages the Kanban SQLite connection lifecycle)."""
    if not PSYCOPG2_AVAILABLE:
        raise DatabaseError("psycopg2 is not installed; add 'psycopg2-binary' to run the SPP v2 Postgres path")
    url = database_url or resolve_database_url()
    if not url:
        raise DatabaseError("no SPP_DATABASE_URL configured (env var or extra.database_url in config.yaml)")
    try:
        conn = psycopg2.connect(url, cursor_factory=psycopg2.extras.RealDictCursor)
    except Exception as exc:
        raise DatabaseError(f"could not connect to Postgres: {exc}") from exc
    conn.autocommit = True
    return conn


# --- social_media_curators ---------------------------------------------------------------


def _row_to_curator(row: Optional[dict]) -> Optional[dict[str, Any]]:
    return dict(row) if row is not None else None


def get_curator_by_phone(conn, phone_number: str) -> Optional[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM social_media_curators WHERE phone_number = %s", (phone_number,))
        return _row_to_curator(cur.fetchone())


def get_curator(conn, curator_id: int) -> Optional[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM social_media_curators WHERE id = %s", (curator_id,))
        return _row_to_curator(cur.fetchone())


def upsert_curator(
    conn,
    phone_number: str,
    name: str,
    *,
    is_publisher: Optional[bool] = None,
    is_reviewer: Optional[bool] = None,
    is_admin: Optional[bool] = None,
) -> dict[str, Any]:
    """Insert-or-update, keyed on ``phone_number`` (the only stable per-person key in a
    shared-password login system — see the plan doc's §Identity & locking). Role flags
    are only ever *added*, never silently cleared: an omitted (``None``) flag on a repeat
    submission leaves whatever the row already has, via ``COALESCE`` against the existing
    value on conflict; a ``True`` flag always sticks (once a publisher, still a publisher
    even if a later submission doesn't re-assert it)."""
    if not phone_number or not str(phone_number).strip():
        raise DatabaseError("upsert_curator requires a non-empty phone_number")
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO social_media_curators (phone_number, name, is_publisher, is_reviewer, is_admin)
            VALUES (%(phone)s, %(name)s, COALESCE(%(is_publisher)s, FALSE), COALESCE(%(is_reviewer)s, FALSE), COALESCE(%(is_admin)s, FALSE))
            ON CONFLICT (phone_number) DO UPDATE SET
                name = EXCLUDED.name,
                is_publisher = social_media_curators.is_publisher OR COALESCE(%(is_publisher)s, FALSE),
                is_reviewer = social_media_curators.is_reviewer OR COALESCE(%(is_reviewer)s, FALSE),
                is_admin = social_media_curators.is_admin OR COALESCE(%(is_admin)s, FALSE),
                updated_at = now()
            RETURNING *
            """,
            {"phone": phone_number, "name": name, "is_publisher": is_publisher, "is_reviewer": is_reviewer, "is_admin": is_admin},
        )
        return _row_to_curator(cur.fetchone())


def list_curators_by_role(
    conn, *, is_admin: bool = False, is_publisher: bool = False, is_reviewer: bool = False, active_only: bool = True,
) -> list[dict[str, Any]]:
    """Every active curator matching **any** of the requested role flags (OR, not AND) —
    e.g. ``list_curators_by_role(conn, is_publisher=True, is_admin=True)`` for the
    lock-timeout notification's "publisher + admin" recipient set."""
    clauses = [flag for flag, wanted in (("is_admin", is_admin), ("is_publisher", is_publisher), ("is_reviewer", is_reviewer)) if wanted]
    if not clauses:
        return []
    where = " OR ".join(clauses)
    sql = f"SELECT * FROM social_media_curators WHERE ({where})"
    if active_only:
        sql += " AND active"
    with conn.cursor() as cur:
        cur.execute(sql)
        return [dict(row) for row in cur.fetchall()]


# --- post_submissions: lock + reminder fields --------------------------------------------


def get_submission(conn, submission_id: int) -> Optional[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM post_submissions WHERE id = %s", (submission_id,))
        return _row_to_curator(cur.fetchone())


def get_submission_by_slug(conn, slug: str) -> Optional[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM post_submissions WHERE slug = %s", (slug,))
        return _row_to_curator(cur.fetchone())


def get_submission_by_task_id(conn, task_id: str) -> Optional[dict[str, Any]]:
    """Looks up a submission by its Kanban root ``task_id`` — this is the id every
    Hermes-side call site (``pipeline.py``/``hooks.py``) already has on hand, so
    notification code never needs the review-page slug just to resolve curators."""
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM post_submissions WHERE task_id = %s", (task_id,))
        return _row_to_curator(cur.fetchone())


@dataclass(frozen=True)
class ClaimResult:
    ok: bool
    submission: Optional[dict[str, Any]] = None  # the (now-locked) row on success
    locked_by: Optional[dict[str, Any]] = None    # the current holder's curator row on failure


def claim_submission(conn, submission_id: int, curator_id: int) -> ClaimResult:
    """The Kanban CAS claim pattern (``kanban_db_dispatch.py``'s
    ``UPDATE ... WHERE claim_lock IS NULL``), ported directly per the plan doc's §5:
    a single atomic ``UPDATE ... WHERE locked_by IS NULL RETURNING *`` — no
    read-then-write, no app-level mutex. Unlike Kanban's claim (which silently
    returns ``None`` on contention), this returns the current holder's identity on
    failure — the plan doc explicitly calls out that Kanban's silent-``None`` behavior
    should *not* be ported as-is for review locks, since a real "already being
    reviewed by X" response is wanted here."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE post_submissions SET locked_by = %s, locked_at = now(), updated_at = now() "
            "WHERE id = %s AND locked_by IS NULL RETURNING *",
            (curator_id, submission_id),
        )
        row = cur.fetchone()
        if row is not None:
            return ClaimResult(ok=True, submission=dict(row))
    submission = get_submission(conn, submission_id)
    holder = get_curator(conn, submission["locked_by"]) if submission and submission.get("locked_by") else None
    return ClaimResult(ok=False, submission=submission, locked_by=holder)


def release_lock(conn, submission_id: int) -> None:
    """Explicit release — the "taking an action releases the lock in the same
    transaction as the status update" half of the plan doc's two release paths (the
    other half, TTL expiry, is ``release_expired_locks`` below, used by the sweep
    script)."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE post_submissions SET locked_by = NULL, locked_at = NULL, updated_at = now() WHERE id = %s",
            (submission_id,),
        )


def release_expired_locks(conn, ttl_seconds: int = DEFAULT_LOCK_TTL_SECONDS) -> list[dict[str, Any]]:
    """Releases every lock older than ``ttl_seconds`` and returns the affected rows
    *before* release (including ``locked_by``, so the caller can still notify the
    abandoning reviewer's identity if it ever wants to — today's notify target is
    publisher + admin per the plan doc, not the reviewer). Single atomic
    ``UPDATE ... WHERE ... RETURNING *``, same CAS spirit as ``claim_submission``:
    a submission that gets released between the caller's check and now simply
    doesn't show up in the result, never a double-release error."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE post_submissions SET locked_by = NULL, locked_at = NULL, updated_at = now() "
            "WHERE locked_by IS NOT NULL AND locked_at < now() - %s::interval "
            "RETURNING *",
            (f"{int(ttl_seconds)} seconds",),
        )
        return [dict(row) for row in cur.fetchall()]


def find_due_reminders(conn, interval_seconds: int = DEFAULT_REMINDER_INTERVAL_SECONDS) -> list[dict[str, Any]]:
    """``pending`` submissions whose reminder is due: never reminded, or last reminded
    more than ``interval_seconds`` ago. Does not stamp — the sweep script calls
    ``stamp_reminder_sent`` itself only after the WhatsApp send actually succeeds, so a
    delivery failure doesn't silently suppress the next tick's retry."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM post_submissions WHERE status = 'pending' "
            "AND (last_reminder_at IS NULL OR last_reminder_at < now() - %s::interval)",
            (f"{int(interval_seconds)} seconds",),
        )
        return [dict(row) for row in cur.fetchall()]


def stamp_reminder_sent(conn, submission_id: int) -> None:
    with conn.cursor() as cur:
        cur.execute("UPDATE post_submissions SET last_reminder_at = now(), updated_at = now() WHERE id = %s", (submission_id,))


# --- post_platform_drafts: read-only ------------------------------------------------------


def list_drafts(conn, submission_id: int) -> list[dict[str, Any]]:
    """Every draft row for ``submission_id`` (all platforms, all refine rounds) —
    read-only; drafts are created/updated exclusively through ``review_client.py``'s
    existing HTTP contract with social-post-portal, never written here."""
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM post_platform_drafts WHERE submission_id = %s ORDER BY platform, round", (submission_id,))
        return [dict(row) for row in cur.fetchall()]
