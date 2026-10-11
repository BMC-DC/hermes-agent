"""Shadow mode (task 08 / W05): run the site service and the blog linter on real runs, RECORD what they say, change nothing.

Three observation points, all log-only:

* ``record_intake``     after the Stylus task has been created: ``/v1/context-pack`` on the submission -> suggested links, passages,
                        ``missing_info`` (flag ``shadow.site_context``). Never written into the Kanban task body.
* ``record_completion`` at a blog draft's completion, before the review page is created/updated: ``lint_blog`` + ``/v1/verify-links``
                        (flag ``shadow.lint``). The draft is read, never edited, never rejected.
* ``record_review_action``  a reviewer's approve/reject/edit/refine of the blog (recorded whenever either flag is on): which links the final
                        text carries, so the report can compare suggested vs used vs approved. Local string work only, no network.

Guarantees (each has a test in ``tests/plugins/test_event_post_pipeline_shadow.py``):

* **Flags default OFF.** ``platforms.event_post_pipeline.extra.blog_enrichment.shadow.{site_context,lint}`` must be the literal ``true``.
  With both off, ``ShadowRecorder.from_extra`` returns ``None`` and the callers' ``if shadow is not None`` branches never run: the
  pipeline's behaviour, results, Kanban bodies, visualizer events and review calls are byte-for-byte what they were.
* **Never raises, never blocks a transition.** Every public method catches ``Exception`` (logged with the class name only, never the
  text), the site client is fail-open with short timeouts, and the intake call runs on a daemon thread so intake does not wait for it.
* **Nothing the writer, the guard, the review page or a notification sees is touched.** No ``pipeline_events`` rows, no Kanban comment, no
  Postgres: the only output is an append to a local JSONL file (the least risky place; the visualizer tests pin exact event sequences).
* **No secrets, truncated text.** Records hold ids, urls of *site* pages, tier/evidence labels, lint finding ids and short messages,
  counts and latencies, plus short control-character-free excerpts of the submission (``EXCERPT_*`` chars). The reviewer's comment is never
  recorded (only its length).

Record file: ``blog_shadow.jsonl`` next to the plugin store (``store_path``), or ``blog_enrichment.shadow.log_path``. One JSON object per
line, ``schema: 1``, kinds ``intake`` | ``completion`` | ``review_action`` | ``research`` | ``research_result`` (research step, DESIGN_11 2.6);
related lines share ``task_id`` (the root Stylus task id).
Read it with ``vps-scripts/shadow-report.py`` (see that script's header).
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from plugins.platforms.event_post_pipeline.intake import parse_intake, to_service_pack
from plugins.platforms.event_post_pipeline.site_client import SiteClient, SiteClientConfig, SiteResult

logger = logging.getLogger("plugins.platforms.event_post_pipeline")

SCHEMA = 1
LOG_FILENAME = "blog_shadow.jsonl"
DEFAULT_TIMEOUT_S = 2.0
MAX_TIMEOUT_S = 5.0
EXCERPT_DESCRIPTION = 160
EXCERPT_QUOTE = 100
MAX_PACK_FIELD_CHARS = 8000       # keeps the request under the client's 60 KB cap for any sane submission
MAX_LINE_BYTES = 48 * 1024
MAX_LINKS = 40
MAX_FINDINGS = 40
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f  ]")
_MD_LINK = re.compile(r"\[([^\]\n]{0,200})\]\(\s*<?(https?://[^\s)>]+)>?[^)]*\)")
_BARE_URL = re.compile(r"https?://[^\s)\]>\"'<]+")
_write_lock = threading.Lock()


# ------------------------------------------------------------------ config
@dataclass(frozen=True)
class ShadowConfig:
    site_context: bool = False
    lint: bool = False
    research: bool = False                # ``blog_enrichment.research.enabled``: record the research step's start and result
    timeout_s: float = DEFAULT_TIMEOUT_S
    service_url: str = ""
    log_path: Optional[str] = None
    background_intake: bool = True        # run the intake call on a daemon thread (tests set False for determinism)

    @property
    def enabled(self) -> bool:
        return self.site_context or self.lint or self.research

    @classmethod
    def from_extra(cls, extra: Optional[dict[str, Any]]) -> "ShadowConfig":
        """From ``platforms.event_post_pipeline.extra``. Anything missing, mistyped or not the literal ``True`` is OFF."""
        be = (extra or {}).get("blog_enrichment")
        rc = be.get("research") if isinstance(be, dict) else None
        research = isinstance(rc, dict) and rc.get("enabled") is True
        sh = be.get("shadow") if isinstance(be, dict) else None
        if not isinstance(sh, dict):
            sh = {}
            if not research:
                return cls()
        timeout = sh.get("timeout_s", DEFAULT_TIMEOUT_S)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= MAX_TIMEOUT_S:
            timeout = DEFAULT_TIMEOUT_S
        url = be.get("service_url")
        if not isinstance(url, str) or not url:
            ss = (extra or {}).get("site_service")
            url = ss.get("url") if isinstance(ss, dict) else ""
        log_path = sh.get("log_path")
        return cls(
            site_context=sh.get("site_context") is True, lint=sh.get("lint") is True, research=research, timeout_s=float(timeout),
            service_url=url if isinstance(url, str) else "", log_path=log_path if isinstance(log_path, str) and log_path.strip() else None,
        )


# ------------------------------------------------------------------ small helpers (pure)
def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def clean(value: Any, limit: int) -> str:
    """Printable, single-line, truncated: safe to store and to print in a terminal."""
    text = _CONTROL.sub(" ", str(value if value is not None else ""))
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def norm_url(url: str) -> str:
    """Comparison form of a link: lowercase scheme/host, no www, no fragment/query-tracking, no trailing slash."""
    u = (url or "").strip().rstrip(".,;:!?)")
    u = re.sub(r"#.*$", "", u)
    u = re.sub(r"\?.*$", "", u)
    u = re.sub(r"^(https?://)www\.", r"\1", u, flags=re.I)
    m = re.match(r"^(https?://[^/]+)(.*)$", u, flags=re.I)
    if m:
        u = m.group(1).lower() + m.group(2)
    return u.rstrip("/")


def extract_links(text: Any) -> list[dict[str, str]]:
    """Every http(s) link in markdown/plain text (markdown links with anchor, then bare URLs), in order, de-duplicated by normalised url."""
    body = text if isinstance(text, str) else ""
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    spans: list[tuple[int, int]] = []
    for m in _MD_LINK.finditer(body):
        spans.append(m.span())
        key = norm_url(m.group(2))
        if key not in seen:
            seen.add(key)
            out.append({"url": clean(m.group(2), 300), "anchor": clean(m.group(1), 80)})
    for m in _BARE_URL.finditer(body):
        if any(a <= m.start() < b for a, b in spans):
            continue
        url = m.group(0).rstrip(".,;:!?")
        key = norm_url(url)
        if key not in seen:
            seen.add(key)
            out.append({"url": clean(url, 300), "anchor": ""})
    return out[:MAX_LINKS]


def _blog_text(blog: Any) -> str:
    if isinstance(blog, dict):
        return str(blog.get("body") or "")
    return blog if isinstance(blog, str) else ""


def _summarise_pack(pack: dict[str, Any]) -> dict[str, Any]:
    desc, quote = str(pack.get("description") or ""), str(pack.get("quote") or "")
    return {"date": clean(pack.get("date"), 20), "description_chars": len(desc), "description_words": len(desc.split()),
            "description_excerpt": clean(desc, EXCERPT_DESCRIPTION), "quote_chars": len(quote), "quote_excerpt": clean(quote, EXCERPT_QUOTE)}


def _candidate_row(c: Any) -> dict[str, Any]:
    if not isinstance(c, dict):
        return {}
    ev = c.get("evidence")
    return {"page_id": clean(c.get("page_id"), 160), "url": clean(c.get("url"), 300), "kind": clean(c.get("kind"), 20), "tier": clean(c.get("tier"), 12),
            "tier_reason": clean(c.get("tier_reason"), 200), "same_event": clean(c.get("same_event"), 12) or None,
            "evidence_types": sorted({clean(e.get("type"), 40) for e in ev if isinstance(e, dict)}) if isinstance(ev, list) else [],
            "warnings": [clean(w, 160) for w in (c.get("warnings") or [])[:3]] if isinstance(c.get("warnings"), list) else []}


def _missing_rows(mi: Any) -> dict[str, Any]:
    if not isinstance(mi, dict):
        return {}
    items = mi.get("items") if isinstance(mi.get("items"), list) else []
    return {"ready_to_draft": mi.get("ready_to_draft") if isinstance(mi.get("ready_to_draft"), bool) else None,
            "items": [{"field": clean(i.get("field"), 40), "required": bool(i.get("required")), "why": clean(i.get("why"), 160)}
                      for i in items[:12] if isinstance(i, dict)],
            "date_conflict": bool(mi.get("date_conflict"))}


# ------------------------------------------------------------------ the recorder
class ShadowRecorder:
    """One per call site (cheap). ``from_extra`` returns ``None`` when both flags are off, so callers can't accidentally do work."""

    def __init__(self, config: ShadowConfig, log_path: Path, client: Optional[SiteClient] = None):
        self.config = config
        self.log_path = Path(log_path)
        self._client = client
        self._threads: list[threading.Thread] = []

    @classmethod
    def from_extra(cls, extra: Optional[dict[str, Any]], store_path: Optional[Path] = None, client: Optional[SiteClient] = None) -> Optional["ShadowRecorder"]:
        try:
            cfg = ShadowConfig.from_extra(extra)
            if not cfg.enabled:
                return None
            if cfg.log_path:
                path = Path(cfg.log_path)
            else:
                if store_path is None:
                    from plugins.platforms.event_post_pipeline.store import resolve_store_path
                    store_path = resolve_store_path((extra or {}).get("store_path"))
                path = Path(store_path).parent / LOG_FILENAME
            return cls(cfg, path, client)
        except Exception as exc:  # noqa: BLE001 - config trouble must leave the pipeline exactly as it was
            logger.warning("event_post_pipeline shadow: disabled for this call (%s)", type(exc).__name__)
            return None

    # -- plumbing
    def _site(self) -> SiteClient:
        if self._client is None:
            url = self.config.service_url
            kwargs: dict[str, Any] = {"enabled": True, "timeout_s": self.config.timeout_s}
            if url:
                kwargs["base_url"] = url.rstrip("/")
            self._client = SiteClient(SiteClientConfig(**kwargs))
        return self._client

    def _append(self, record: dict[str, Any]) -> None:
        record = {"schema": SCHEMA, "ts": _now(), **record}
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        if len(line.encode("utf-8")) > MAX_LINE_BYTES:      # drop the bulky parts first, keep the verdict
            for key in ("passages", "links", "findings", "verify_links", "optional_links", "dropped"):
                record.pop(key, None)
            record["truncated_record"] = True
            line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        data = (line + "\n").encode("utf-8")
        with _write_lock:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.log_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                try:
                    import fcntl
                    fcntl.flock(fd, fcntl.LOCK_EX)
                except Exception:  # noqa: BLE001 - not available / not needed on every platform
                    pass
                os.write(fd, data)
            finally:
                os.close(fd)

    def _safe(self, what: str, fn, *args, **kwargs) -> None:
        try:
            fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            logger.warning("event_post_pipeline shadow: %s failed (%s); ignored", what, type(exc).__name__)

    def wait(self, timeout: float = 10.0) -> None:
        """Join background intake threads (tests; a worker about to exit)."""
        for t in list(self._threads):
            t.join(timeout)

    # -- 1. intake
    def record_intake(self, *, submission_id: str, task_id: str, event_pack: dict[str, Any]) -> None:
        if not self.config.site_context:
            return
        pack = {"date": clean(event_pack.get("date"), 40), "description": str(event_pack.get("description") or "")[:MAX_PACK_FIELD_CHARS],
                "quote": str(event_pack.get("quote") or "")[:MAX_PACK_FIELD_CHARS]}
        if intake := parse_intake(event_pack):  # intake v2 (W08): the richer fields, mapped to the service's pack keys (INTAKE_FORM_SPEC §6b)
            for key, value in to_service_pack(event_pack, intake).items():
                pack.setdefault(key, [v[:MAX_PACK_FIELD_CHARS] for v in value] if isinstance(value, list) else str(value)[:MAX_PACK_FIELD_CHARS])
        if self.config.background_intake:
            t = threading.Thread(target=self._safe, args=("intake", self._intake, submission_id, task_id, pack), name="bmc-shadow-intake", daemon=True)
            self._threads.append(t)
            t.start()
        else:
            self._safe("intake", self._intake, submission_id, task_id, pack)

    def _intake(self, submission_id: str, task_id: str, pack: dict[str, Any]) -> None:
        t0 = time.monotonic()
        rec: dict[str, Any] = {"kind": "intake", "submission_id": clean(submission_id, 80), "task_id": clean(task_id, 80), "request": _summarise_pack(pack)}
        client = self._site()
        health = client.health()
        rec["index_built_at"] = clean((health.meta or {}).get("index_built_at"), 30) if health.available else None
        rec["stale_index"] = bool(health.stale) if health.available else None
        if not health.available:
            rec.update(ok=False, reason=clean(health.reason, 80), latency_ms=int((time.monotonic() - t0) * 1000))
            self._append(rec)
            return
        res = client.context_pack(pack)
        rec["latency_ms"] = int((time.monotonic() - t0) * 1000)
        rec["ok"] = bool(res.available)
        if not res.available:
            rec["reason"] = clean(res.reason, 120)
            self._append(rec)
            return
        data = res.data or {}
        rec.update(
            service_version=clean((res.meta or {}).get("service_version"), 20), corpus_hash=clean(res.corpus_hash, 40), request_id=clean((res.meta or {}).get("request_id"), 40),
            links=[r for r in (_candidate_row(c) for c in (data.get("candidates") or [])[:MAX_LINKS]) if r],
            optional_links=[{"page_id": clean(o.get("page_id"), 160), "url": clean(o.get("url"), 300), "label": clean(o.get("label"), 60)}
                            for o in (data.get("optional_links") or [])[:10] if isinstance(o, dict)],
            weak_hidden=data.get("weak_hidden") if isinstance(data.get("weak_hidden"), int) else None, program_found=data.get("program_found") if isinstance(data.get("program_found"), bool) else None,
            passages=[{"page_id": clean(p.get("page_id"), 160), "title": clean(p.get("title"), 100), "heading": clean(p.get("heading"), 100)}
                      for p in (data.get("passages") or [])[:6] if isinstance(p, dict)],
            missing_info=_missing_rows(data.get("missing_info")),
        )
        self._append(rec)

    # -- 2. completion
    def record_completion(self, *, task_id: str, platform: str, round_no: Optional[int], blog: Any, pack: Optional[dict[str, Any]],
                          brief: Optional[dict[str, Any]] = None) -> None:
        if not self.config.lint:
            return
        self._safe("completion", self._completion, task_id, platform, round_no, blog, pack or {}, brief)

    def _completion(self, task_id: str, platform: str, round_no: Optional[int], blog: Any, pack: dict[str, Any],
                    brief: Optional[dict[str, Any]] = None) -> None:
        from plugins.platforms.event_post_pipeline.blog_lint import lint_blog

        rec: dict[str, Any] = {"kind": "completion", "task_id": clean(task_id, 80), "platform": clean(platform, 8), "round": round_no,
                               "blog_shape": "object" if isinstance(blog, dict) else "string" if isinstance(blog, str) else type(blog).__name__,
                               "body_words": len(_blog_text(blog).split()), "draft_links": extract_links(_blog_text(blog))}
        if isinstance(brief, dict):  # research step: was a brief delivered to this blog task, and how much of it did the draft use
            rec["brief"] = {"delivered": bool(brief.get("delivered")), "sha": clean(brief.get("sha"), 12) or None,
                            "links_offered": _int(brief.get("links_offered")), "links_used": _int(brief.get("links_used"))}
        try:
            rep = lint_blog(blog, {k: pack.get(k) for k in ("date", "description", "quote") if pack.get(k)})
            findings = [f for f in (rep.get("findings") or []) if isinstance(f, dict)]
            rec["lint"] = {"ok": bool(rep.get("ok")), "hard": sum(1 for f in findings if f.get("level") == "hard"), "warn": sum(1 for f in findings if f.get("level") == "warn"),
                           "skipped": [clean(s if isinstance(s, str) else (s.get("rule") if isinstance(s, dict) else s), 60) for s in (rep.get("skipped") or [])[:10]]}
            rec["findings"] = [{"id": clean(f.get("id"), 8), "level": clean(f.get("level"), 6), "message": clean(f.get("message"), 160)} for f in findings[:MAX_FINDINGS]]
        except Exception as exc:  # noqa: BLE001
            rec["lint"] = {"error": type(exc).__name__}
        suggested = self._suggested_ids_for(task_id)
        t0 = time.monotonic()
        res = self._site().verify_links(blog if isinstance(blog, (dict, str)) else "", candidates=suggested)
        rec["verify_links"] = self._verify_row(res, suggested, int((time.monotonic() - t0) * 1000))
        self._append(rec)

    @staticmethod
    def _verify_row(res: SiteResult, suggested: Optional[list[str]], ms: int) -> dict[str, Any]:
        row: dict[str, Any] = {"available": bool(res.available), "latency_ms": ms, "closed_list_checked": suggested is not None}
        if not res.available:
            row["reason"] = clean(res.reason, 120)        # "links NOT verified": never reported as a pass
            return row
        d = res.data or {}
        row.update(passed=res.passed, errors=d.get("errors") if isinstance(d.get("errors"), int) else None, warnings=d.get("warnings") if isinstance(d.get("warnings"), int) else None,
                   placeholders=len(d["placeholders"]) if isinstance(d.get("placeholders"), list) else None, request_id=clean((res.meta or {}).get("request_id"), 40),
                   links=[{"url": clean(l.get("url"), 300), "findings": sorted({clean(f.get("code"), 40) for f in (l.get("findings") or []) if isinstance(f, dict)})}
                          for l in (d.get("links") or [])[:MAX_LINKS] if isinstance(l, dict)])
        return row

    def _suggested_ids_for(self, task_id: str) -> Optional[list[str]]:
        """Page ids the shadow intake call suggested for this task (strong candidates + optional links); ``None`` (skip the closed-list
        check) when there is no successful intake record for it."""
        last = None
        try:
            for r in read_records(self.log_path):
                if r.get("kind") == "intake" and r.get("task_id") == task_id and r.get("ok"):
                    last = r
        except Exception:  # noqa: BLE001
            return None
        if last is None:
            return None
        ids = [x.get("page_id") for x in (last.get("links") or []) + (last.get("optional_links") or []) if isinstance(x, dict) and x.get("page_id")]
        return sorted(set(ids))

    # -- 2b. research step (flag research.enabled)
    def record_research_start(self, *, submission_id: str, task_id: str, research_task_id: Optional[str], service_ok: bool, corpus_hash: str,
                              allowed_urls_n: int, handoff: bool, reason: Optional[str] = None, latency_ms: Optional[int] = None) -> None:
        if not self.config.research:
            return
        self._safe("research", self._research_start, submission_id, task_id, research_task_id, service_ok, corpus_hash, allowed_urls_n, handoff, reason, latency_ms)

    def _research_start(self, submission_id, task_id, research_task_id, service_ok, corpus_hash, allowed_urls_n, handoff, reason, latency_ms) -> None:
        self._append({"kind": "research", "submission_id": clean(submission_id, 80), "task_id": clean(task_id, 80),
                      "research_task_id": clean(research_task_id, 80) or None, "service_ok": bool(service_ok), "corpus_hash": clean(corpus_hash, 40),
                      "allowed_urls_n": _int(allowed_urls_n), "handoff": bool(handoff), "latency_ms": latency_ms if isinstance(latency_ms, int) else None})
        if research_task_id is None:  # no R: the outcome is already known
            self._append({"kind": "research_result", "task_id": clean(task_id, 80), "outcome": "skipped", "reason": clean(reason, 120)})

    def record_research_result(self, *, task_id: Optional[str], research_task_id: str, outcome: str, reason: Optional[str] = None,
                               duration_s: Optional[float] = None, queue_s: Optional[float] = None, run_s: Optional[float] = None,
                               items: Optional[dict[str, Any]] = None, dropped: Optional[list[dict[str, Any]]] = None,
                               brief_sha: Optional[str] = None, delivered: bool = False, delivered_before_claim: Optional[bool] = None,
                               guard_rejections: int = 0, web_wanted: bool = False) -> None:
        if not self.config.research or not task_id:
            return
        self._safe("research_result", self._research_result, task_id, research_task_id, outcome, reason, duration_s, queue_s, run_s, items,
                   dropped, brief_sha, delivered, delivered_before_claim, guard_rejections, web_wanted)

    def _research_result(self, task_id, research_task_id, outcome, reason, duration_s, queue_s, run_s, items, dropped, brief_sha, delivered,
                         delivered_before_claim, guard_rejections, web_wanted) -> None:
        sec = lambda v: round(float(v), 1) if isinstance(v, (int, float)) and not isinstance(v, bool) else None  # noqa: E731
        self._append({
            "kind": "research_result", "task_id": clean(task_id, 80), "research_task_id": clean(research_task_id, 80),
            "outcome": clean(outcome, 20), "reason": clean(reason, 120) or None, "duration_s": sec(duration_s), "queue_s": sec(queue_s), "run_s": sec(run_s),
            "items": {clean(k, 30): _int(v) for k, v in list((items or {}).items())[:8]},
            "dropped": [{"section": clean(d.get("section"), 60), "reason": clean(d.get("reason"), 30)} for d in (dropped or [])[:MAX_FINDINGS] if isinstance(d, dict)],
            "brief_sha": clean(brief_sha, 12) or None, "delivered": bool(delivered),
            "delivered_before_claim": delivered_before_claim if isinstance(delivered_before_claim, bool) else None,
            "guard_rejections": _int(guard_rejections), "web_wanted": bool(web_wanted),
        })

    # -- 3. reviewer outcome
    def record_review_action(self, *, task_id: str, platform: str, action: str, comment: str, final_text: Optional[str], edited: bool) -> None:
        if platform != "blog":
            return
        self._safe("review_action", self._review_action, task_id, action, comment, final_text, edited)

    def _review_action(self, task_id: str, action: str, comment: str, final_text: Optional[str], edited: bool) -> None:
        rec: dict[str, Any] = {"kind": "review_action", "task_id": clean(task_id, 80), "action": clean(action, 12), "comment_chars": len(comment or ""),
                               "text_is_manual_edit": bool(edited)}
        if final_text is not None:
            rec["final_links"] = extract_links(final_text)
        self._append(rec)


# ------------------------------------------------------------------ reading (shared with the report script)
def read_records(path: Path) -> list[dict[str, Any]]:
    """All parseable records, oldest first. A corrupt or foreign line is skipped, never an error."""
    out: list[dict[str, Any]] = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if isinstance(rec, dict) and rec.get("schema") == SCHEMA and isinstance(rec.get("kind"), str):
                    out.append(rec)
    except OSError:
        return []
    return out
