"""Postgres access for the ProjectManager plugin — schema ``pm`` in spp-postgres
(extra/plans/pm/migrations/0001_pm_schema.sql).

Rules:
- Every statement is parameterised. Nothing here builds SQL from input text.
- Every change to meetings / people / approvals writes a ``pm.audit`` row in
  the SAME transaction (``with conn:`` commits both or neither).
- Approval state changes are conditional UPDATEs (``WHERE status = <expected>``
  ... RETURNING): two concurrent "yes" replies, or two executor runs, can never
  both win. That is what makes the executor idempotent.
- Scheduled sends claim a ``pm.message_log`` row by unique ``dedupe_key``
  BEFORE sending (see ``claim_send``).

Driver: sync ``psycopg2`` with ``RealDictCursor``, same as the pipeline plugins.
DSN: env ``SPP_DATABASE_URL`` (role ``spp_app``), never logged.
"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Iterator, Optional

try:
    import psycopg2
    import psycopg2.errors
    import psycopg2.extras
except ImportError:  # pragma: no cover - the image ships psycopg2-binary
    psycopg2 = None  # type: ignore[assignment]


class StoreError(RuntimeError):
    """Missing DSN/driver or a failed query."""


APPROVAL_KINDS = (
    "clickup_create_task", "clickup_update_task", "clickup_comment",
    "meeting_create", "meeting_update", "meeting_cancel",
)


def _json(value: Any) -> Optional[str]:
    return None if value is None else json.dumps(value, default=str, ensure_ascii=False)


def connect(dsn: Optional[str] = None):
    if psycopg2 is None:
        raise StoreError("psycopg2 is not installed")
    dsn = dsn or os.environ.get("SPP_DATABASE_URL", "")
    if not dsn:
        raise StoreError("SPP_DATABASE_URL is not set")
    try:
        conn = psycopg2.connect(dsn, cursor_factory=psycopg2.extras.RealDictCursor, connect_timeout=10)
    except Exception as exc:
        # The exception text can include the host but never the password; still keep it short.
        raise StoreError(f"could not connect to Postgres: {type(exc).__name__}") from None
    conn.autocommit = False
    return conn


@contextmanager
def connection(dsn: Optional[str] = None) -> Iterator[Any]:
    conn = connect(dsn)
    try:
        yield conn
    finally:
        conn.close()


def _audit(cur, entity: str, entity_id: Any, action: str, actor: str,
           before: Optional[dict], after: Optional[dict]) -> None:
    cur.execute(
        "INSERT INTO pm.audit (entity, entity_id, action, actor, before, after) VALUES (%s, %s, %s, %s, %s, %s)",
        (entity, str(entity_id), action, actor, _json(before), _json(after)),
    )


# ── ClickUp scope (pm.yaml -> table, for the webapp) ─────────────────────────
def sync_scope(conn, entries: list[tuple[str, str, str, Optional[str]]], actor: str) -> dict[str, int]:
    """``entries`` = (kind, clickup_id, display_name, parent_clickup_id). Upserts
    them and deletes rows no longer in pm.yaml, auditing each change."""
    wanted = {e[1]: e for e in entries}
    changed = removed = 0
    with conn, conn.cursor() as cur:
        cur.execute("SELECT kind, clickup_id, display_name, parent_clickup_id FROM pm.clickup_scope FOR UPDATE")
        existing = {r["clickup_id"]: dict(r) for r in cur.fetchall()}
        for clickup_id, (kind, _, name, parent) in wanted.items():
            after = {"kind": kind, "clickup_id": clickup_id, "display_name": name, "parent_clickup_id": parent}
            before = existing.get(clickup_id)
            if before == after:
                continue
            cur.execute(
                """INSERT INTO pm.clickup_scope (kind, clickup_id, display_name, parent_clickup_id, synced_at)
                   VALUES (%s, %s, %s, %s, now())
                   ON CONFLICT (clickup_id) DO UPDATE SET kind = EXCLUDED.kind,
                       display_name = EXCLUDED.display_name, parent_clickup_id = EXCLUDED.parent_clickup_id,
                       synced_at = now()""",
                (kind, clickup_id, name, parent),
            )
            _audit(cur, "clickup_scope", clickup_id, "upsert", actor, before, after)
            changed += 1
        for clickup_id, before in existing.items():
            if clickup_id not in wanted:
                cur.execute("DELETE FROM pm.clickup_scope WHERE clickup_id = %s", (clickup_id,))
                _audit(cur, "clickup_scope", clickup_id, "delete", actor, before, None)
                removed += 1
    return {"changed": changed, "removed": removed, "total": len(wanted)}


def sync_topics(conn, topics: dict[str, list[str]], actor: str) -> dict[str, int]:
    """pm.yaml meeting_topics -> pm.meeting_topics (for the webapp's topic picker)."""
    changed = removed = 0
    with conn, conn.cursor() as cur:
        cur.execute("SELECT topic_key, tags FROM pm.meeting_topics FOR UPDATE")
        existing = {r["topic_key"]: sorted(r["tags"]) for r in cur.fetchall()}
        for key, tags in topics.items():
            tags = sorted(tags)
            if existing.get(key) == tags:
                continue
            cur.execute("""INSERT INTO pm.meeting_topics (topic_key, tags, synced_at) VALUES (%s, %s, now())
                           ON CONFLICT (topic_key) DO UPDATE SET tags = EXCLUDED.tags, synced_at = now()""",
                        (key, tags))
            _audit(cur, "meeting_topic", key, "upsert", actor,
                   {"tags": existing[key]} if key in existing else None, {"tags": tags})
            changed += 1
        for key, tags in existing.items():
            if key not in topics:
                cur.execute("DELETE FROM pm.meeting_topics WHERE topic_key = %s", (key,))
                _audit(cur, "meeting_topic", key, "delete", actor, {"tags": tags}, None)
                removed += 1
    return {"changed": changed, "removed": removed, "total": len(topics)}


# ── People ───────────────────────────────────────────────────────────────────
_PERSON_COLS = "id, display_name, clickup_user_id, whatsapp_number, timezone, country, role, description, is_active"


def person_by_clickup_id(conn, clickup_user_id: str) -> Optional[dict]:
    with conn, conn.cursor() as cur:
        cur.execute(f"SELECT {_PERSON_COLS} FROM pm.people WHERE clickup_user_id = %s AND is_active", (clickup_user_id,))
        row = cur.fetchone()
    return dict(row) if row else None


def person_by_id(conn, person_id: int) -> Optional[dict]:
    with conn, conn.cursor() as cur:
        cur.execute(f"SELECT {_PERSON_COLS} FROM pm.people WHERE id = %s", (person_id,))
        row = cur.fetchone()
    return dict(row) if row else None


def active_people(conn) -> list[dict]:
    with conn, conn.cursor() as cur:
        cur.execute(f"SELECT {_PERSON_COLS} FROM pm.people WHERE is_active ORDER BY display_name, id")
        return [dict(r) for r in cur.fetchall()]


def insert_person_if_absent(conn, *, display_name: str, clickup_user_id: Optional[str], timezone: str,
                            country: Optional[str], role: Optional[str], actor: str) -> Optional[int]:
    """Seed helper: inserts only when no row has this clickup_user_id (or, for
    someone outside ClickUp, this display name). Never overwrites a row someone
    already edited in the webapp, so re-running a seed is safe."""
    with conn, conn.cursor() as cur:
        if clickup_user_id is None:
            cur.execute("SELECT 1 FROM pm.people WHERE clickup_user_id IS NULL AND lower(display_name) = lower(%s)",
                        (display_name,))
            if cur.fetchone():
                return None
        cur.execute(
            """INSERT INTO pm.people (display_name, clickup_user_id, timezone, country, role, updated_by)
               VALUES (%s, %s, %s, %s, %s, %s)
               ON CONFLICT (clickup_user_id) DO NOTHING RETURNING id""",
            (display_name, clickup_user_id, timezone, country, role, actor),
        )
        row = cur.fetchone()
        if row:
            _audit(cur, "person", row["id"], "create", actor, None,
                   {"display_name": display_name, "clickup_user_id": clickup_user_id, "timezone": timezone,
                    "country": country, "role": role})
            return int(row["id"])
    return None


# ── Meetings ─────────────────────────────────────────────────────────────────
def meetings_between(conn, start: datetime, end: datetime, *, include_cancelled: bool = False) -> list[dict]:
    """Meetings with ``start <= starts_at < end``, with attendee display names."""
    with conn, conn.cursor() as cur:
        cur.execute(
            """SELECT m.id, m.title, m.topic_key, m.starts_at, m.duration_minutes, m.notes, m.status,
                      COALESCE(array_agg(p.display_name ORDER BY p.display_name)
                               FILTER (WHERE p.id IS NOT NULL), '{}') AS attendees
               FROM pm.meetings m
               LEFT JOIN pm.meeting_attendees a ON a.meeting_id = m.id
               LEFT JOIN pm.people p ON p.id = a.person_id
               WHERE m.starts_at >= %s AND m.starts_at < %s AND (%s OR m.status <> 'cancelled')
               GROUP BY m.id ORDER BY m.starts_at, m.id""",
            (start, end, include_cancelled),
        )
        return [dict(r) for r in cur.fetchall()]


def meeting_by_id(conn, meeting_id: int) -> Optional[dict]:
    with conn, conn.cursor() as cur:
        cur.execute("SELECT id, title, topic_key, starts_at, duration_minutes, notes, status FROM pm.meetings WHERE id = %s",
                    (meeting_id,))
        row = cur.fetchone()
    return dict(row) if row else None


def apply_meeting_change(cur, kind: str, payload: dict, actor: str) -> dict:
    """Executes an approved meeting change inside the caller's transaction."""
    if kind == "meeting_create":
        cur.execute(
            """INSERT INTO pm.meetings (title, topic_key, starts_at, duration_minutes, notes, created_by, updated_by)
               VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id, title, topic_key, starts_at, duration_minutes, notes, status""",
            (payload["title"], payload.get("topic_key"), payload["starts_at"], payload.get("duration_minutes"),
             payload.get("notes"), actor, actor),
        )
        after = dict(cur.fetchone())
        _audit(cur, "meeting", after["id"], "create", actor, None, after)
        return {"meeting_id": after["id"]}
    meeting_id = int(payload["meeting_id"])
    cur.execute("SELECT id, title, topic_key, starts_at, duration_minutes, notes, status FROM pm.meetings WHERE id = %s FOR UPDATE",
                (meeting_id,))
    before = cur.fetchone()
    if before is None:
        raise StoreError(f"meeting {meeting_id} does not exist")
    before = dict(before)
    if kind == "meeting_cancel":
        fields = {"status": "cancelled"}
    else:
        fields = {k: payload[k] for k in ("title", "topic_key", "starts_at", "duration_minutes", "notes") if k in payload}
        if not fields:
            raise StoreError("meeting_update with no fields")
    # Column names come from the fixed tuple above, never from input.
    sets = ", ".join(f"{col} = %s" for col in fields)
    cur.execute(
        f"UPDATE pm.meetings SET {sets}, updated_by = %s, updated_at = now() WHERE id = %s "
        "RETURNING id, title, topic_key, starts_at, duration_minutes, notes, status",
        (*fields.values(), actor, meeting_id),
    )
    after = dict(cur.fetchone())
    _audit(cur, "meeting", meeting_id, kind.removeprefix("meeting_"), actor, before, after)
    return {"meeting_id": meeting_id}


# ── Reports ──────────────────────────────────────────────────────────────────
def upsert_report(conn, *, kind: str, period_start: datetime, period_end: datetime, payload: dict,
                  rendered_text: str, agent_note: Optional[str]) -> int:
    """One row per (kind, period_start). Re-running a not-yet-sent report
    refreshes it; a sent report is never overwritten."""
    with conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO pm.reports (kind, period_start, period_end, payload, rendered_text, agent_note)
               VALUES (%s, %s, %s, %s, %s, %s)
               ON CONFLICT (kind, period_start) DO UPDATE SET period_end = EXCLUDED.period_end,
                   payload = EXCLUDED.payload, rendered_text = EXCLUDED.rendered_text,
                   agent_note = EXCLUDED.agent_note, generated_at = now()
                 WHERE pm.reports.send_status <> 'sent'
               RETURNING id""",
            (kind, period_start, period_end, _json(payload), rendered_text, agent_note),
        )
        row = cur.fetchone()
        if row:
            return int(row["id"])
        cur.execute("SELECT id FROM pm.reports WHERE kind = %s AND period_start = %s", (kind, period_start))
        return int(cur.fetchone()["id"])


def report_for_period(conn, kind: str, period_start: datetime) -> Optional[dict]:
    with conn, conn.cursor() as cur:
        cur.execute("SELECT id, kind, period_start, period_end, rendered_text, agent_note, generated_at, send_status, sent_at "
                    "FROM pm.reports WHERE kind = %s AND period_start = %s", (kind, period_start))
        row = cur.fetchone()
    return dict(row) if row else None


def latest_report(conn, kind: str, *, on_or_before: Optional[datetime] = None) -> Optional[dict]:
    with conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, kind, period_start, period_end, rendered_text, agent_note, generated_at, send_status, sent_at "
            "FROM pm.reports WHERE kind = %s AND (%s::timestamptz IS NULL OR period_start <= %s) "
            "ORDER BY period_start DESC LIMIT 1",
            (kind, on_or_before, on_or_before),
        )
        row = cur.fetchone()
    return dict(row) if row else None


def set_report_send_status(conn, report_id: int, status: str) -> None:
    with conn, conn.cursor() as cur:
        cur.execute("UPDATE pm.reports SET send_status = %s, sent_at = CASE WHEN %s = 'sent' THEN now() ELSE sent_at END "
                    "WHERE id = %s", (status, status, report_id))


# ── Message log ──────────────────────────────────────────────────────────────
def claim_send(conn, *, dedupe_key: Optional[str], purpose: str, chat_id: str, body: str,
               approval_id: Optional[int] = None, report_id: Optional[int] = None,
               meeting_id: Optional[int] = None) -> Optional[int]:
    """Insert the outbound row with status 'sending' BEFORE the send. Returns
    None when ``dedupe_key`` was already claimed (already sent or attempted)."""
    with conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO pm.message_log (direction, purpose, chat_id, body, status, dedupe_key,
                                           related_approval_id, related_report_id, related_meeting_id)
               VALUES ('out', %s, %s, %s, 'sending', %s, %s, %s, %s)
               ON CONFLICT (dedupe_key) DO NOTHING RETURNING id""",
            (purpose, chat_id, body, dedupe_key, approval_id, report_id, meeting_id),
        )
        row = cur.fetchone()
    return int(row["id"]) if row else None


def finish_send(conn, message_id: int, status: str, error: Optional[str] = None) -> None:
    """Record the outcome. A failed send releases its dedupe key (renamed to
    ``<key>#failed#<id>``) so the next scheduled run may try again; callers cap
    attempts with ``failed_attempts``."""
    with conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE pm.message_log SET status = %s, error = %s, updated_at = now(),
                   dedupe_key = CASE WHEN %s = 'failed' AND dedupe_key IS NOT NULL
                                     THEN dedupe_key || '#failed#' || id ELSE dedupe_key END
               WHERE id = %s""",
            (status, (error or None) and error[:500], status, message_id),
        )


def failed_attempts(conn, dedupe_key: str) -> int:
    with conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM pm.message_log WHERE dedupe_key LIKE %s AND status = 'failed'",
                    (dedupe_key.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "#failed#%",))
        return int(cur.fetchone()["n"])


def log_inbound(conn, *, purpose: str, chat_id: str, body: str, approval_id: Optional[int]) -> None:
    with conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO pm.message_log (direction, purpose, chat_id, body, status, related_approval_id)
               VALUES ('in', %s, %s, %s, 'received', %s)""",
            (purpose, chat_id, body[:2000], approval_id),
        )


def stuck_sends(conn, older_than: datetime) -> list[dict]:
    with conn, conn.cursor() as cur:
        cur.execute("SELECT id, purpose, dedupe_key, created_at FROM pm.message_log "
                    "WHERE direction = 'out' AND status = 'sending' AND created_at < %s ORDER BY id", (older_than,))
        return [dict(r) for r in cur.fetchall()]


# ── Approvals ────────────────────────────────────────────────────────────────
_APPROVAL_COLS = ("id, kind, payload, summary, reason, requested_by, approver_person_id, status, decided_by, "
                  "decided_at, executed_at, result, error, idempotency_key, created_at")


def create_approval(conn, *, kind: str, payload: dict, summary: str, reason: Optional[str], requested_by: str,
                    approver_person_id: int, idempotency_key: str) -> tuple[int, bool]:
    """Returns (approval_id, created). A repeated proposal with the same
    idempotency key returns the existing row instead of a second approval."""
    if kind not in APPROVAL_KINDS:
        raise StoreError(f"unknown approval kind {kind!r}")
    with conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO pm.approvals (kind, payload, summary, reason, requested_by, approver_person_id, idempotency_key)
               VALUES (%s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (idempotency_key) DO NOTHING RETURNING id""",
            (kind, _json(payload), summary, reason, requested_by, approver_person_id, idempotency_key),
        )
        row = cur.fetchone()
        if row:
            _audit(cur, "approval", row["id"], "propose", requested_by, None,
                   {"kind": kind, "payload": payload, "summary": summary, "reason": reason})
            return int(row["id"]), True
        cur.execute("SELECT id FROM pm.approvals WHERE idempotency_key = %s", (idempotency_key,))
        return int(cur.fetchone()["id"]), False


def get_approval(conn, approval_id: int) -> Optional[dict]:
    with conn, conn.cursor() as cur:
        cur.execute(f"SELECT {_APPROVAL_COLS} FROM pm.approvals WHERE id = %s", (approval_id,))
        row = cur.fetchone()
    return dict(row) if row else None


def pending_for_approver(conn, approver_person_id: int) -> list[dict]:
    with conn, conn.cursor() as cur:
        cur.execute(f"SELECT {_APPROVAL_COLS} FROM pm.approvals WHERE approver_person_id = %s AND status = 'pending' "
                    "ORDER BY id", (approver_person_id,))
        return [dict(r) for r in cur.fetchall()]


def decide_approval(conn, approval_id: int, approver_person_id: int, decision: str, decided_by: str) -> Optional[dict]:
    """pending -> approved|rejected, only by the assigned approver. Returns the
    updated row, or None when it was not pending (already decided, expired, or
    someone else's) — a repeated "yes" is therefore a no-op."""
    if decision not in ("approved", "rejected"):
        raise StoreError(f"bad decision {decision!r}")
    with conn, conn.cursor() as cur:
        cur.execute(
            f"""UPDATE pm.approvals SET status = %s, decided_by = %s, decided_at = now(), updated_at = now()
                WHERE id = %s AND approver_person_id = %s AND status = 'pending'
                RETURNING {_APPROVAL_COLS}""",
            (decision, decided_by, approval_id, approver_person_id),
        )
        row = cur.fetchone()
        if row:
            _audit(cur, "approval", approval_id, decision, decided_by, {"status": "pending"}, {"status": decision})
    return dict(row) if row else None


def approved_ids(conn) -> list[int]:
    with conn, conn.cursor() as cur:
        cur.execute("SELECT id FROM pm.approvals WHERE status = 'approved' ORDER BY id")
        return [int(r["id"]) for r in cur.fetchall()]


def claim_for_execution(conn, approval_id: int) -> Optional[dict]:
    """approved -> executing. Only one caller can win; a crash after this point
    leaves 'executing' (never re-run automatically; reported for a human)."""
    with conn, conn.cursor() as cur:
        cur.execute(
            f"""UPDATE pm.approvals SET status = 'executing', updated_at = now()
                WHERE id = %s AND status = 'approved' RETURNING {_APPROVAL_COLS}""",
            (approval_id,),
        )
        row = cur.fetchone()
    return dict(row) if row else None


def finish_execution(conn, approval_id: int, *, ok: bool, result: Optional[dict], error: Optional[str],
                     actor: str, meeting_change: Optional[tuple[str, dict]] = None) -> None:
    """executing -> executed|failed. A meeting change is applied in the same
    transaction as the status flip, so it can never apply twice."""
    with conn, conn.cursor() as cur:
        if ok and meeting_change is not None:
            result = apply_meeting_change(cur, meeting_change[0], meeting_change[1], actor)
        status = "executed" if ok else "failed"
        cur.execute(
            """UPDATE pm.approvals SET status = %s, executed_at = CASE WHEN %s THEN now() ELSE NULL END,
                   result = %s, error = %s, updated_at = now()
               WHERE id = %s AND status = 'executing'""",
            (status, ok, _json(result), (error or None) and error[:1000], approval_id),
        )
        if cur.rowcount != 1:
            raise StoreError(f"approval {approval_id} was not in 'executing'")
        _audit(cur, "approval", approval_id, status, actor, {"status": "executing"},
               {"status": status, "result": result, "error": error})


def expire_pending(conn, older_than: datetime, actor: str) -> list[int]:
    with conn, conn.cursor() as cur:
        cur.execute("UPDATE pm.approvals SET status = 'expired', updated_at = now() "
                    "WHERE status = 'pending' AND created_at < %s RETURNING id", (older_than,))
        ids = [int(r["id"]) for r in cur.fetchall()]
        for approval_id in ids:
            _audit(cur, "approval", approval_id, "expired", actor, {"status": "pending"}, {"status": "expired"})
    return ids


def stuck_executions(conn, older_than: datetime) -> list[int]:
    with conn, conn.cursor() as cur:
        cur.execute("SELECT id FROM pm.approvals WHERE status = 'executing' AND updated_at < %s ORDER BY id", (older_than,))
        return [int(r["id"]) for r in cur.fetchall()]
