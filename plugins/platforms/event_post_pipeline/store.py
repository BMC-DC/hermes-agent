"""Durable local state for the event-post-pipeline plugin.

Mirrors ``plugins/teams_pipeline/store.py``'s shape (atomic temp-file-replace
JSON, one bucket per record kind). This is a fast-lookup cache only, never
the source of truth: every fact here (task id, review-page slug, round
number) is also recoverable from Kanban's own comment history, exactly as
the skill file it replaces already relies on ("garbage collection only
prunes old audit events and worker logs, never a task's actual content").
A missing or stale store must never cause incorrect behavior, only a slower
recovery path.
"""

from __future__ import annotations

import json
import os
import threading
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Optional

from hermes_constants import get_hermes_home

DEFAULT_STORE_FILENAME = "event_post_pipeline_store.json"
_BUCKETS = ("submissions",)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def resolve_store_path(path: Optional[str] = None) -> Path:
    explicit = str(path).strip() if path is not None else ""
    env_path = os.getenv("EVENT_POST_PIPELINE_STORE_PATH", "").strip()
    return Path(explicit or env_path) if (explicit or env_path) else get_hermes_home() / DEFAULT_STORE_FILENAME


class EventPostPipelineStore:
    """JSON-backed cache keyed by ``submission_id``.

    Each record: ``{root_task_id, review_slug, event_title, rounds:
    {platform: {task_id, round, status}}, created_at, updated_at}``.
    """

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._state: dict[str, dict[str, Any]] = {bucket: {} for bucket in _BUCKETS}
        self._load()

    def _load(self) -> None:
        with self._lock:
            data = json.loads(self.path.read_text(encoding="utf-8") or "{}") if self.path.exists() else None
            if isinstance(data, dict):
                self._state = {bucket: dict(data.get(bucket) or {}) for bucket in _BUCKETS}

    def _persist(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile("w", encoding="utf-8", dir=str(self.path.parent), delete=False) as tmp:
            json.dump(self._state, tmp, indent=2, sort_keys=True)
            tmp.flush()
            tmp_path = Path(tmp.name)
        tmp_path.replace(self.path)

    @contextmanager
    def _exclusive(self):
        """Cross-process exclusive section (the S and B completion hooks run in different worker
        processes, plus the gateway's sweep): thread lock + ``flock`` on a sidecar file, with the
        in-memory state re-read from disk on entry so a long-lived instance never writes back a
        stale view. Degrades to the thread lock alone where ``fcntl`` is unavailable."""
        with self._lock:
            handle = None
            try:
                import fcntl
                self.path.parent.mkdir(parents=True, exist_ok=True)
                handle = open(str(self.path) + ".lock", "a")
                fcntl.flock(handle, fcntl.LOCK_EX)
            except (ImportError, OSError):
                handle = None
            try:
                self._load()
                yield
            finally:
                if handle is not None:
                    handle.close()  # releases the flock

    def get_submission(self, submission_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            record = self._state["submissions"].get(submission_id)
            return deepcopy(record) if isinstance(record, dict) else None

    def upsert_submission(self, submission_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        """Shallow-merge ``patch`` over the existing record (``rounds`` merges one level deeper)."""
        with self._exclusive():
            existing = self._state["submissions"].get(submission_id, {})
            merged = {**existing, **deepcopy(patch)}
            # Per-platform dicts: merge one level deeper so patching one platform
            # never drops another's entry.
            for nested in ("rounds", "manual_edits"):
                if nested in patch:
                    merged[nested] = {**existing.get(nested, {}), **deepcopy(patch[nested])}
            merged["submission_id"] = submission_id
            merged.setdefault("created_at", existing.get("created_at") or _utc_now_iso())
            merged["updated_at"] = _utc_now_iso()
            self._state["submissions"][submission_id] = merged
            self._persist()
            return deepcopy(merged)

    def find_by_task_id(self, task_id: str) -> Optional[dict[str, Any]]:
        """Find the submission record whose root task, or one of its round tasks, matches ``task_id``."""
        with self._lock:
            for record in self._state["submissions"].values():
                if record.get("root_task_id") == task_id:
                    return deepcopy(record)
                for round_info in (record.get("rounds") or {}).values():
                    if round_info.get("task_id") == task_id:
                        return deepcopy(record)
        return None

    # --- Split-intake join (blog_enrichment.split) ---------------------------------------
    #
    # ``record["join"] = {"mode": "split", "state": waiting|creating|done|fallback, "blog_task_id",
    # "social": {task_id, event_title, fb, ig, completed_at}, "blog": {task_id, blog, completed_at, event_title},
    # "claimed_at", "review_url", ...}``. Every mutation below is one atomic read-modify-write under
    # ``_exclusive`` so two worker processes can never both win the page-creation claim.

    def join_init(self, submission_id: str, blog_task_id: Optional[str]) -> None:
        with self._exclusive():
            existing = self._state["submissions"].get(submission_id, {})
            if (existing.get("join") or {}).get("mode") == "split":
                return
            self._state["submissions"][submission_id] = {
                **existing, "submission_id": submission_id,
                "join": {"mode": "split", "state": "waiting", "blog_task_id": blog_task_id},
                "updated_at": _utc_now_iso(),
            }
            self._persist()

    def join_store_half(self, submission_id: str, half: str, data: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        """Stores ``half`` ("social" | "blog") once. Returns ``(record, newly_stored)``; a repeat (duplicate
        completion event / retry) keeps the first value and returns ``newly_stored=False``."""
        with self._exclusive():
            existing = self._state["submissions"].get(submission_id)
            if not isinstance(existing, dict):
                raise KeyError(submission_id)
            join = dict(existing.get("join") or {"mode": "split", "state": "waiting"})
            newly = not isinstance(join.get(half), dict)
            if newly:
                join[half] = deepcopy(data)
                self._state["submissions"][submission_id] = {**existing, "join": join, "updated_at": _utc_now_iso()}
                self._persist()
            return deepcopy(self._state["submissions"][submission_id]), newly

    def join_claim(self, submission_id: str, now: float, *, stale_after: float = 300.0) -> bool:
        """Atomic ``waiting -> creating``. A ``creating`` claim older than ``stale_after`` seconds (a worker that died
        mid-create) can be taken over. Exactly one caller gets ``True``."""
        with self._exclusive():
            existing = self._state["submissions"].get(submission_id) or {}
            join = dict(existing.get("join") or {})
            state = join.get("state")
            if state == "creating" and now - float(join.get("claimed_at") or 0) >= stale_after:
                state = "waiting"
            if state != "waiting":
                return False
            join.update({"state": "creating", "claimed_at": now})
            self._state["submissions"][submission_id] = {**existing, "join": join, "updated_at": _utc_now_iso()}
            self._persist()
            return True

    def join_set(self, submission_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        """Shallow-merge ``patch`` into the join object (e.g. release a claim, mark done/fallback)."""
        with self._exclusive():
            existing = self._state["submissions"].get(submission_id) or {}
            join = {**(existing.get("join") or {}), **deepcopy(patch)}
            self._state["submissions"][submission_id] = {**existing, "join": join, "updated_at": _utc_now_iso()}
            self._persist()
            return deepcopy(self._state["submissions"][submission_id])

    def join_pending(self) -> list[dict[str, Any]]:
        """Every record whose join has not produced a page yet (``waiting``, or a ``creating`` claim that may be stale)."""
        with self._exclusive():
            return [
                deepcopy(r) for r in self._state["submissions"].values()
                if isinstance(r, dict) and (r.get("join") or {}).get("state") in ("waiting", "creating")
            ]
