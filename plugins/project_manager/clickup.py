"""Minimal ClickUp REST v2 client for the ProjectManager plugin.

Sync ``urllib``, same style as the pipeline plugins' HTTP clients. The token
comes from env ``CLICKUP_API_TOKEN`` (Doppler vidu/prd -> /opt/data/.env) and
is never logged, echoed or put in an exception message.

Reads (official docs, read 2026-10-14):
- ``GET /v2/team/{team_id}/task`` — "Responses are limited to 100 tasks per
  page", ``page`` starts at 0. The docs give no last-page field, so we stop on
  the first page with fewer than 100 tasks (an empty page also stops), with a
  hard page cap so a misbehaving API can never loop forever.
- Rate limit: 100 requests/minute per token on Free/Unlimited/Business; a 429
  carries ``X-RateLimit-Reset`` (Unix seconds). Reads wait for the reset
  (bounded) and retry; 5xx/network errors on reads retry with backoff.

Writes (create task, update task, comment) are NEVER retried on an ambiguous
failure (timeout, 5xx, connection reset) — the request may have been applied.
Only a 429, which ClickUp rejects before processing, is retried once. The
approval executor marks anything else ``failed`` for a human to look at.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Optional

logger = logging.getLogger("plugins.project_manager")

API_BASE = "https://api.clickup.com/api/v2"
PAGE_SIZE = 100
MAX_PAGES = 100                 # 10,000 tasks; far above the ~131 we have
DEFAULT_TIMEOUT_SECONDS = 30
READ_RETRIES = 3
MAX_RATE_LIMIT_WAIT_SECONDS = 65


class ClickUpError(RuntimeError):
    """A ClickUp request failed. ``status`` is the HTTP status, if any."""

    def __init__(self, message: str, status: Optional[int] = None, ambiguous: bool = False):
        super().__init__(message)
        self.status = status
        # True when a write may or may not have been applied (timeout, 5xx, reset).
        self.ambiguous = ambiguous


@dataclass
class _Response:
    status: int
    body: Any
    headers: dict[str, str]


def token_from_env() -> str:
    token = os.environ.get("CLICKUP_API_TOKEN", "").strip()
    if not token:
        raise ClickUpError("CLICKUP_API_TOKEN is not set")
    return token


class ClickUpClient:
    def __init__(
        self,
        token: str,
        *,
        base_url: str = API_BASE,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        opener: Optional[Callable[..., Any]] = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
    ):
        if not token:
            raise ClickUpError("empty ClickUp token")
        self._token = token
        self._base = base_url.rstrip("/")
        self._timeout = timeout
        self._open = opener or urllib.request.urlopen
        self._sleep = sleep
        self._clock = clock

    def __repr__(self) -> str:  # never expose the token
        return f"ClickUpClient(base_url={self._base!r})"

    # ── transport ────────────────────────────────────────────────────────
    def _request(self, method: str, path: str, *, query: Optional[list[tuple[str, str]]] = None,
                 payload: Optional[dict[str, Any]] = None) -> _Response:
        url = f"{self._base}{path}"
        if query:
            url = f"{url}?{urllib.parse.urlencode(query)}"
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": self._token,
            "Content-Type": "application/json",
            "Accept": "application/json",
        })
        try:
            with self._open(req, timeout=self._timeout) as resp:
                raw = resp.read().decode() or "null"
                return _Response(resp.status, json.loads(raw), {k.lower(): v for k, v in resp.headers.items()})
        except urllib.error.HTTPError as exc:
            try:
                body = json.loads(exc.read().decode() or "null")
            except Exception:
                body = None
            headers = {k.lower(): v for k, v in (exc.headers or {}).items()}
            return _Response(exc.code, body, headers)
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
            raise ClickUpError(f"{method} {path}: network error: {type(exc).__name__}", ambiguous=True) from None

    def _rate_limit_wait(self, resp: _Response) -> float:
        reset = resp.headers.get("x-ratelimit-reset")
        try:
            wait = float(reset) - self._clock() + 1 if reset else 60.0
        except ValueError:
            wait = 60.0
        return max(1.0, min(wait, MAX_RATE_LIMIT_WAIT_SECONDS))

    @staticmethod
    def _error_text(resp: _Response) -> str:
        body = resp.body
        if isinstance(body, dict):
            return str(body.get("err") or body.get("error") or "")[:200]
        return ""

    def _get(self, path: str, query: Optional[list[tuple[str, str]]] = None) -> Any:
        last_error = ""
        for attempt in range(READ_RETRIES + 1):
            try:
                resp = self._request("GET", path, query=query)
            except ClickUpError as exc:
                last_error = str(exc)
                if attempt < READ_RETRIES:
                    self._sleep(2 ** attempt)
                    continue
                raise ClickUpError(f"GET {path} failed: {last_error}") from None
            if resp.status == 200:
                return resp.body
            if resp.status == 429 and attempt < READ_RETRIES:
                self._sleep(self._rate_limit_wait(resp))
                continue
            if resp.status >= 500 and attempt < READ_RETRIES:
                self._sleep(2 ** attempt)
                continue
            raise ClickUpError(f"GET {path} -> HTTP {resp.status} {self._error_text(resp)}".strip(), status=resp.status)
        raise ClickUpError(f"GET {path} failed after retries")  # pragma: no cover

    def _write(self, method: str, path: str, payload: dict[str, Any]) -> Any:
        for attempt in range(2):
            resp = self._request(method, path, payload=payload)  # network error -> ambiguous ClickUpError
            if resp.status in (200, 201):
                return resp.body
            if resp.status == 429 and attempt == 0:
                self._sleep(self._rate_limit_wait(resp))
                continue
            raise ClickUpError(
                f"{method} {path} -> HTTP {resp.status} {self._error_text(resp)}".strip(),
                status=resp.status, ambiguous=resp.status >= 500,
            )
        raise ClickUpError(f"{method} {path} rate-limited twice", status=429)

    # ── reads ────────────────────────────────────────────────────────────
    def get_team_tasks(self, team_id: str) -> list[dict[str, Any]]:
        """Every task in the workspace, open and closed, subtasks included."""
        tasks: list[dict[str, Any]] = []
        seen: set[str] = set()
        for page in range(MAX_PAGES):
            body = self._get(f"/team/{urllib.parse.quote(team_id)}/task", [
                ("page", str(page)), ("include_closed", "true"), ("subtasks", "true"),
                ("order_by", "created"), ("reverse", "true"),
            ])
            batch = (body or {}).get("tasks")
            if not isinstance(batch, list):
                raise ClickUpError("unexpected task page shape: no 'tasks' list")
            for task in batch:
                tid = str(task.get("id"))
                if tid not in seen:   # a task created mid-pagination can shift pages
                    seen.add(tid)
                    tasks.append(task)
            if len(batch) < PAGE_SIZE:
                return tasks
        raise ClickUpError(f"task pagination exceeded {MAX_PAGES} pages; refusing to continue")

    def get_list(self, list_id: str) -> dict[str, Any]:
        return self._get(f"/list/{urllib.parse.quote(list_id)}")

    def get_space(self, space_id: str) -> dict[str, Any]:
        return self._get(f"/space/{urllib.parse.quote(space_id)}")

    def get_task(self, task_id: str) -> dict[str, Any]:
        return self._get(f"/task/{urllib.parse.quote(task_id)}")

    # ── writes (only the approval executor calls these) ──────────────────
    def create_task(self, list_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return self._write("POST", f"/list/{urllib.parse.quote(list_id)}/task", body)

    def update_task(self, task_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return self._write("PUT", f"/task/{urllib.parse.quote(task_id)}", body)

    def create_comment(self, task_id: str, text: str) -> dict[str, Any]:
        return self._write("POST", f"/task/{urllib.parse.quote(task_id)}/comment",
                           {"comment_text": text, "notify_all": False})
