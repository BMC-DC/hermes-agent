#!/usr/bin/env python3
"""Per-submission shadow-mode report (task 08). Read-only, standard library only, no plugin imports (run it with any python3).

    python3 shadow_report.py [--log /path/blog_shadow.jsonl] [--task ROOT_TASK_ID] [--json]

Default log: ``$EVENT_POST_PIPELINE_SHADOW_LOG``, else ``$HERMES_HOME/blog_shadow.jsonl`` (inside the container ``/opt/data``; on the host
``/home/bmc/.hermes/blog_shadow.jsonl``), else ``./blog_shadow.jsonl``. For each submission it prints: the intake summary and the links the
site service suggested, the links each Stylus draft actually used (suggested / not suggested / suggested but unused), lint findings, the
verify-links verdict, what the reviewer finally approved (kept / removed / added versus the last draft) and ``missing_info``. When the research
step was on it also prints, per submission, the research outcome (done / nothing_useful / invalid / timeout / blocked / skipped, durations, item
counts, dropped items, brief sha, whether it was handed to the blog) and the summary splits submissions by whether their blog got a brief
(the simple before/after switch: median blog words, lint hard/warn, links used from the brief, reviewer outcomes). A summary
at the end gives the rates worth watching. Everything printed is control-character-free (submission text is untrusted).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import OrderedDict
from pathlib import Path

_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f  ]")


def clean(v, n=200):
    t = " ".join(_CONTROL.sub(" ", str(v if v is not None else "")).split())
    return t if len(t) <= n else t[: n - 1] + "…"


def norm(url):
    u = re.sub(r"[#?].*$", "", (url or "").strip().rstrip(".,;:!?)"))
    u = re.sub(r"^(https?://)www\.", r"\1", u, flags=re.I)
    m = re.match(r"^(https?://[^/]+)(.*)$", u, flags=re.I)
    return ((m.group(1).lower() + m.group(2)) if m else u).rstrip("/")


def read(path):
    out = []
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if isinstance(r, dict) and r.get("schema") == 1 and isinstance(r.get("kind"), str) and r.get("task_id"):
                    out.append(r)
    except OSError as e:
        sys.exit(f"cannot read {path}: {e.strerror}")
    return out


def group(records):
    g = OrderedDict()
    for r in records:
        g.setdefault(r["task_id"], []).append(r)
    return g


def analyse(recs):
    """Structured per-submission result (also what --json prints)."""
    intake = [r for r in recs if r["kind"] == "intake"]
    comps = [r for r in recs if r["kind"] == "completion"]
    acts = [r for r in recs if r["kind"] == "review_action"]
    it = intake[-1] if intake else None
    rstart = [r for r in recs if r["kind"] == "research"]
    rres = [r for r in recs if r["kind"] == "research_result"]
    suggested = OrderedDict()
    if it and it.get("ok"):
        for c in it.get("links") or []:
            suggested[norm(c.get("url"))] = {"url": c.get("url"), "tier": c.get("tier"), "kind": c.get("kind"), "optional": False}
        for o in it.get("optional_links") or []:
            suggested.setdefault(norm(o.get("url")), {"url": o.get("url"), "tier": "optional", "kind": o.get("label"), "optional": True})
    rounds = []
    for c in comps:
        used = [norm(l.get("url")) for l in c.get("draft_links") or []]
        rounds.append({"round": c.get("round"), "used": used, "used_suggested": [u for u in used if u in suggested], "used_not_suggested": [u for u in used if u not in suggested],
                       "suggested_unused": [u for u in suggested if u not in used] if it and it.get("ok") else None, "lint": c.get("lint"), "findings": c.get("findings") or [],
                       "verify": c.get("verify_links"), "body_words": c.get("body_words"), "brief": c.get("brief")})
    # the reviewer's last approved/edited text vs the draft that was current when it was approved (records are in file = time order)
    approved = draft_then = None
    current = None
    for r in recs:
        if r["kind"] == "completion":
            current = [norm(l.get("url")) for l in r.get("draft_links") or []]
        elif r["kind"] == "review_action" and r.get("action") in ("approved", "edited") and r.get("final_links") is not None:
            approved, draft_then = r, current
    final = [norm(l.get("url")) for l in approved["final_links"]] if approved else None
    outcome = None
    if final is not None and draft_then is not None:
        outcome = {"kept": [u for u in draft_then if u in final], "removed_by_reviewer": [u for u in draft_then if u not in final], "added_by_reviewer": [u for u in final if u not in draft_then],
                   "manual_edit": bool(approved.get("text_is_manual_edit")), "final_suggested": [u for u in final if u in suggested], "final_not_suggested": [u for u in final if u not in suggested]}
    return {"intake": it, "research": {"start": rstart[-1] if rstart else None, "result": rres[-1] if rres else None},
            "suggested": list(suggested.values()), "rounds": rounds, "actions": [{"action": a.get("action"), "ts": a.get("ts")} for a in acts], "approved_links": final, "outcome": outcome}


def research_lines(start, result):
    out = []
    if start:
        out.append(f"  research: task {clean(start.get('research_task_id') or '(none)', 30)} service_ok={start.get('service_ok')} handoff={start.get('handoff')} "
                   f"allowed_urls={start.get('allowed_urls_n')} corpus {clean(start.get('corpus_hash'), 12)} {start.get('latency_ms')} ms")
    if result:
        secs = lambda v: "n/a" if v is None else f"{v:g} s"  # noqa: E731
        it = result.get("items") or {}
        out.append(f"    result: {clean(result.get('outcome'), 20)}" + (f" ({clean(result.get('reason'), 60)})" if result.get("reason") else "")
                   + f"; took {secs(result.get('duration_s'))} (queued {secs(result.get('queue_s'))}, ran {secs(result.get('run_s'))}); "
                   + (", ".join(f"{clean(k, 20)}={v}" for k, v in it.items()) or "no items")
                   + f"; brief {clean(result.get('brief_sha'), 10) or '-'} delivered={result.get('delivered')}"
                   + (f" before_claim={result.get('delivered_before_claim')}" if result.get("delivered_before_claim") is not None else "")
                   + f"; guard_rejections={result.get('guard_rejections')} web_wanted={result.get('web_wanted')}")
        for d in (result.get("dropped") or [])[:6]:
            out.append(f"    dropped {clean(d.get('section'), 50)}: {clean(d.get('reason'), 30)}")
    return out


def show(tid, a, w):
    it = a["intake"]
    w(f"=== task {clean(tid, 60)}" + (f"  submission {clean(it.get('submission_id'), 60)}" if it else "  (no intake record)"))
    if it:
        rq = it.get("request") or {}
        w(f"  intake {clean(it.get('ts'), 20)}: date {clean(rq.get('date'), 20)}, {rq.get('description_words')} words; \"{clean(rq.get('description_excerpt'), 120)}\"")
        if it.get("ok"):
            mi = it.get("missing_info") or {}
            w(f"  service v{clean(it.get('service_version'), 10)} index {clean(it.get('index_built_at'), 20)} stale={it.get('stale_index')} {it.get('latency_ms')} ms; "
              f"program_found={it.get('program_found')} weak_hidden={it.get('weak_hidden')} ready_to_draft={mi.get('ready_to_draft')}")
            for m in mi.get("items") or []:
                w(f"    missing [{'required' if m.get('required') else 'optional'}] {clean(m.get('field'), 30)}: {clean(m.get('why'), 100)}")
        else:
            w(f"  site service NOT available at intake: {clean(it.get('reason'), 80)} ({it.get('latency_ms')} ms)")
    rs = a.get("research") or {}
    if rs.get("start") or rs.get("result"):
        for line in research_lines(rs.get("start"), rs.get("result")):
            w(line)
    if a["suggested"]:
        w("  suggested links:")
        for s in a["suggested"]:
            w(f"    [{clean(s['tier'], 8)}/{clean(s['kind'], 20)}] {clean(s['url'], 100)}")
    for r in a["rounds"]:
        w(f"  draft round {r['round']}: {len(r['used'])} link(s), {r['body_words']} words")
        if r.get("brief"):
            b = r["brief"]
            w(f"    brief: delivered={b.get('delivered')} sha {clean(b.get('sha'), 10) or '-'}; links used from the brief {b.get('links_used')}/{b.get('links_offered')}")
        for u in r["used_suggested"]:
            w(f"    used, suggested:      {clean(u, 100)}")
        for u in r["used_not_suggested"]:
            w(f"    used, NOT suggested:  {clean(u, 100)}")
        for u in r["suggested_unused"] or []:
            w(f"    suggested, unused:    {clean(u, 100)}")
        lt = r["lint"] or {}
        w("  lint: " + (f"ERROR {clean(lt.get('error'), 40)}" if lt.get("error") else f"ok={lt.get('ok')} hard={lt.get('hard')} warn={lt.get('warn')}"))
        for f in r["findings"][:12]:
            w(f"    {clean(f.get('level'), 5).upper():5} {clean(f.get('id'), 6):5} {clean(f.get('message'), 110)}")
        v = r["verify"] or {}
        w("  verify-links: " + ("service NOT available: " + clean(v.get("reason"), 60) + " (links NOT verified)" if not v.get("available") else
                               f"passed={v.get('passed')} errors={v.get('errors')} warnings={v.get('warnings')} placeholders={v.get('placeholders')} closed_list_checked={v.get('closed_list_checked')}"))
    o = a["outcome"]
    if o:
        w(f"  reviewer final ({'manual edit' if o['manual_edit'] else 'Stylus text'}): kept {len(o['kept'])}, removed {len(o['removed_by_reviewer'])}, added {len(o['added_by_reviewer'])}")
        for u in o["removed_by_reviewer"]:
            w(f"    removed by reviewer: {clean(u, 100)}")
        for u in o["added_by_reviewer"]:
            w(f"    added by reviewer:   {clean(u, 100)}")
    elif a["actions"]:
        w("  reviewer actions: " + ", ".join(clean(x["action"], 10) for x in a["actions"]) + " (no approved text recorded yet)")
    w("")


def summary(results):
    n = len(results)
    ok = sum(1 for a in results.values() if a["intake"] and a["intake"].get("ok"))
    sug = used_sug = used = final_total = final_sug = suggested_total = kept_sug = 0
    for a in results.values():
        suggested_total += len(a["suggested"])
        if a["rounds"]:
            r = a["rounds"][-1]
            used += len(r["used"])
            used_sug += len(r["used_suggested"])
        if a["outcome"]:
            final_total += len(a["outcome"]["final_suggested"]) + len(a["outcome"]["final_not_suggested"])
            final_sug += len(a["outcome"]["final_suggested"])
    pct = lambda x, y: f"{100 * x / y:.0f}%" if y else "n/a"
    return base_summary(n, ok, pct, used_sug, used, suggested_total, final_sug, final_total) + brief_summary(results)


def _median(xs):
    xs = sorted(xs)
    if not xs:
        return None
    mid = len(xs) // 2
    return xs[mid] if len(xs) % 2 else (xs[mid - 1] + xs[mid]) / 2


def brief_summary(results):
    """Before/after switch: submissions whose last blog draft had a delivered research brief vs the rest. Printed only when the
    research step left any trace in the log (so a log without research renders exactly as before)."""
    if not any((a.get("research") or {}).get("start") or (a.get("research") or {}).get("result") or any(r.get("brief") for r in a["rounds"])
               for a in results.values()):
        return []
    cohorts = {"with brief": [], "without brief": []}
    for a in results.values():
        last = a["rounds"][-1] if a["rounds"] else None
        if last is None:
            continue
        delivered = bool((last.get("brief") or {}).get("delivered"))
        cohorts["with brief" if delivered else "without brief"].append((a, last))
    out = ["RESEARCH BRIEF (before/after: submissions whose last blog draft had a delivered brief vs not):"]
    outcomes = {}
    for a in results.values():
        res = (a.get("research") or {}).get("result")
        if res:
            outcomes[res.get("outcome")] = outcomes.get(res.get("outcome"), 0) + 1
    out.append("  research outcomes: " + (", ".join(f"{clean(k, 20)}={v}" for k, v in sorted(outcomes.items(), key=lambda kv: str(kv[0]))) or "none"))
    for name, rows in cohorts.items():
        if not rows:
            out.append(f"  {name}: 0 submission(s)")
            continue
        words = [last.get("body_words") for _, last in rows if isinstance(last.get("body_words"), int)]
        hard = sum((last.get("lint") or {}).get("hard") or 0 for _, last in rows)
        warn = sum((last.get("lint") or {}).get("warn") or 0 for _, last in rows)
        used = sum(((last.get("brief") or {}).get("links_used") or 0) for _, last in rows)
        offered = sum(((last.get("brief") or {}).get("links_offered") or 0) for _, last in rows)
        acts = [x.get("action") for a, _ in rows for x in a["actions"]]
        edited = sum(1 for a, _ in rows if a["outcome"] and a["outcome"].get("manual_edit"))
        out.append(f"  {name}: {len(rows)} submission(s); median blog words {_median(words) if words else 'n/a'}; lint hard {hard}, warn {warn}; "
                   f"links used from the brief {used}/{offered}; reviewer: approved {acts.count('approved')}, edited {edited}, refine {acts.count('refine')}, rejected {acts.count('rejected')}")
    return out


def base_summary(n, ok, pct, used_sug, used, suggested_total, final_sug, final_total):
    return [f"SUMMARY: {n} submission(s); site service answered at intake for {ok} ({pct(ok, n)}).",
            f"  last draft links that were suggested: {used_sug}/{used} ({pct(used_sug, used)}); suggestions offered in total: {suggested_total}.",
            f"  reviewer-approved links that were suggested: {final_sug}/{final_total} ({pct(final_sug, final_total)})."]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--log")
    ap.add_argument("--task")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    path = Path(a.log or os.environ.get("EVENT_POST_PIPELINE_SHADOW_LOG") or (Path(os.environ["HERMES_HOME"]) / "blog_shadow.jsonl" if os.environ.get("HERMES_HOME") else "blog_shadow.jsonl"))
    groups = group(read(path))
    if a.task:
        groups = OrderedDict((k, v) for k, v in groups.items() if k == a.task)
    results = OrderedDict((k, analyse(v)) for k, v in groups.items())
    if a.json:
        json.dump(results, sys.stdout, ensure_ascii=False, indent=1)
        print()
        return 0
    if not results:
        print(f"no shadow records in {path}")
        return 0
    for tid, an in results.items():
        show(tid, an, print)
    print("\n".join(summary(results)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
