"""Core deterministic-glue logic — the code replacement for
``event-post-pipeline/SKILL.md`` steps 2, 3, 4 (partial), 5, 6. Stylus's
drafting (step 2's target, and every refine round) is untouched: this module
only creates its handoff tasks and reacts to their completion.

Every function here is plain, synchronous, testable Python — no LLM call,
no agent turn. The one deliberate exception this plugin depends on is that
Stylus itself must keep producing the structured ``kanban_complete(metadata=
{"event_title", "fb", "ig", "blog": {...}})`` contract verified against the
live Kanban DB during design (see
``extra/plans/socialpost/event-post-pipeline-deterministic-glue-plan.md``).
``event_title`` may be absent until that SOUL.md addition ships — handled
by falling back to a mechanical truncation of the description, never by
crashing or fabricating a review page from partial data.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Optional

from plugins.platforms.event_post_pipeline import db
from plugins.platforms.event_post_pipeline.fence import defuse_fence_tags
from plugins.platforms.event_post_pipeline.intake import INTAKE_VERSION, parse_intake, render_intake_lines
from plugins.platforms.event_post_pipeline.review_client import (
    ReviewClientConfig, ReviewClientError, create_review, slug_from_review_url, update_review,
)
from plugins.platforms.event_post_pipeline.store import EventPostPipelineStore

logger = logging.getLogger("plugins.platforms.event_post_pipeline")

TENANT = "event-post-pipeline-v2"
CREATED_BY = "event-post-pipeline-plugin"
_REFINE_IDEMPOTENCY_RE = re.compile(r"^(?P<root>.+)-(?P<platform>fb|ig|blog)-r(?P<round>\d+)$")
_TITLE_MAX_CHARS = 80
_REVIEW_URL_COMMENT_RE = re.compile(r"Review page:\s*(\S+)")

# Split intake (``blog_enrichment.split``): first line of each task body tells Stylus (and this plugin) the task kind.
SOCIAL_MARKER = "[task-kind: social]"
BLOG_MARKER = "[task-kind: blog]"
DEFAULT_BLOG_WAIT_SECONDS = 1200
_SOCIAL_ALLOWED_KEYS = frozenset({"event_title", "fb", "ig"})
_BLOG_ALLOWED_KEYS = frozenset({"event_title", "blog"})


def split_enabled(extra: Optional[dict[str, Any]]) -> bool:
    """``platforms.event_post_pipeline.extra.blog_enrichment.split`` — literal ``true`` only; default off."""
    cfg = (extra or {}).get("blog_enrichment")
    return isinstance(cfg, dict) and cfg.get("split") is True


def blog_wait_seconds(extra: Optional[dict[str, Any]]) -> float:
    """``blog_enrichment.blog_wait_seconds`` (default 1200); anything not a positive number falls back to the default."""
    cfg = (extra or {}).get("blog_enrichment")
    value = cfg.get("blog_wait_seconds") if isinstance(cfg, dict) else None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return float(DEFAULT_BLOG_WAIT_SECONDS)
    return float(value)


class PipelineError(RuntimeError):
    """Raised for conditions that should stop this event and leave it for a human,
    never for something safe to silently retry or guess past."""


def _shadow_call(shadow: Any, method: str, **kwargs: Any) -> None:
    """Shadow mode (task 08): hand an observation to ``shadow`` (a ``shadow.ShadowRecorder``, or ``None`` = flags off = do nothing).
    Log-only by construction and doubly guarded here: nothing it does or raises may change what this pipeline does."""
    if shadow is None:
        return
    try:
        getattr(shadow, method)(**kwargs)
    except Exception as exc:  # noqa: BLE001
        logger.warning("event_post_pipeline: shadow %s failed (%s); ignored", method, type(exc).__name__)


@dataclass(frozen=True)
class KanbanOps:
    """The subset of ``hermes_cli.kanban_db`` this module calls, injected so
    tests can run against a real throwaway SQLite board with zero mocking."""

    kb: Any  # the hermes_cli.kanban_db module itself

    def create_task(self, conn, **kwargs) -> str:
        return self.kb.create_task(conn, **kwargs)

    def get_task(self, conn, task_id: str):
        return self.kb.get_task(conn, task_id)

    def latest_run(self, conn, task_id: str):
        return self.kb.latest_run(conn, task_id)

    def add_comment(self, conn, task_id: str, author: str, body: str) -> int:
        return self.kb.add_comment(conn, task_id, author, body)

    def list_comments(self, conn, task_id: str):
        return self.kb.list_comments(conn, task_id)

    def list_tasks(self, conn, **kwargs):
        return self.kb.list_tasks(conn, **kwargs)


def _short_title(description: str) -> str:
    """Mechanical fallback only — prefer Stylus's ``event_title`` metadata field."""
    first_sentence = re.split(r"(?<=[.!?])\s", description.strip(), maxsplit=1)[0]
    text = first_sentence if len(first_sentence) <= _TITLE_MAX_CHARS else description.strip()
    return (text[: _TITLE_MAX_CHARS - 1] + "…") if len(text) > _TITLE_MAX_CHARS else text


def _wrap_untrusted(event_pack: dict[str, Any]) -> str:
    """Same defensive framing the skill file's webhook prompt used: the fields inside
    came from a public form and must never be read as instructions by anything that
    later reads this task body (Stylus is itself an LLM).

    Every interpolated value goes through ``defuse_fence_tags`` so the text cannot
    close or nest the fence (a no-op for text without a fence tag, so ordinary
    submissions render byte-for-byte as before).

    Intake v2 (W08): when the payload carries a usable ``intake`` object its labelled lines
    (``intake.render_intake_lines``) are added after the seven legacy lines, inside the same fence.
    A payload without one renders exactly as before."""
    lines = [
        "<untrusted_submission>",
        f"Date: {defuse_fence_tags(event_pack.get('date', ''))}",
        f"Description: {defuse_fence_tags(event_pack.get('description', ''))}",
        f"Quote: {defuse_fence_tags(event_pack.get('quote', ''))}",
        f"First photo: {defuse_fence_tags((event_pack.get('first') or {}).get('finalUrl', ''))}",
        f"Last photo: {defuse_fence_tags((event_pack.get('last') or {}).get('finalUrl', ''))}",
        f"Rest: {defuse_fence_tags([r.get('finalUrl') for r in (event_pack.get('rest') or [])])}",
        *render_intake_lines(parse_intake(event_pack), defuse_fence_tags),
        "</untrusted_submission>",
    ]
    return "\n".join(lines)


# --- Pipeline visualizer tracking ---------------------------------------------------------
#
# Best-effort only: per whatsapp_notify.py's own stated rule ("a notification problem
# should never take down the deterministic pipeline logic around it"), the same applies
# here — a visualizer-tracking failure must never break the actual pipeline. Every helper
# below swallows its own errors (after logging) and is a silent no-op when no Postgres is
# configured for this profile (same "degrade gracefully" convention as db_url elsewhere in
# this plugin).


def _resolve_visualizer_db_url(database_url: Optional[str]) -> str:
    return database_url if database_url is not None else db.resolve_database_url()


def _display_title(record: Optional[dict[str, Any]], fallback: str) -> str:
    description = (record or {}).get("description")
    return _short_title(description) if description else fallback


def _track_run(
    database_url: Optional[str], task_id: str, *, title: str, state: str, current_step: str,
    step: str, status: str, detail: Optional[str] = None, actor: Optional[str] = None,
) -> None:
    """Upserts the ``pipeline_runs`` row then appends a ``pipeline_events`` row — for a
    call site that represents a real state/step transition."""
    url = _resolve_visualizer_db_url(database_url)
    if not url:
        return
    try:
        conn = db.get_connection(url)
    except db.DatabaseError:
        logger.debug("event_post_pipeline: visualizer tracking skipped (no Postgres) for task=%s step=%s", task_id, step)
        return
    try:
        db.upsert_pipeline_run(conn, task_id, title=title, state=state, current_step=current_step)
        db.record_pipeline_event(conn, task_id, step, status, detail=detail, actor=actor)
    except Exception:
        logger.exception("event_post_pipeline: visualizer tracking failed for task=%s step=%s", task_id, step)
    finally:
        conn.close()


def _track_event_only(
    database_url: Optional[str], task_id: str, step: str, status: str, *,
    detail: Optional[str] = None, actor: Optional[str] = None,
) -> None:
    """Appends a ``pipeline_events`` row without touching ``pipeline_runs``' state/
    current_step — for events that aren't themselves a state transition (a notification
    send, a review action taken). Relies on an earlier ``_track_run`` call in this run's
    lifecycle having already created the ``pipeline_runs`` row (true from intake onward)."""
    url = _resolve_visualizer_db_url(database_url)
    if not url:
        return
    try:
        conn = db.get_connection(url)
    except db.DatabaseError:
        logger.debug("event_post_pipeline: visualizer tracking skipped (no Postgres) for task=%s step=%s", task_id, step)
        return
    try:
        db.record_pipeline_event(conn, task_id, step, status, detail=detail, actor=actor)
    except Exception:
        logger.exception("event_post_pipeline: visualizer event tracking failed for task=%s step=%s", task_id, step)
    finally:
        conn.close()


def _link_submission_if_resolvable(database_url: Optional[str], task_id: str, review_url: str) -> None:
    """Best-effort backfill of ``pipeline_runs.submission_id`` once social-post-portal's
    own row exists (resolved via the review-page slug, which is a stable 1:1 key on
    ``post_submissions``). Silently does nothing if it can't be resolved — not critical,
    per the visualizer spec."""
    url = _resolve_visualizer_db_url(database_url)
    if not url:
        return
    try:
        slug = slug_from_review_url(review_url)
    except Exception:
        return
    try:
        conn = db.get_connection(url)
    except db.DatabaseError:
        return
    try:
        submission_id = db.get_submission_id_by_slug(conn, slug)
        if submission_id is not None:
            db.link_submission_id(conn, task_id, submission_id)
    except Exception:
        logger.exception("event_post_pipeline: visualizer submission-id link failed for task=%s", task_id)
    finally:
        conn.close()


def root_task_id_for_idempotency_key(idempotency_key: Optional[str]) -> Optional[str]:
    """The root Stylus task id for a (possibly refine-round) idempotency key — public so
    ``hooks.py``'s ``kanban_task_blocked`` callback can resolve the ``pipeline_runs`` key
    for a blocked refine-round task without reaching into this module's private regex."""
    match = _REFINE_IDEMPOTENCY_RE.match(idempotency_key or "")
    return match.group("root") if match else None


def track_stylus_blocked(database_url: Optional[str], task_id: str, *, title: str, reason: Optional[str]) -> None:
    """Called from ``hooks.py``'s ``kanban_task_blocked`` callback — a Kanban-dispatcher-
    level event this plugin's own code doesn't otherwise see."""
    _track_run(
        database_url, task_id, title=title, state="blocked", current_step="stylus_blocked",
        step="stylus_blocked", status="error", detail=reason,
    )


def track_notification(database_url: Optional[str], task_id: str, *, ok: bool, message: str) -> None:
    """Called from ``hooks.py`` and ``adapter.py`` right after a WhatsApp notification
    send succeeds or fails — never itself a state/current_step transition.

    ``message`` is always the actual notification text (attempted or sent), never the
    exception — found the hard way (2026-09-21): the earlier version stored the
    exception string as ``detail`` on failure, and the visualizer's "Retry notify"
    button naively resent whatever was in ``detail`` — so retrying a failed notification
    literally sent the error message ("name 'SimpleNamespace' is not defined") to the
    WhatsApp group instead of the real content. The exception itself is already
    captured by each call site's own ``logger.exception(...)``/``logger.error(...)`` —
    server logs are the right place for it, not a field a retry button reads back."""
    step = "notified" if ok else "notify_failed"
    status = "ok" if ok else "error"
    _track_event_only(database_url, task_id, step, status, detail=message)


def track_stylus_retry_requested(database_url: Optional[str], task_id: str, *, ok: bool, detail: Optional[str] = None) -> None:
    """Called from ``adapter.py``'s ``POST /retry`` (``kind=stylus_blocked``) right after
    ``kanban_db.unblock_task`` returns — success or "wasn't actually blocked" alike — so
    the retry attempt itself shows up in the visualizer timeline, same convention as
    ``track_notification`` above. Never itself a state/current_step transition (the
    dispatcher picking the task back up is what actually changes ``pipeline_runs``, via
    whatever step fires next)."""
    _track_event_only(database_url, task_id, "stylus_retry_requested", "ok" if ok else "error", detail=detail)


def _image_list(event_pack: dict[str, Any]) -> list[str]:
    first = (event_pack.get("first") or {}).get("finalUrl")
    rest = [r.get("finalUrl") for r in (event_pack.get("rest") or []) if r.get("finalUrl")]
    last = (event_pack.get("last") or {}).get("finalUrl")
    return [u for u in ([first] + rest + [last]) if u]


def handle_intake(
    conn, ops: KanbanOps, store: EventPostPipelineStore, *, submission_id: str, event_pack: dict[str, Any],
    database_url: Optional[str] = None, shadow: Any = None, split: bool = False,
    blog_parents: Optional[list[str]] = None,
) -> str:
    """Step 2: create the Stylus handoff. No LLM call. Returns the new task id.

    ``shadow``: optional ``shadow.ShadowRecorder`` (task 08). Observation only, called after everything above has happened.

    ``split`` (``blog_enrichment.split``, default off = exactly the single task below): create S (social: FB+IG only,
    this root id and key) and B (blog only, key ``<root>-blog-r1``). ``blog_parents`` is the hook for the research
    task R (``parents=[R]``, a later work package); with none, B has no parent and does not wait for S."""
    description = str(event_pack.get("description") or "")
    title = _short_title(description) if description else submission_id
    body = _wrap_untrusted(event_pack)
    task_id = ops.create_task(
        conn, title=f"Draft social copy: {defuse_fence_tags(title)}",
        body=f"{SOCIAL_MARKER}\n{body}" if split else body,
        assignee="stylus", idempotency_key=submission_id, tenant=TENANT, created_by=CREATED_BY,
    )
    blog_task_id: Optional[str] = None
    if split:
        blog_kwargs: dict[str, Any] = {"parents": list(blog_parents)} if blog_parents else {}
        blog_task_id = ops.create_task(
            conn, title=f"Draft blog: {defuse_fence_tags(title)}", body=f"{BLOG_MARKER}\n{body}",
            assignee="stylus", idempotency_key=f"{task_id}-blog-r1", tenant=TENANT, created_by=CREATED_BY, **blog_kwargs,
        )
    # Visualizer: "intake received" and "Stylus task created" both happen at this exact
    # call site (a single Kanban task creation serves both), so both events are recorded
    # here, back to back — see the plugin's own doc comment about merging near-simultaneous
    # lifecycle events (whatsapp_notify.py / adapter.py's notification merge for the same
    # reason).
    _track_run(
        database_url, task_id, title=title, state="running", current_step="intake_received",
        step="intake_received", status="ok",
    )
    _track_run(
        database_url, task_id, title=title, state="running", current_step="stylus_drafting",
        step="stylus_drafting", status="ok",
    )
    intake = parse_intake(event_pack)
    store.upsert_submission(
        submission_id,
        {
            "root_task_id": task_id, "date": event_pack.get("date"), "description": description,
            "quote": event_pack.get("quote"), "images": _image_list(event_pack),
            # Intake v2: the validated object, so refine rounds and the review row keep the new
            # facts. Absent keys (not None values) for a v1 payload, so old records look unchanged.
            **({"intake": intake, "intake_version": INTAKE_VERSION} if intake else {}),
            # Carried through to create_review() at first-round completion so
            # post_submissions.submitted_by can be resolved (see plan doc's
            # "Social Media Curator" ledger rationale) — same fields adapter.py's
            # intake notification already reads off the raw event_pack.
            "submitter_name": event_pack.get("submitterName"), "submitter_phone": event_pack.get("submitterPhone"),
        },
    )
    if split:
        store.join_init(submission_id, blog_task_id)
    _shadow_call(shadow, "record_intake", submission_id=submission_id, task_id=task_id, event_pack=event_pack)
    logger.info("event_post_pipeline: intake submission=%s -> task=%s", submission_id, task_id)
    return task_id


def _record_for_task(conn, ops: KanbanOps, store: EventPostPipelineStore, task) -> Optional[dict[str, Any]]:
    """The submission record owning ``task`` — from the store, or reconstructed from
    Kanban's own comment history when the store is empty/stale, or when the store
    record exists but is missing ``review_url`` (never the reverse).

    Found the hard way (2026-09-26, live production, task t_9026ad85): a first-round
    completion that fails before it ever reaches ``store.upsert_submission(...,
    {"review_url": ...})`` (e.g. the blog-shape bug) leaves a real, non-None store
    record with no ``review_url`` — the old "record is not None: return it" short
    circuit then skipped the Kanban-comment fallback entirely, even after a human
    manually posted "Review page: <url>" as a remediation comment on the root task."""
    refine = _REFINE_IDEMPOTENCY_RE.match(task.idempotency_key or "")
    root_task_id = refine.group("root") if refine else task.id
    record = store.find_by_task_id(root_task_id) or store.get_submission(root_task_id)
    if record is not None and record.get("review_url"):
        return record
    # Store miss, or a store hit missing review_url: reconstruct the minimum needed
    # (review slug) from the root task's own comment history, exactly as the skill
    # file this replaces already relies on.
    root_task = ops.get_task(conn, root_task_id)
    if root_task is None:
        return record
    for comment in ops.list_comments(conn, root_task_id):
        if match := _REVIEW_URL_COMMENT_RE.search(comment.body):
            return {**(record or {}), "root_task_id": root_task_id, "review_url": match.group(1)}
    return record


def blog_shape_error(blog: Any) -> Optional[str]:
    """``None`` if ``blog`` is a well-formed completion object, else a message
    explaining the required shape — shared between the post-hoc check below
    and ``pre_tool_call_guard.py``'s synchronous ``kanban_complete`` block, so
    both sites enforce exactly the same contract."""
    if isinstance(blog, dict) and blog.get("body"):
        return None
    return (
        "metadata['blog'] must be an object with at least a non-empty 'body' string "
        "(also expects 'seo_title', 'url_slug', 'focus_keyword', 'supporting_keywords', "
        f"'meta_description') — got {type(blog).__name__}: {blog!r}"
    )


def _extract_stylus_metadata(
    run, *, platform: Optional[str] = None, required: Optional[tuple[str, ...]] = None,
) -> dict[str, Any]:
    """``platform`` is the single platform a refine round asked Stylus to redraft (its
    task body explicitly says "Only the {PLATFORM} content needs to be redrafted — the
    other platforms are unaffected", so Stylus correctly omits them). ``None`` means a
    first-round completion, which needs all three.

    Found the hard way (2026-09-21, live production): this validation used to
    unconditionally require a well-formed 'blog' object plus non-empty 'fb'/'ig'
    strings regardless of which platform a refine round actually asked for — so
    every fb/ig refine round has always failed here with "Stylus completion metadata
    missing a well-formed 'blog' object", before ever reaching the platform-specific
    logic below that already correctly only needs the one refined field. Only blog
    refines happened to pass, by coincidence of blog being the first key checked."""
    if run is None or run.outcome != "completed" or not isinstance(run.metadata, dict):
        raise PipelineError(f"Stylus run for is missing or did not complete cleanly (outcome={getattr(run, 'outcome', None)!r})")
    metadata = run.metadata
    # ``required`` (split intake) names exactly the keys one half of the split must carry; it wins over ``platform``.
    required = tuple(required) if required else ((platform,) if platform else ("blog", "fb", "ig"))
    for key in required:
        if key == "blog":
            if (err := blog_shape_error(metadata.get("blog"))) is not None:
                raise PipelineError(f"Stylus completion {err}")
        elif not isinstance(metadata.get(key), str) or not metadata[key].strip():
            raise PipelineError(f"Stylus completion metadata missing required string field {key!r}: {metadata!r}")
    return metadata


def _blog_seo_payload(blog: dict[str, Any]) -> dict[str, Any]:
    return {
        "seoTitle": blog.get("seo_title", ""), "urlSlug": blog.get("url_slug", ""),
        "focusKeyword": blog.get("focus_keyword", ""), "supportingKeywords": blog.get("supporting_keywords") or [],
        "metaDescription": blog.get("meta_description", ""),
    }


def handle_stylus_completion(
    conn, ops: KanbanOps, store: EventPostPipelineStore, review_config: ReviewClientConfig, task_id: str,
    database_url: Optional[str] = None, shadow: Any = None, now: Optional[float] = None,
) -> Optional[dict[str, Any]]:
    """The ``kanban_task_completed`` hook callback's core logic (transport/async
    concerns live in ``adapter.py``). Returns a dict describing what happened (for
    logging/tests), or ``None`` when this completion isn't ours to react to.

    Raises :class:`PipelineError` for anything that should stop and leave the task
    visible for a human rather than silently guess past it.
    """
    task = ops.get_task(conn, task_id)
    if task is None or task.tenant != TENANT or task.assignee != "stylus":
        return None  # not ours
    run = ops.latest_run(conn, task_id)
    refine = _REFINE_IDEMPOTENCY_RE.match(task.idempotency_key or "")
    platform = refine.group("platform") if refine else None
    kind = _split_kind(store, task, refine)
    if kind is not None:
        when = time.time() if now is None else now
        if kind == "social":
            metadata = _extract_stylus_metadata(run, required=("fb", "ig"))
            _warn_stray_keys(task, metadata, _SOCIAL_ALLOWED_KEYS)
            return _handle_split_social_completion(conn, ops, store, review_config, task, metadata, database_url, shadow, when)
        metadata = _extract_stylus_metadata(run, required=("blog",))
        _warn_stray_keys(task, metadata, _BLOG_ALLOWED_KEYS)
        return _handle_split_blog_completion(
            conn, ops, store, review_config, task, refine.group("root"), metadata, database_url, shadow, when,
        )
    metadata = _extract_stylus_metadata(run, platform=platform)
    if refine:
        return _handle_refine_round_completion(
            conn, ops, store, review_config, task, metadata, platform, database_url, shadow,
        )
    return _handle_first_round_completion(conn, ops, store, review_config, task, metadata, database_url, shadow)


def _handle_first_round_completion(
    conn, ops, store, review_config, task, metadata: dict[str, Any], database_url: Optional[str] = None, shadow: Any = None,
) -> dict[str, Any]:
    record = store.get_submission(task.idempotency_key) or {}
    blog = metadata["blog"]
    event_title = str(metadata.get("event_title") or _short_title(record.get("description") or task.title))
    _shadow_call(shadow, "record_completion", task_id=task.id, platform="blog", round_no=1, blog=blog, pack=record)
    try:
        review_url = create_review(
            review_config, task_id=task.id, event_title=event_title, date=record.get("date", ""),
            images=record.get("images", []), fb=metadata["fb"], ig=metadata["ig"], blog_body=blog["body"],
            blog_seo=_blog_seo_payload(blog),
            description=record.get("description"), quote=record.get("quote"), intake=record.get("intake"),
            submitter_name=record.get("submitter_name"), submitter_phone=record.get("submitter_phone"),
        )
    except ReviewClientError as exc:
        raise PipelineError(str(exc)) from exc
    ops.add_comment(conn, task.id, CREATED_BY, f"Review page: {review_url}")
    store.upsert_submission(task.idempotency_key, {"review_url": review_url})
    _track_run(
        database_url, task.id, title=event_title, state="waiting", current_step="awaiting_review",
        step="stylus_done", status="ok", detail=review_url,
    )
    _link_submission_if_resolvable(database_url, task.id, review_url)
    return {"action": "created_review_page", "task_id": task.id, "root_task_id": task.id, "review_url": review_url}


def _handle_refine_round_completion(
    conn, ops, store, review_config, task, metadata: dict[str, Any], platform: str, database_url: Optional[str] = None, shadow: Any = None,
    first_draft: bool = False,
) -> dict[str, Any]:
    record = _record_for_task(conn, ops, store, task)
    review_url = (record or {}).get("review_url")
    if not review_url:
        raise PipelineError(f"could not recover the review-page URL for refine task {task.id} (platform={platform})")
    slug = slug_from_review_url(review_url)
    blog = metadata.get("blog") if platform == "blog" else None
    if platform == "blog":
        _rm = _REFINE_IDEMPOTENCY_RE.match(task.idempotency_key or "")
        _shadow_call(shadow, "record_completion", task_id=(record or {}).get("root_task_id") or (_rm.group("root") if _rm else task.id), platform="blog",
                     round_no=(1 if first_draft else int(_rm.group("round")) + 1) if _rm else None, blog=blog, pack=record)
    text = blog["body"] if platform == "blog" else metadata[platform]
    seo = _blog_seo_payload(blog) if platform == "blog" else None
    try:
        update_review(review_config, slug=slug, platform=platform, text=text, seo=seo)
    except ReviewClientError as exc:
        raise PipelineError(str(exc)) from exc
    root_task_id = (record or {}).get("root_task_id") or task.idempotency_key.rsplit("-", 2)[0]
    # The store's own dict key is submission_id (an intake-time slug), never root_task_id
    # (a Kanban task id — a different identity space, per handle_intake()'s own upsert).
    # Found the hard way alongside the review_url bug above: upserting by root_task_id
    # here silently created a second, orphaned store record instead of updating the
    # original submission's own "rounds" field.
    store_key = (record or {}).get("submission_id") or root_task_id
    store.upsert_submission(store_key, {"rounds": {platform: {"task_id": task.id, "status": "pending"}}})
    _track_run(
        database_url, root_task_id, title=_display_title(record, root_task_id), state="waiting",
        current_step="awaiting_review", step="stylus_done", status="ok", detail=review_url,
    )
    _link_submission_if_resolvable(database_url, root_task_id, review_url)
    return {
        "action": "updated_review_page", "task_id": task.id, "root_task_id": root_task_id,
        "platform": platform, "review_url": review_url,
        # Split intake only: the blog that arrived after the fallback page (hooks sends the one allowed extra message).
        **({"late_blog": True} if first_draft else {}),
    }


# --- Split intake: S (fb+ig) and B (blog) joined into ONE review page ---------------------------------
#
# S and B run in parallel in separate Stylus sessions. Each completion stores its validated half in the
# plugin store (``record["join"]``); whichever half arrives second triggers the single ``create_review``.
# Idempotent (a repeated completion keeps the first stored half and the atomic claim lets exactly one
# caller create the page), restart-safe (all state is in the store file, re-read under a file lock), and
# ``sweep_join`` (called from a dispatcher-tick hook) covers a stuck blog (fallback page) and a failed
# page creation (retry).


def _warn_stray_keys(task, metadata: dict[str, Any], allowed: frozenset) -> None:
    stray = sorted(str(k) for k in metadata if k not in allowed)
    if stray:
        logger.warning(
            "event_post_pipeline: split task %s (%s) completed with unexpected metadata keys %s; ignored",
            task.id, task.idempotency_key, stray,
        )


def _split_kind(store: EventPostPipelineStore, task, refine) -> Optional[str]:
    """``"social"`` / ``"blog"`` when ``task`` is one half of a split intake, else ``None`` (flags-off path)."""
    body = getattr(task, "body", None) or ""
    if refine is None:
        if body.lstrip().startswith(SOCIAL_MARKER):
            return "social"
        record = store.get_submission(task.idempotency_key) or {}
        return "social" if (record.get("join") or {}).get("mode") == "split" else None
    if refine.group("platform") != "blog" or int(refine.group("round")) != 1:
        return None
    if body.lstrip().startswith(BLOG_MARKER):
        return "blog"
    record = store.find_by_task_id(refine.group("root")) or {}
    join = record.get("join") or {}
    return "blog" if join.get("mode") == "split" and join.get("blog_task_id") == task.id else None


def _handle_split_social_completion(
    conn, ops, store, review_config, task, metadata: dict[str, Any], database_url: Optional[str], shadow: Any, now: float,
) -> dict[str, Any]:
    submission_id = task.idempotency_key
    record = store.get_submission(submission_id)
    if record is None:
        raise PipelineError(f"no stored submission for split social task {task.id} (key={submission_id!r}); cannot join")
    store.join_init(submission_id, None)
    event_title = str(metadata.get("event_title") or _short_title(record.get("description") or task.title))
    half = {"task_id": task.id, "event_title": event_title, "fb": metadata["fb"], "ig": metadata["ig"], "completed_at": now}
    record, newly = store.join_store_half(submission_id, "social", half)
    if newly and not (record["join"].get("blog")):
        _track_event_only(database_url, task.id, "join_waiting", "info", detail="Facebook/Instagram drafts done; waiting for the blog draft")
    return _advance_join(conn, ops, store, review_config, submission_id, now, database_url, shadow)


def _handle_split_blog_completion(
    conn, ops, store, review_config, task, root_task_id: str, metadata: dict[str, Any], database_url: Optional[str],
    shadow: Any, now: float,
) -> dict[str, Any]:
    record = store.find_by_task_id(root_task_id)
    if record is None:
        raise PipelineError(f"no stored submission for split blog task {task.id} (root={root_task_id!r}); cannot join")
    submission_id = record["submission_id"]
    store.join_init(submission_id, task.id)
    join = store.get_submission(submission_id)["join"]
    if join.get("state") in ("done", "fallback"):
        if join.get("state") == "fallback" and not join.get("late_applied"):
            # The page exists without the blog (fallback): this is the late blog, applied through the existing refine path.
            result = _handle_refine_round_completion(
                conn, ops, store, review_config, task, metadata, "blog", database_url, shadow, first_draft=True,
            )
            store.join_set(submission_id, {"late_applied": True})
            return result
        return {"action": "join_duplicate", "task_id": task.id, "root_task_id": root_task_id}
    blog = metadata["blog"]
    event_title = str(metadata.get("event_title") or "") or None
    half = {"task_id": task.id, "blog": blog, "event_title": event_title, "completed_at": now}
    record, newly = store.join_store_half(submission_id, "blog", half)
    if newly:
        _shadow_call(shadow, "record_completion", task_id=root_task_id, platform="blog", round_no=1, blog=blog, pack=record)
        if not record["join"].get("social"):
            _track_event_only(database_url, root_task_id, "join_waiting", "info", detail="Blog draft done; waiting for the Facebook/Instagram drafts")
    return _advance_join(conn, ops, store, review_config, submission_id, now, database_url, shadow)


def _advance_join(
    conn, ops, store, review_config, submission_id: str, now: float, database_url: Optional[str], shadow: Any, *, fallback: bool = False,
) -> dict[str, Any]:
    """Creates the page if the join is ready (both halves, or ``fallback`` with social only). Exactly one caller wins the claim."""
    record = store.get_submission(submission_id) or {}
    join = record.get("join") or {}
    root = record.get("root_task_id") or ((join.get("social") or {}).get("task_id")) or submission_id
    waiting = {"action": "join_waiting", "task_id": root, "root_task_id": root}
    social, blog_half = join.get("social"), join.get("blog")
    if join.get("state") in ("done", "fallback"):
        return {"action": "join_noop", "task_id": root, "root_task_id": root}
    if not social or (not blog_half and not fallback):
        return waiting
    if not store.join_claim(submission_id, now):
        return {"action": "join_noop", "task_id": root, "root_task_id": root}
    event_title = social["event_title"]
    kwargs: dict[str, Any] = {}
    if blog_half:
        blog = blog_half["blog"]
        blog_body, blog_seo = blog["body"], _blog_seo_payload(blog)
    else:
        blog_body, blog_seo = "", _blog_seo_payload({})
        kwargs["blog_state"] = "in_progress"
    try:
        review_url = create_review(
            review_config, task_id=root, event_title=event_title, date=record.get("date", ""),
            images=record.get("images", []), fb=social["fb"], ig=social["ig"], blog_body=blog_body, blog_seo=blog_seo,
            description=record.get("description"), quote=record.get("quote"), intake=record.get("intake"),
            submitter_name=record.get("submitter_name"), submitter_phone=record.get("submitter_phone"), **kwargs,
        )
    except ReviewClientError as exc:
        store.join_set(submission_id, {"state": "waiting", "claimed_at": None})  # releases the claim; the sweep or a retry tries again
        raise PipelineError(str(exc)) from exc
    except BaseException:
        store.join_set(submission_id, {"state": "waiting", "claimed_at": None})
        raise
    # From here on the page exists: record that first so nothing below can ever cause a second page.
    store.join_set(submission_id, {"state": "fallback" if not blog_half else "done", "review_url": review_url, "claimed_at": None})
    store.upsert_submission(submission_id, {"review_url": review_url})
    ops.add_comment(conn, root, CREATED_BY, f"Review page: {review_url}")
    _track_run(
        database_url, root, title=event_title, state="waiting", current_step="awaiting_review",
        step="stylus_done", status="ok", detail=review_url,
    )
    if blog_half:
        _track_event_only(database_url, root, "join_done", "ok", detail="Facebook/Instagram and blog drafts joined; review page created")
    else:
        _track_event_only(database_url, root, "join_done", "ok", detail="review page created without the blog draft (blog row in progress)")
        _track_event_only(
            database_url, root, "blog_late_fallback", "info",
            detail="blog draft not finished within the wait limit; the blog will be added to the page when it completes",
        )
    _link_submission_if_resolvable(database_url, root, review_url)
    result = {"action": "created_review_page", "task_id": root, "root_task_id": root, "review_url": review_url}
    if not blog_half:
        result["fallback"] = True
    return result


def sweep_join(
    conn, ops: KanbanOps, store: EventPostPipelineStore, review_config: ReviewClientConfig, *, now: float,
    blog_wait_seconds: float = DEFAULT_BLOG_WAIT_SECONDS, database_url: Optional[str] = None, shadow: Any = None,
) -> list[dict[str, Any]]:
    """Tick-hook entry point (registered by a later work package). For every split submission without a page yet:
    both halves present -> (re)try the page creation; only social and ``blog_wait_seconds`` elapsed since its
    completion -> create the page with the blog row in progress (fallback); only blog present -> keep waiting.
    Returns one result dict per page created (``action == "created_review_page"``) so the caller can send the
    single WhatsApp message. Never raises: a failing creation is logged and retried on the next tick."""
    created: list[dict[str, Any]] = []
    for record in store.join_pending():
        join = record.get("join") or {}
        social = join.get("social")
        if not social:
            continue
        if join.get("blog"):
            fallback = False
        elif now - float(social.get("completed_at") or now) >= blog_wait_seconds:
            fallback = True
        else:
            continue
        try:
            result = _advance_join(
                conn, ops, store, review_config, record["submission_id"], now, database_url, shadow, fallback=fallback,
            )
        except Exception as exc:  # noqa: BLE001 - one bad submission must not stop the sweep
            logger.error("event_post_pipeline: join sweep failed for submission=%s: %s", record.get("submission_id"), exc)
            continue
        if result.get("action") == "created_review_page":
            created.append(result)
    return created


# --- Step 5/6: reacting to a review-page action -----------------------------------------

def _platform_round_tasks(conn, ops: KanbanOps, root_task_id: str, platform: str) -> list:
    """Every redraft round already created for ``platform`` under ``root_task_id``, in
    creation order — queried against Kanban directly (never the local store) so the next
    round number is always correct even after a store loss or restart."""
    prefix = f"{root_task_id}-{platform}-r"
    tasks = ops.list_tasks(conn, tenant=TENANT, assignee="stylus", include_archived=True)
    matches = [t for t in tasks if (t.idempotency_key or "").startswith(prefix)]
    return sorted(matches, key=lambda t: int((t.idempotency_key or "").rsplit("r", 1)[-1]))


def _current_platform_task(conn, ops: KanbanOps, root_task_id: str, platform: str):
    """The most recent task that actually drafted ``platform``: the latest redraft round,
    or the original root task if there has never been one."""
    rounds = _platform_round_tasks(conn, ops, root_task_id, platform)
    return rounds[-1] if rounds else ops.get_task(conn, root_task_id)


def handle_review_action(
    conn, ops: KanbanOps, store: EventPostPipelineStore, *, task_id: str, platform: str, action: str, comment: str,
    database_url: Optional[str] = None, edited_text: Optional[str] = None, edited_seo: Optional[dict[str, Any]] = None,
    shadow: Any = None,
) -> dict[str, Any]:
    result = _handle_review_action(
        conn, ops, store, task_id=task_id, platform=platform, action=action, comment=comment, database_url=database_url,
        edited_text=edited_text, edited_seo=edited_seo,
    )
    if shadow is not None and platform == "blog":
        try:
            # What the reviewer is deciding on: their hand-edit if it was made against the current draft, else Stylus's draft. Read-only.
            current = _current_platform_task(conn, ops, task_id, platform)
            final_text: Optional[str] = None
            edited = False
            if action == "edited":
                final_text, edited = edited_text, True
            elif action == "approved" and current is not None:
                record = store.find_by_task_id(task_id) or store.get_submission(task_id) or {}
                manual = (record.get("manual_edits") or {}).get(platform) or {}
                if manual.get("base_task_id") == current.id and isinstance(manual.get("text"), str) and manual["text"].strip():
                    final_text, edited = manual["text"], True
                else:
                    final_text = _previous_draft_text(ops.latest_run(conn, current.id), platform)
            _shadow_call(shadow, "record_review_action", task_id=task_id, platform=platform, action=action, comment=comment or "", final_text=final_text, edited=edited)
        except Exception as exc:  # noqa: BLE001
            logger.warning("event_post_pipeline: shadow review-action capture failed (%s); ignored", type(exc).__name__)
    return result


def _handle_review_action(
    conn, ops: KanbanOps, store: EventPostPipelineStore, *, task_id: str, platform: str, action: str, comment: str,
    database_url: Optional[str] = None, edited_text: Optional[str] = None, edited_seo: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Step 5/6: a review-page button click. ``task_id`` here is the review page's
    recorded ``taskId`` — always the *root* Stylus task, per the skill's own contract
    (``lib/event-review.ts``'s ``EventReview.taskId`` is set once at page creation).
    """
    root_task = ops.get_task(conn, task_id)
    if root_task is None:
        raise PipelineError(f"review action referenced unknown task {task_id!r}")
    current_task = _current_platform_task(conn, ops, task_id, platform)
    if current_task is None:
        raise PipelineError(f"no drafting task found for platform={platform!r} under root task {task_id!r}")

    if action == "edited":
        return _handle_manual_edit(
            conn, ops, store, root_task=root_task, current_task=current_task, platform=platform,
            edited_text=edited_text, edited_seo=edited_seo, database_url=database_url,
        )

    if action in ("approved", "rejected"):
        outcome_line = f"{platform.upper()} {action.upper()} via review page: {defuse_fence_tags(comment)}" if comment else f"{platform.upper()} {action.upper()} via review page"
        ops.add_comment(conn, current_task.id, CREATED_BY, outcome_line)
        # Deliberately does NOT touch pipeline_runs' state/current_step: social-post-portal
        # owns deciding when a submission is fully done (see db.py's ownership-boundary
        # docstring) — this is only the timeline event.
        _track_event_only(database_url, task_id, "action_taken", "ok", detail=f"{action} {platform}")
        return {"action": action, "task_id": current_task.id, "platform": platform}

    if action == "refine":
        # ``task_id`` here is the Kanban root task id, never the store's own dict key
        # (submission_id, an intake-time slug — a different identity space, per
        # handle_intake()'s own upsert). Found the hard way alongside the review_url
        # recovery bug (2026-09-26, task t_9026ad85): ``store.get_submission(task_id)``
        # is a direct-key lookup that always misses here, and upserting "rounds" back
        # onto that same wrong key silently created a second, orphaned store record on
        # every single refine dispatch instead of updating the real submission.
        record = store.find_by_task_id(task_id) or store.get_submission(task_id) or {}
        store_key = record.get("submission_id") or task_id
        previous_draft = _previous_draft_text(ops.latest_run(conn, current_task.id), platform)
        # A reviewer's hand-edit of THIS draft supersedes what Stylus originally wrote:
        # a redraft must start from the version the reviewer actually shaped. Only honored
        # when it was made against the very task the redraft is built on (``base_task_id``)
        # — once a newer Stylus round exists, that round is the draft of record again.
        manual = (record.get("manual_edits") or {}).get(platform) or {}
        if manual.get("base_task_id") == current_task.id and isinstance(manual.get("text"), str) and manual["text"].strip():
            previous_draft = manual["text"]
        next_round = len(_platform_round_tasks(conn, ops, task_id, platform)) + 1
        idempotency_key = f"{task_id}-{platform}-r{next_round}"
        body = _refine_task_body(record=record, platform=platform, previous_draft=previous_draft, comment=comment)
        if platform == "blog" and not previous_draft.strip() and (record.get("join") or {}).get("mode") == "split":
            # Split intake, fallback page: the blog task never produced a draft, so there is nothing to revise.
            body = _fresh_blog_body(body)
        new_task_id = ops.create_task(
            conn, title=f"Redraft {platform.upper()} (round {next_round}): {defuse_fence_tags(root_task.title.removeprefix('Draft social copy: '))}",
            body=body, assignee="stylus", idempotency_key=idempotency_key, tenant=TENANT, created_by=CREATED_BY,
        )
        store.upsert_submission(store_key, {"rounds": {platform: {"task_id": new_task_id, "round": next_round, "status": "in_progress"}}})
        _track_run(
            database_url, task_id, title=_display_title(record, root_task.title.removeprefix("Draft social copy: ")),
            state="running", current_step="refine_drafting", step="refine_dispatched", status="ok",
            detail=f"{platform} round {next_round}",
        )
        return {"action": "refine_dispatched", "task_id": new_task_id, "platform": platform, "round": next_round}

    raise PipelineError(f"unknown review action {action!r}")


def _handle_manual_edit(
    conn, ops: KanbanOps, store: EventPostPipelineStore, *, root_task, current_task, platform: str,
    edited_text: Optional[str], edited_seo: Optional[dict[str, Any]], database_url: Optional[str],
) -> dict[str, Any]:
    """A reviewer rewrote ``platform``'s draft by hand on the review page (the portal
    has already validated it, and ``models.parse_review_action`` re-validated it here).

    This is Stylus's acknowledgement of the edit — it does two things:

    * comments the final text on the task that drafted this platform, so the Kanban
      history (which is what Stylus and any human read) shows what actually shipped;
    * records it in the store keyed by that task, so ``handle_review_action``'s refine
      branch redrafts from the edited text instead of Stylus's superseded original.

    It does NOT approve anything and does not touch ``pipeline_runs``' state — the
    portal sends a normal ``approved`` action afterwards when the reviewer chose
    "Save & approve", and the existing approve path handles that exactly as before."""
    if not isinstance(edited_text, str) or not edited_text.strip():
        raise PipelineError("manual edit has no text")
    lines = [f"{platform.upper()} EDITED by a reviewer on the review page — this manual version replaces Stylus's draft and must be used as the base for any redraft:", edited_text]
    if platform == "blog" and edited_seo:
        lines.append(
            f"SEO title: {edited_seo.get('seoTitle', '')}\nURL slug: {edited_seo.get('urlSlug', '')}\n"
            f"Focus keyword: {edited_seo.get('focusKeyword', '')}\n"
            f"Supporting keywords: {', '.join(edited_seo.get('supportingKeywords') or [])}\n"
            f"Meta description: {edited_seo.get('metaDescription', '')}"
        )
    ops.add_comment(conn, current_task.id, CREATED_BY, defuse_fence_tags("\n\n".join(lines)))
    record = store.find_by_task_id(root_task.id) or store.get_submission(root_task.id) or {}
    store_key = record.get("submission_id") or root_task.id
    store.upsert_submission(
        store_key,
        {"manual_edits": {platform: {"base_task_id": current_task.id, "text": edited_text, "seo": edited_seo}}},
    )
    _track_event_only(database_url, root_task.id, "action_taken", "ok", detail=f"edited {platform}")
    return {"action": "edited", "task_id": current_task.id, "platform": platform}


def _previous_draft_text(run, platform: str) -> str:
    if run is None or not isinstance(run.metadata, dict):
        return ""
    value = run.metadata.get(platform)
    if platform == "blog" and isinstance(value, dict):
        return str(value.get("body") or "")
    return str(value or "")


def _fresh_blog_body(body: str) -> str:
    """A blog redraft where the first draft was never produced: write it fresh from the pack (blog marker kept)."""
    body = body.replace(
        "Previous BLOG draft:\n",
        "Previous BLOG draft: none (the first draft was not produced; write the blog post fresh from the submission above).\n", 1,
    )
    return f"{BLOG_MARKER}\n{body}"


def _refine_task_body(*, record: dict[str, Any], platform: str, previous_draft: str, comment: str) -> str:
    return "\n\n".join(
        part for part in (
            "<untrusted_submission>",
            f"Date: {defuse_fence_tags(record.get('date', ''))}",
            f"Description: {defuse_fence_tags(record.get('description', ''))}",
            f"Quote: {defuse_fence_tags(record.get('quote', ''))}",
            "\n".join(render_intake_lines(record.get("intake") if isinstance(record.get("intake"), dict) else None, defuse_fence_tags)),
            "</untrusted_submission>",
            f"Previous {platform.upper()} draft:\n{defuse_fence_tags(previous_draft)}",
            f"Requested change (reviewer's own words):\n{defuse_fence_tags(comment)}",
            f"Only the {platform.upper()} content needs to be redrafted — the other platforms are "
            "unaffected and already handled separately.",
        ) if part
    )
