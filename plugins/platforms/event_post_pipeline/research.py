"""Research step (DESIGN_11 sections 2.2-2.6 and 7): an optional WebMaster task R that prepares a short, code-checked
background brief for the blog task B.

This module is the plugin side of that step. At intake (``ResearchPlanner.start``) the plugin itself calls the host
site service (fail-open, hard 2.5 s budget), keeps the URLs it received (``allowed_urls``), and builds R's body: the
fenced submission (no submitter name/phone) plus a fenced ``<site_candidates>`` block. WebMaster does judgment only.

Flags (all default OFF, literal ``true`` required; ``platforms.event_post_pipeline.extra.blog_enrichment``):
``split`` (pipeline.py), ``research.enabled`` (create R), ``research.handoff`` (link B under R and deliver the brief;
ignored, with a warning, unless ``split`` is on).

Nothing here raises into the intake path: planning problems become ``ResearchPlan(ok=False, reason=...)``.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from plugins.platforms.event_post_pipeline.fence import defuse_fence_tags
from plugins.platforms.event_post_pipeline.intake import parse_intake, to_service_pack
from plugins.platforms.event_post_pipeline.site_client import SiteClient, SiteClientConfig

logger = logging.getLogger("plugins.platforms.event_post_pipeline")

RESEARCH_SUFFIX = "-research"
RESEARCH_MARKER = "[task-kind: research]"
BLOG_RESEARCH_LINE = (
    'Research: requested; a "Research brief" comment follows if one could be prepared. '
    "Proceed without it if there is none."
)
SERVICE_BUDGET_S = 2.5
BODY_BUDGET = 7500           # characters; the worker context reads at most 8 KB of a body
MIN_CANDIDATE_ROOM = 400     # below this much room for site material the research is skipped
MAX_PASSAGES = 4
PASSAGE_CHARS = 400
PROGRAM_CHARS = 700
MAX_PACK_FIELD_CHARS = 8000
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f  ]")
_URL_OK = re.compile(r"^https?://[^\s<>\"']{4,196}$", re.I)


def _line(value: Any, limit: int) -> str:
    """One printable line, truncated, fence tags defused (site text is data for an LLM)."""
    text = " ".join(_CONTROL.sub(" ", str(value if value is not None else "")).split())
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return str(defuse_fence_tags(text))


# --------------------------------------------------------------------------------------------- config
def _cfg_dict(extra: Optional[dict[str, Any]]) -> dict[str, Any]:
    be = (extra or {}).get("blog_enrichment")
    rc = be.get("research") if isinstance(be, dict) else None
    return rc if isinstance(rc, dict) else {}


def _num(value: Any, default: float, lo: float, hi: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not lo <= value <= hi:
        return default
    return float(value)


@dataclass(frozen=True)
class ResearchConfig:
    enabled: bool = False
    handoff: bool = False
    assignee: str = "webmaster"
    skill: str = "event-research"
    max_runtime_s: int = 240
    deadline_s: int = 360
    reasoning_effort: str = "medium"
    max_candidates: int = 8
    fact_patterns: tuple[str, ...] = ()
    budget_s: float = SERVICE_BUDGET_S
    service_url: str = ""

    @classmethod
    def from_extra(cls, extra: Optional[dict[str, Any]]) -> "ResearchConfig":
        """Anything missing, mistyped or not the literal ``True`` (for the two flags) is the safe default (off)."""
        rc = _cfg_dict(extra)
        be = (extra or {}).get("blog_enrichment")
        url = be.get("service_url") if isinstance(be, dict) else None
        if not isinstance(url, str) or not url:
            ss = (extra or {}).get("site_service")
            url = ss.get("url") if isinstance(ss, dict) else ""
        pats = rc.get("fact_patterns")
        text = lambda key, default: rc[key].strip() if isinstance(rc.get(key), str) and rc[key].strip() else default  # noqa: E731
        return cls(
            enabled=rc.get("enabled") is True, handoff=rc.get("handoff") is True,
            assignee=text("assignee", "webmaster"), skill=text("skill", "event-research"),
            max_runtime_s=int(_num(rc.get("max_runtime_s"), 240, 30, 3600)),
            deadline_s=int(_num(rc.get("deadline_s"), 360, 60, 7200)),
            reasoning_effort=text("reasoning_effort", "medium"),
            max_candidates=int(_num(rc.get("max_candidates"), 8, 1, 20)),
            fact_patterns=tuple(p for p in pats if isinstance(p, str) and p.strip()) if isinstance(pats, (list, tuple)) else (),
            budget_s=SERVICE_BUDGET_S, service_url=url if isinstance(url, str) else "",
        )

    def effective_handoff(self, split: bool) -> bool:
        """``handoff`` only counts together with ``split`` (a separate blog task is needed to gate on R)."""
        if self.handoff and not self.enabled:
            return False
        if self.handoff and not split:
            logger.warning("event_post_pipeline: research.handoff is on but blog_enrichment.split is off; handoff ignored")
            return False
        return self.handoff


def research_enabled(extra: Optional[dict[str, Any]]) -> bool:
    return _cfg_dict(extra).get("enabled") is True


def is_research_key(idempotency_key: Optional[str]) -> bool:
    return bool(idempotency_key) and idempotency_key.endswith(RESEARCH_SUFFIX) and len(idempotency_key) > len(RESEARCH_SUFFIX)


def submission_id_for_key(idempotency_key: str) -> str:
    return idempotency_key[: -len(RESEARCH_SUFFIX)]


def is_research_task(task: Any, tenant: str) -> bool:
    return bool(task is not None and getattr(task, "tenant", None) == tenant and is_research_key(getattr(task, "idempotency_key", None)))


# --------------------------------------------------------------------------------------------- plan
@dataclass
class ResearchPlan:
    ok: bool
    reason: str = ""                  # when not ok: "service down: <why>", "nothing useful", "submission too large", ...
    title: str = ""
    body: str = ""
    allowed_urls: list[str] = field(default_factory=list)
    corpus_hash: str = ""
    n_candidates: int = 0
    service_ok: bool = False
    latency_ms: int = 0


def _candidate_line(c: dict[str, Any]) -> Optional[tuple[str, str]]:
    url = c.get("url")
    if not isinstance(url, str) or not _URL_OK.match(url.strip()):
        return None
    url = url.strip()
    kind = "/".join(x for x in (_line(c.get("kind"), 20), _line(c.get("tier"), 12)) if x)
    why = _line(c.get("tier_reason") or c.get("why"), 160)
    parts = [url, _line(c.get("title"), 100), kind, why]
    return url, "- " + " | ".join(p for p in parts if p)


def build_research_body(pack_block: str, data: dict[str, Any], max_candidates: int) -> tuple[str, list[str], int, str]:
    """``(body, allowed_urls, n_candidates, reason)``; ``reason`` is non-empty (and body empty) when there is nothing to send.
    ``pack_block`` is the already fenced submission (``pipeline._wrap_untrusted``): it never carries submitter name or phone."""
    head = f"{RESEARCH_MARKER}\n{pack_block}\n<site_candidates>"
    tail = "</site_candidates>"
    room = BODY_BUDGET - len(head) - len(tail) - 2
    if room < MIN_CANDIDATE_ROOM:
        return "", [], 0, "submission too large for the research body"
    lines: list[str] = []
    allowed: list[str] = []
    used = 0

    def add(text: str, *urls: str) -> bool:
        nonlocal used
        if used + len(text) + 1 > room:
            return False
        lines.append(text)
        used += len(text) + 1
        for u in urls:
            if u and u not in allowed:
                allowed.append(u)
        return True

    add("Pages from the Center's website (the ONLY urls you may cite):")
    n = 0
    for c in (data.get("candidates") or []):
        if n >= max_candidates:
            break
        if isinstance(c, dict) and (row := _candidate_line(c)):
            if add(row[1], row[0]):
                n += 1
    for o in (data.get("optional_links") or [])[:4]:
        if isinstance(o, dict) and (row := _candidate_line({**o, "kind": "optional", "tier": ""})):
            add(row[1], row[0])
    prog = data.get("program_excerpt")
    has_program = False
    if isinstance(prog, dict) and isinstance(prog.get("url"), str) and _URL_OK.match(prog["url"].strip()) and prog.get("text"):
        has_program = add(f"Program page ({prog['url'].strip()}): standing description excerpt: {_line(prog.get('text'), PROGRAM_CHARS)}", prog["url"].strip())
    npass = 0
    for p in (data.get("passages") or []):
        if npass >= MAX_PASSAGES:
            break
        if isinstance(p, dict) and isinstance(p.get("url"), str) and _URL_OK.match(p["url"].strip()) and p.get("text"):
            if add(f"Passage [{p['url'].strip()}] {_line(p.get('heading'), 80)}: {_line(p.get('text'), PASSAGE_CHARS)}", p["url"].strip()):
                npass += 1
    mi = data.get("missing_info")
    if isinstance(mi, dict) and isinstance(mi.get("items"), list):
        miss = [f"{_line(i.get('field'), 40)}: {_line(i.get('why'), 100)}" for i in mi["items"][:8] if isinstance(i, dict)]
        if miss:
            add("The submission lacks: " + "; ".join(miss))
    if n == 0 and not has_program and npass == 0:
        return "", [], 0, "nothing useful"
    body = "\n".join([head, *lines, tail])
    return body, allowed, n, ""


class ResearchPlanner:
    """Plans the research task at intake. ``start`` never raises and returns within about ``budget_s`` seconds
    (the site call runs on a daemon thread that is abandoned at the deadline)."""

    def __init__(self, config: ResearchConfig, client_factory: Optional[Callable[[float], Any]] = None):
        self.config = config
        self._client_factory = client_factory

    def _client(self, timeout_s: float):
        if self._client_factory is not None:
            return self._client_factory(timeout_s)
        kwargs: dict[str, Any] = {"enabled": True, "timeout_s": max(0.2, timeout_s)}
        if self.config.service_url:
            kwargs["base_url"] = self.config.service_url.rstrip("/")
        return SiteClient(SiteClientConfig(**kwargs))

    def start(self, *, submission_id: str, event_pack: dict[str, Any], pack_block: str, title: str) -> ResearchPlan:
        t0 = time.monotonic()
        budget = self.config.budget_s
        holder: dict[str, Any] = {}

        def work() -> None:
            try:
                client = self._client(budget)
                health = client.health()
                if not health.available:
                    holder["res"] = ("down", health.reason or "health failed", None)
                    return
                remaining = budget - (time.monotonic() - t0)
                if remaining <= 0.1:
                    holder["res"] = ("down", "timeout", None)
                    return
                # the context-pack call gets only what is left of the budget
                client = self._client(remaining) if self._client_factory is None else client
                pack = self._service_pack(event_pack)
                res = client.context_pack(pack)
                holder["res"] = ("ok", "", res) if res.available else ("down", res.reason or "context-pack failed", None)
            except Exception as exc:  # noqa: BLE001
                holder["res"] = ("down", type(exc).__name__, None)

        thread = threading.Thread(target=work, name="bmc-research-plan", daemon=True)
        thread.start()
        thread.join(budget)
        latency = int((time.monotonic() - t0) * 1000)
        res = holder.get("res")
        if res is None:
            return ResearchPlan(ok=False, reason="service down: timeout", latency_ms=latency)
        kind, why, result = res
        if kind != "ok":
            return ResearchPlan(ok=False, reason=f"service down: {_line(why, 60)}", latency_ms=latency)
        try:
            body, allowed, n, reason = build_research_body(pack_block, result.data or {}, self.config.max_candidates)
        except Exception as exc:  # noqa: BLE001
            logger.warning("event_post_pipeline: research body build failed (%s)", type(exc).__name__)
            return ResearchPlan(ok=False, reason="body build failed", service_ok=True, latency_ms=latency)
        if reason:
            return ResearchPlan(ok=False, reason=reason, service_ok=True, corpus_hash=result.corpus_hash, latency_ms=latency)
        return ResearchPlan(
            ok=True, title=f"Research: {title}", body=body, allowed_urls=allowed, corpus_hash=str(result.corpus_hash or ""),
            n_candidates=n, service_ok=True, latency_ms=latency,
        )

    @staticmethod
    def _service_pack(event_pack: dict[str, Any]) -> dict[str, Any]:
        """Only the service's own pack keys (date/description/quote + intake fields). Submitter name/phone never leave the plugin."""
        raw = to_service_pack(event_pack, parse_intake(event_pack))
        return {k: ([str(x)[:MAX_PACK_FIELD_CHARS] for x in v] if isinstance(v, list) else str(v)[:MAX_PACK_FIELD_CHARS]) for k, v in raw.items()}


def planner_from_extra(extra: Optional[dict[str, Any]]) -> Optional[ResearchPlanner]:
    """``None`` unless ``research.enabled`` is literally true (flags off: no planner, no behaviour change)."""
    cfg = ResearchConfig.from_extra(extra)
    return ResearchPlanner(cfg) if cfg.enabled else None
