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

    def get_submission(self, submission_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            record = self._state["submissions"].get(submission_id)
            return deepcopy(record) if isinstance(record, dict) else None

    def upsert_submission(self, submission_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        """Shallow-merge ``patch`` over the existing record (``rounds`` merges one level deeper)."""
        with self._lock:
            existing = self._state["submissions"].get(submission_id, {})
            merged = {**existing, **deepcopy(patch)}
            if "rounds" in patch:
                merged["rounds"] = {**existing.get("rounds", {}), **deepcopy(patch["rounds"])}
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
