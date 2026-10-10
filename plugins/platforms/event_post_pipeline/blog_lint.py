"""Deterministic linter for Stylus blog drafts (work item W04).

Pure functions over ``(draft, pack)`` — no LLM call, no network, no database, standard
library plus PyYAML only (the ``hermes`` container has ``yaml`` but no numpy). The same
input and rules always give the same findings, in the same order.

    from plugins.platforms.event_post_pipeline.blog_lint import lint_blog
    report = lint_blog(draft, pack)          # rules=None -> lint_rules.yaml next to this file
    report["ok"]                             # False when any *hard* finding exists
    report["findings"]                       # [{"id", "level", "message", "field"?, "span"?}, ...]

``draft`` is what Stylus put in ``metadata["blog"]``: an object (``{"body", "title"?,
"focus_keyword"?, ...}``) **or** a plain string (the legacy shape; trailing ``focus_keyword:`` /
``seo_title:`` ... lines are read as the SEO fields). A full completion ``{"fb", "ig", "blog"}`` is
also accepted (its ``blog`` is used). A draft with no usable blog text yields one hard finding
``C1`` and nothing else. ``pack`` is the submission: a dict (``date``, ``description``, ``quote``
plus optional extras such as ``speaker``) or the labelled ``Date:/Description:/Quote:`` text.

Each finding: ``id`` (RUBRIC item, e.g. ``"R7"``; sub-checks ``"R8a"``, ``"R11b"``; ``"C1"``/``"C2"``
are contract/size notices), ``level`` (``"hard"`` | ``"warn"``), ``message`` (plain language, quotes at
most a short excerpt of the draft), optionally ``field`` (``"body"`` | ``"title"`` | an SEO field name)
and ``span`` (``[start, end]`` character offsets into ``report["body"]`` or ``report["title"]``).
The report also carries ``rules`` (per item: ``pass`` / ``fail`` / ``n/a``), ``metrics`` and
``skipped`` (rules that could not run, with the reason).

Rule levels
-----------
*hard*  = the output is broken or unsafe to publish as is:
  ``C1`` no blog text; ``R7`` placeholders / unresolved markup; ``R8a`` malformed or dangerous links.
*warn*  = a stylistic heuristic whose detector is approximate: R1, R2, R3, R4, R6, R8b/c, R10, R11,
  R12 (spelling variants; owner decision D4: names and spelling are not a hard weight), R13, R14, R15, R16. They are for a reviewer/critic to weigh, never to block on.
The reason for each default is written next to the rule in ``lint_rules.yaml``.

Changing the rules
------------------
Everything tunable — phrase lists, thresholds, spelling variants, levels, on/off — is in
``lint_rules.yaml`` (same directory; shipped with the plugin because the Docker image copies the whole
source tree and installs it editable). Edit the file, run the ``tests/plugins/test_event_post_pipeline_blog_lint*.py``
tests, bump ``version``. A malformed rules file raises :class:`LintRulesError` instead of silently
linting less. To lint with other rules in code pass ``rules=`` (a loaded dict or a path).

Not implemented here (needs the site service or an LLM; see RUBRIC.md): R5 claims beyond the pack,
R8's "on the closed list / resolves / anchor matches the target", R9, and the parts of R11 that need
judgement (R11b is a pattern approximation, see its comment in the YAML).

Command line (a human can lint a draft file during shadow mode)::

    python plugins/platforms/event_post_pipeline/blog_lint.py DRAFT [--pack PACK] [--rules RULES] [--json]
    python -m plugins.platforms.event_post_pipeline.blog_lint DRAFT ...     # needs the package's deps

``DRAFT`` / ``PACK`` are files (``-`` = stdin for the draft): JSON, or plain text. Exit status: 0 ok,
1 a hard finding, 2 usage error.

Known approximations carried over from ``style-study/scripts/rubric_score.py`` (calibrated on 9 drafts):
R3 counts "names" as capitalised words after a lowercase word or an honorific, minus a stop-list, plus digit
groups and number words — sentence-initial names are missed and unlisted capitalised words are counted;
R4 matches wording, not meaning; R14 treats short lines without closing punctuation as headings. See the
comments in ``lint_rules.yaml`` for each rule's limits.
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Any, Optional, Union

import yaml

RULES_PATH = Path(__file__).with_name("lint_rules.yaml")
LEVELS = ("hard", "warn")
SEO_KEYS = ("focus_keyword", "supporting_keywords", "seo_title", "meta_description", "url_slug")

# Bounded quantifiers everywhere a user-controlled string is scanned: an unbounded ``[^\]]*`` makes
# ``"[" * 100000`` quadratic.
WORD = re.compile(r"[A-Za-zÀ-ɏḀ-ỿ0-9’'\-]+")
_LINK = re.compile(r"(!?)\[([^\]\n]{0,200})\]\(([^)\n]{0,600})\)")
_SENT_SPLIT = re.compile(r"(?<=[.!?”])\s+|\n")
_META_LINE = re.compile(r"(?m)^(%s):[ \t]*(.*)$" % "|".join(SEO_KEYS))
_TOKEN = re.compile(r"[^\W_]+(?:['’][^\W_]+)*", re.UNICODE)


class LintRulesError(ValueError):
    """``lint_rules.yaml`` (or a ``rules=`` argument) is missing, unreadable or invalid."""


# ---------------------------------------------------------------------------------------------
# rules loading / validation
# ---------------------------------------------------------------------------------------------

_RULE_IDS = ("R1", "R2", "R3", "R4", "R6", "R7", "R8", "R10", "R11", "R12", "R13", "R14", "R15", "R16")
_LEVEL_KEYS = {
    "R8": ("level_malformed", "level_generic_anchor", "level_duplicate"),
    "R11": ("level_verbatim", "level_speaker"),
    }
_REGEX_LISTS = {
    "R1": ("patterns",), "R4": ("phrases",), "R6": ("stock_endings",), "R7": ("patterns",),
}
_cache: dict[str, Any] = {"key": None, "rules": None}


def _split_words(value: Any, where: str) -> list[str]:
    if isinstance(value, str):
        return value.split()
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return list(value)
    raise LintRulesError(f"{where}: expected a list of strings (or a whitespace-separated string)")


def _check_regex(pattern: Any, where: str) -> None:
    if not isinstance(pattern, str) or not pattern:
        raise LintRulesError(f"{where}: expected a non-empty regex string, got {pattern!r}")
    try:
        re.compile(pattern)
    except re.error as exc:
        raise LintRulesError(f"{where}: bad regex {pattern!r}: {exc}") from exc


def _validate(rules: Any, source: str) -> dict[str, Any]:
    if not isinstance(rules, dict):
        raise LintRulesError(f"{source}: top level must be a mapping")
    for key in ("version", "limits", "specifics", "rules"):
        if key not in rules:
            raise LintRulesError(f"{source}: missing top-level key {key!r}")
    body = rules["rules"]
    if not isinstance(body, dict):
        raise LintRulesError(f"{source}: 'rules' must be a mapping")
    unknown = sorted(set(body) - set(_RULE_IDS))
    if unknown:
        raise LintRulesError(f"{source}: unknown rule id(s) {unknown}; known: {list(_RULE_IDS)}")
    for rid in _RULE_IDS:
        cfg = body.get(rid)
        if cfg is None:
            raise LintRulesError(f"{source}: rule {rid} is missing (use enabled: false to switch it off)")
        if not isinstance(cfg, dict):
            raise LintRulesError(f"{source}: rule {rid} must be a mapping")
        for lk in _LEVEL_KEYS.get(rid, ("level",)):
            if cfg.get(lk) not in LEVELS:
                raise LintRulesError(f"{source}: rule {rid}.{lk} must be one of {LEVELS}, got {cfg.get(lk)!r}")
        for lk in _REGEX_LISTS.get(rid, ()):
            items = cfg.get(lk)
            if not isinstance(items, list):
                raise LintRulesError(f"{source}: rule {rid}.{lk} must be a list")
            for item in items:
                _check_regex(item, f"{source}: rule {rid}.{lk}")
    for key in ("tail_link_regex",):
        _check_regex(body["R6"].get(key), f"{source}: rule R6.{key}")
    _check_regex(body["R15"].get("pattern"), f"{source}: rule R15.pattern")
    entries = body["R12"].get("variants")
    if not isinstance(entries, list):
        raise LintRulesError(f"{source}: rule R12.variants must be a list")
    for entry in entries:
        if not isinstance(entry, dict) or "pattern" not in entry or "use" not in entry:
            raise LintRulesError(f"{source}: rule R12.variants entries need 'pattern' and 'use'")
        _check_regex(entry["pattern"], f"{source}: rule R12.variants")
    spec = rules["specifics"]
    for key in ("not_names", "honorifics", "number_words", "scene_words"):
        _split_words(spec.get(key), f"{source}: specifics.{key}")
    for rid in ("R10", "R13"):
        if not isinstance(body[rid].get("pack_fields"), list):
            raise LintRulesError(f"{source}: rule {rid}.pack_fields must be a list")
    for key in ("name_honorifics", "speech_verbs", "speaker_fields"):
        if not isinstance(body["R11"].get(key), list):
            raise LintRulesError(f"{source}: rule R11.{key} must be a list")
    for key in ("generic_anchors", "dangerous_schemes"):
        if not isinstance(body["R8"].get(key), list):
            raise LintRulesError(f"{source}: rule R8.{key} must be a list")
    _split_words(body["R16"].get("stopwords"), f"{source}: rule R16.stopwords")
    for rid, cfg in body.items():
        for key, value in cfg.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value < 0:
                raise LintRulesError(f"{source}: rule {rid}.{key} must not be negative")
    return rules


def load_rules(path: Union[str, Path, None] = None) -> dict[str, Any]:
    """Load and validate a rules file (default: ``lint_rules.yaml`` next to this module)."""
    p = Path(path) if path else RULES_PATH
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    except OSError as exc:
        raise LintRulesError(f"cannot read rules file {p}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise LintRulesError(f"rules file {p} is not valid YAML: {exc}") from exc
    return _validate(raw, str(p))


def _default_rules() -> dict[str, Any]:
    try:
        stamp = RULES_PATH.stat().st_mtime_ns
    except OSError as exc:
        raise LintRulesError(f"cannot read rules file {RULES_PATH}: {exc}") from exc
    if _cache["key"] != stamp:
        _cache["rules"] = load_rules(RULES_PATH)
        _cache["key"] = stamp
    return _cache["rules"]


def _resolve_rules(rules: Any) -> dict[str, Any]:
    if rules is None:
        return _default_rules()
    if isinstance(rules, (str, Path)):
        return load_rules(rules)
    return _validate(rules, "<rules argument>")


# ---------------------------------------------------------------------------------------------
# normalising the inputs
# ---------------------------------------------------------------------------------------------

def parse_pack_text(text: str) -> dict[str, str]:
    """``Date: …\\nDescription: …\\nQuote: …`` (the pipeline's own rendering) -> dict. Text with no
    such labels is taken as the description."""
    labels = list(re.finditer(r"(?m)^(Date|Description|Quote|First photo|Last photo|Rest):[ \t]?", text))
    if not labels:
        return {"description": text}
    out: dict[str, str] = {}
    for i, m in enumerate(labels):
        end = labels[i + 1].start() if i + 1 < len(labels) else len(text)
        out[m.group(1).lower().replace(" ", "_")] = text[m.end():end].strip()
    return out


def _pack_dict(pack: Any) -> dict[str, Any]:
    if pack is None:
        return {}
    if isinstance(pack, str):
        return parse_pack_text(pack)
    if isinstance(pack, dict):
        return {str(k).lower(): v for k, v in pack.items()}
    return {}


def _pack_text(pack: dict[str, Any], fields: list[str]) -> str:
    return "\n\n".join(v for f in fields if isinstance(v := pack.get(f), str) and v.strip())


def normalize_draft(draft: Any) -> dict[str, Any]:
    """Return ``{shape, title, title_in_body, body, seo}`` for any accepted draft shape.
    ``shape`` is ``"object"``, ``"string"`` or ``"missing"``."""
    if isinstance(draft, dict) and "body" not in draft and isinstance(draft.get("blog"), (dict, str)):
        draft = draft["blog"]
    if isinstance(draft, dict) and isinstance(draft.get("body"), str):
        shape, body, obj = "object", draft["body"], draft
    elif isinstance(draft, str):
        shape, body, obj = "string", draft, {}
    else:
        return {"shape": "missing", "title": "", "title_in_body": False, "body": "", "seo": {}}
    seo: dict[str, Any] = {k: obj[k] for k in SEO_KEYS if k in obj and obj[k] not in (None, "")}
    for m in _META_LINE.finditer(body):
        seo.setdefault(m.group(1), m.group(2).strip())
    body = _META_LINE.sub("", body).strip()
    title = obj.get("title") if isinstance(obj.get("title"), str) and obj.get("title").strip() else ""
    in_body = not title
    if in_body:
        title = body.split("\n", 1)[0].strip()
    return {"shape": shape, "title": title, "title_in_body": in_body, "body": body, "seo": seo}


# ---------------------------------------------------------------------------------------------
# text helpers (ported from style-study/scripts/rubric_score.py)
# ---------------------------------------------------------------------------------------------

def _plain(md: str) -> str:
    md = re.sub(r"!\[[^\]\n]{0,200}\]\([^)\n]{0,600}\)", " ", md)
    md = re.sub(r"\[([^\]\n]{0,200})\]\([^)\n]{0,600}\)", r"\1", md)
    return re.sub(r"[*_#>`\\]", " ", md)


def _prose_only(body: str) -> list[str]:
    """Drop headings, image/button/caption/video lines and short heading-like lines; keep the
    paragraphs people read as prose."""
    keep: list[str] = []
    for blk in re.split(r"\n\s*\n", body):
        b = blk.strip()
        if not b or b.startswith(("#", "!", "Video:", "[!")):
            continue
        rest = re.sub(r"\[[^\]\n]{0,200}\]\([^)\n]{0,600}\)", "", b).strip(" *_\\|→")
        if "](" in b and len(WORD.findall(rest)) <= 1:
            continue  # button-only line
        if len(WORD.findall(_plain(b))) <= 8 and not re.search(r"[.!?”\"]\s*\**$", b):
            continue  # heading-like short line in plain-text drafts
        keep.append(b)
    return keep


class _Ctx:
    """Everything the rules share, computed once per lint."""

    def __init__(self, rules: dict[str, Any], d: dict[str, Any], pack: dict[str, Any]):
        self.rules = rules
        self.cfg = rules["rules"]
        spec = rules["specifics"]
        self.not_names = set(_split_words(spec["not_names"], "not_names"))
        self.honorifics = tuple(_split_words(spec["honorifics"], "honorifics"))
        self.num_re = re.compile(r"\b(%s)\b" % "|".join(re.escape(w) for w in _split_words(spec["number_words"], "number_words")), re.I)
        self.scene_re = re.compile(r"\b(?:%s)" % "|".join(re.escape(w) for w in _split_words(spec["scene_words"], "scene_words")), re.I)
        self.d, self.pack = d, pack
        self.title, self.body = d["title"], d["body"]
        self.paras = _prose_only(self.body)
        self.prose = _plain("\n\n".join(self.paras))
        self.words = WORD.findall(self.prose)
        self.nw = len(self.words)
        self._spec_cache: dict[tuple, tuple[set, list]] = {}
        # Stylus usually repeats the headline as the first line of ``body`` even when it also sends a
        # ``title`` key; a separately-scanned title is only extra text when it is NOT already in the body.
        self.extra_title = self.title if self.title and self.title not in self.body else ""

    def specifics(self, paras: list[str]) -> tuple[set[str], list[str]]:
        key = tuple(paras)
        if key in self._spec_cache:
            return self._spec_cache[key]
        t = _plain(" ".join(paras))
        names: set[str] = set()
        for sent in _SENT_SPLIT.split(t):
            toks = WORD.findall(sent)
            for i, w in enumerate(toks):
                if i > 0 and w[:1].isupper() and w not in self.not_names and len(w) > 2 and \
                        (toks[i - 1][:1].islower() or toks[i - 1] in self.honorifics):
                    names.add(w)
        nums = re.findall(r"\b\d[\d,.:]*\b", t) + self.num_re.findall(t)
        self._spec_cache[key] = (names, nums)
        return names, nums


def _f(rid: str, level: str, message: str, *, field: Optional[str] = None, span: Optional[tuple[int, int]] = None) -> dict[str, Any]:
    out: dict[str, Any] = {"id": rid, "level": level, "message": message}
    if field:
        out["field"] = field
    if span:
        out["span"] = [span[0], span[1]]
    return out


def _quoted_spans(text: str) -> list[tuple[int, int]]:
    """Spans of “curly” and "straight" double-quoted passages (single line each). Linear time:
    a regex with a bounded class rescans up to its bound from every unmatched opener."""
    spans: list[tuple[int, int]] = []
    i = 0
    while True:
        a = text.find("“", i)
        if a < 0:
            break
        b = text.find("”", a + 1)
        if b < 0:
            break
        if "\n" not in text[a:b]:
            spans.append((a, b + 1))
        i = b + 1
    marks = [m.start() for m in re.finditer('"', text)]
    for a, b in zip(marks[::2], marks[1::2]):
        if "\n" not in text[a:b]:
            spans.append((a, b + 1))
    return spans


def _excerpt(text: str, n: int = 60) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1] + "…"


# ---------------------------------------------------------------------------------------------
# the rules. Each returns (status, findings, metrics): status is "pass" | "fail" | "n/a".
# ---------------------------------------------------------------------------------------------

def _r1(c: _Ctx):
    cfg = c.cfg["R1"]
    for rx in cfg["patterns"]:
        m = re.search(rx, c.title)
        if m:
            span = (m.start(), m.end()) if c.d["title_in_body"] or c.title else None
            return "fail", [_f("R1", cfg["level"], f"headline reads like a keyword/SEO string (matches {rx!r}): {_excerpt(c.title, 80)}",
                               field="title", span=span)], {}
    return "pass", [], {}


def _r2(c: _Ctx):
    cfg = c.cfg["R2"]
    first = c.paras[: cfg["paragraphs"]]
    if not first:
        return "fail", [_f("R2", cfg["level"], "no prose paragraph to judge the opening")], {}
    names, nums = c.specifics(first)
    if names or nums or c.scene_re.search(" ".join(first)):
        return "pass", [], {}
    return "fail", [_f("R2", cfg["level"], f"the first {cfg['paragraphs']} paragraphs have no concrete specific (a name, number, date or scene detail)")], {}


def _r3(c: _Ctx):
    cfg = c.cfg["R3"]
    names, nums = c.specifics(c.paras)
    spec = round(100 * (len(names) + len(nums)) / max(1, c.nw), 1)
    metrics = {"specificity_per_100w": spec, "specific_names": sorted(names), "specific_numbers": len(nums)}
    if c.nw < cfg["min_words"]:
        return "n/a", [], metrics
    if spec >= cfg["min_per_100"]:
        return "pass", [], metrics
    return "fail", [_f("R3", cfg["level"], f"specificity {spec} per 100 words is below {cfg['min_per_100']} (names and numbers; the detector is approximate)")], metrics


def _span_in_body(c: _Ctx, rx: str) -> Optional[tuple[int, int]]:
    m = re.search(rx, c.body)
    return (m.start(), m.end()) if m else None


def _r4(c: _Ctx):
    cfg = c.cfg["R4"]
    hits = [rx for rx in cfg["phrases"] if re.search(rx, c.prose)]
    metrics = {"filler_hits": hits}
    if len(hits) <= cfg["max_allowed"]:
        return "pass", [], metrics
    findings = []
    for rx in hits:
        m = re.search(rx, c.prose)
        findings.append(_f("R4", cfg["level"], f"generic bridge sentence ({len(hits)} found, at most {cfg['max_allowed']} allowed): “{_excerpt(m.group(0))}…”",
                           field="body", span=_span_in_body(c, rx)))
    return "fail", findings, metrics


def _r6(c: _Ctx):
    cfg = c.cfg["R6"]
    findings = []
    for rx in cfg["stock_endings"]:
        m = re.search(rx, c.body)
        if m:
            findings.append(_f("R6", cfg["level"], f"stock/template closing or placeholder: “{_excerpt(m.group(0))}”", field="body", span=(m.start(), m.end())))
    tail = c.paras[-cfg["tail_paragraphs"]:]
    names, nums = c.specifics(tail)
    tail_lines = "\n".join(c.body.strip().split("\n")[-cfg["tail_lines"]:])
    if not (names or nums or re.search(cfg["tail_link_regex"], tail_lines)):
        findings.append(_f("R6", cfg["level"], "the ending has no event-specific element (a name, number, date, place or link)"))
    return ("fail" if findings else "pass"), findings, {}


def _r7(c: _Ctx):
    cfg = c.cfg["R7"]
    findings = []
    fields = [("body", c.body)] + ([("title", c.extra_title)] if c.extra_title else []) + \
             [(k, v) for k, v in c.d["seo"].items() if k != "supporting_keywords" and isinstance(v, str)] + \
             [("supporting_keywords", "; ".join(map(str, v)) if isinstance(v, list) else str(v)) for k, v in c.d["seo"].items() if k == "supporting_keywords"]
    for name, text in fields:
        for rx in cfg["patterns"]:
            for m in list(re.finditer(rx, text))[:5]:
                span = (m.start(), m.end()) if name in ("body", "title") else None
                ctx = text[max(0, m.start() - 10): m.end() + 40]
                findings.append(_f("R7", cfg["level"], f"placeholder or unresolved markup in {name}: “{_excerpt(ctx)}”", field=name, span=span))
    return ("fail" if findings else "pass"), findings[:20], {}


def _r8(c: _Ctx):
    cfg = c.cfg["R8"]
    findings: list[dict[str, Any]] = []
    seen: Counter = Counter()
    first_span: dict[str, tuple[int, int]] = {}
    generic = {a.casefold() for a in cfg["generic_anchors"]}
    schemes = tuple(s.casefold() for s in cfg["dangerous_schemes"])
    for m in _LINK.finditer(c.body):
        is_img, anchor, target = m.group(1) == "!", m.group(2).strip(), m.group(3).strip()
        href = target.split(None, 1)[0] if target else ""
        span = (m.start(), m.end())
        if not href:
            findings.append(_f("R8a", cfg["level_malformed"], "link with an empty target", field="body", span=span))
            continue
        if href.casefold().startswith(schemes):
            findings.append(_f("R8a", cfg["level_malformed"], f"link target uses a disallowed scheme: {_excerpt(href, 40)}", field="body", span=span))
            continue
        if is_img:
            continue
        if not anchor:
            findings.append(_f("R8a", cfg["level_malformed"], "link with empty anchor text", field="body", span=span))
            continue
        if re.sub(r"[^\w ]+", "", anchor.casefold()).strip() in generic:
            findings.append(_f("R8b", cfg["level_generic_anchor"], f"generic link anchor “{_excerpt(anchor, 40)}” (name what the link is)", field="body", span=span))
        key = re.sub(r"#.*$", "", href).rstrip("/").casefold()
        seen[key] += 1
        first_span.setdefault(key, span)
    leftover = _LINK.sub(lambda m: " " * len(m.group(0)), c.body)
    for m in list(re.finditer(r"\]\(", leftover))[:5]:
        findings.append(_f("R8a", cfg["level_malformed"], "unbalanced link markup: “](” without a complete [text](target)", field="body", span=(m.start(), m.end())))
    for key, n in sorted(seen.items()):
        if n > cfg["max_per_target"]:
            findings.append(_f("R8c", cfg["level_duplicate"], f"the same target is linked {n} times (at most {cfg['max_per_target']}): {_excerpt(key, 60)}", field="body", span=first_span[key]))
    return ("fail" if findings else "pass"), findings, {"links": sum(seen.values())}


def _r10(c: _Ctx):
    cfg = c.cfg["R10"]
    text = _pack_text(c.pack, cfg["pack_fields"])
    st = [w.lower() for w in WORD.findall(text)]
    if len(st) < cfg["min_pack_words"]:
        return "n/a", [], {}
    bt = [w.lower() for w in c.words]
    if len(st) > c.rules["limits"]["r10_max_words"] or len(bt) > c.rules["limits"]["r10_max_words"]:
        return "skipped", [], {"skip_reason": "text too long for the run matcher"}
    sm = difflib.SequenceMatcher(None, st, bt, autojunk=False)
    kept = round(100 * sum(m.size for m in sm.get_matching_blocks() if m.size >= cfg["run_words"]) / max(1, len(st)))
    metrics = {"curator_prose_kept_pct": kept}
    if kept >= cfg["min_kept_pct"]:
        return "pass", [], metrics
    return "fail", [_f("R10", cfg["level"], f"only {kept}% of the curator's {len(st)}-word text survives in runs of {cfg['run_words']}+ words (at least {cfg['min_kept_pct']}% expected)")], metrics


def _tokens(text: str) -> list[tuple[str, int, int]]:
    return [(m.group(0).casefold().replace("’", "'"), m.start(), m.end()) for m in _TOKEN.finditer(text)]


def _locate_quote(body: str, quote: str, limits: dict[str, Any], fuzzy_ratio: float) -> Optional[tuple[int, int, bool]]:
    """Find the quote in the body. Returns ``(start, end, exact)`` char offsets or ``None``."""
    qt = [t for t, _, _ in _tokens(quote)]
    bt = _tokens(body)
    if not qt or not bt:
        return None
    words = [t for t, _, _ in bt]
    n = len(qt)
    first = qt[0]
    for i, w in enumerate(words):
        if w == first and words[i:i + n] == qt:
            return bt[i][1], bt[i + n - 1][2], True
    if len(words) > limits["r11_fuzzy_max_words"]:
        return None
    qset, qs = set(qt), " ".join(qt)
    best: tuple[float, int, int] = (0.0, 0, 0)
    tried = 0
    for size in {max(1, n - 1), n, n + 1}:
        for i in range(0, max(1, len(words) - size + 1)):
            window = words[i:i + size]
            if len(qset & set(window)) * 2 < len(qset):
                continue
            tried += 1
            if tried > 300:
                break
            ratio = difflib.SequenceMatcher(None, qs, " ".join(window), autojunk=False).ratio()
            if ratio > best[0]:
                best = (ratio, bt[i][1], bt[min(i + size, len(bt)) - 1][2])
    if best[0] >= fuzzy_ratio:
        return best[1], best[2], False
    return None


def _speaker_in_context(c: _Ctx, text: str, qs: int, qe: int) -> Optional[str]:
    """Name the draft attaches to the quote at ``text[qs:qe]`` (an honorific + name, or two
    capitalised non-stop-list words, next to a speech verb), or ``None``."""
    cfg = c.cfg["R11"]
    w = cfg["window_chars"]
    hon = cfg["name_honorifics"]
    verbs = "|".join(re.escape(v) for v in sorted(cfg["speech_verbs"], key=len, reverse=True))
    cap = r"[A-ZĀĪŪ][\w’'\-āīūṃṇṭḍñḷ]+"
    name_rx = r"(?:(?:%s)\.?\s+)?%s(?:\s+%s){0,3}" % ("|".join(map(re.escape, hon)), cap, cap)
    before = text[max(0, qs - w):qs].rsplit("\n\n", 1)[-1]
    after = text[qe:qe + w].split("\n\n", 1)[0]
    stop = c.not_names - set(hon)
    cands: list[str] = []
    for m in re.finditer(r"(%s)\s+(?:\w+\s+){0,2}?(?:%s)\b" % (name_rx, verbs), before):
        cands.append(m.group(1))
    m = re.match(r"^[\s,.”\"')\]:;]{0,6}(?:(?:%s)\s+(%s)|(%s)\s+(?:\w+\s+){0,2}?(?:%s)\b)" % (verbs, name_rx, name_rx, verbs), after)
    if m:
        cands.append(m.group(1) or m.group(2))
    m = re.match(r"^\W{0,3}[—–-]\s*(%s)" % name_rx, after)
    if m:
        cands.append(m.group(1))
    for cand in reversed(cands):
        words = cand.replace(".", " ").split()
        core = [x for x in words if x not in stop and x not in hon]
        has_hon = any(x in hon for x in words)
        while words and words[0] in stop:
            words = words[1:]
        if core and (has_hon or len(core) >= 2):
            return " ".join(words)
    return None


def _r11(c: _Ctx):
    cfg = c.cfg["R11"]
    quote = c.pack.get("quote")
    if not isinstance(quote, str) or not quote.strip():
        return "n/a", [], {}
    findings: list[dict[str, Any]] = []
    loc = _locate_quote(c.body, quote, c.rules["limits"], cfg["fuzzy_ratio"])
    if loc is None:
        findings.append(_f("R11a", cfg["level_verbatim"], f"the submitted quote does not appear in the draft word for word: “{_excerpt(quote, 80)}”"))
        return "fail", findings, {"quote_found": False}
    qs, qe, exact = loc
    metrics = {"quote_found": True, "quote_exact": exact}
    declared = " ".join([quote] + [v for f in cfg["speaker_fields"] if isinstance(v := c.pack.get(f), str)]).casefold()
    speaker = _speaker_in_context(c, c.body, qs, qe)
    if speaker:
        parts = [p for p in re.split(r"[\s.]+", speaker.casefold()) if p and p not in {h.casefold() for h in cfg["name_honorifics"]}]
        if not any(p in declared for p in parts):
            findings.append(_f("R11b", cfg["level_speaker"], f"the draft attributes the quote to “{speaker}” but the submission names no speaker for it (ask the curator)",
                               field="body", span=(qs, qe)))
    return ("fail" if findings else "pass"), findings, metrics


def _r12(c: _Ctx):
    cfg = c.cfg["R12"]
    pack_text = "\n".join(v for v in c.pack.values() if isinstance(v, str))
    fields = [("body", c.body)] + ([("title", c.extra_title)] if c.extra_title else []) + \
             [(k, c.d["seo"][k]) for k in ("seo_title", "meta_description") if isinstance(c.d["seo"].get(k), str)]
    n = cfg["context_chars"]
    findings: list[dict[str, Any]] = []
    quoted_cache: dict[str, list[tuple[int, int]]] = {}
    for entry in cfg["variants"]:
        rx = re.compile(entry["pattern"])
        for name, text in fields:
            quoted = quoted_cache.setdefault(name, _quoted_spans(text) if name == "body" else [])
            for m in rx.finditer(text):
                if any(a <= m.start() and m.end() <= b for a, b in quoted):
                    continue
                lo = max(text.rfind("\n", 0, m.start()) + 1, m.start() - n)
                hi = m.end() + n
                nl = text.find("\n", m.end(), hi)
                window = text[lo: nl if nl >= 0 else hi]
                if len(window) > len(m.group(0)) + 4 and window in pack_text:
                    continue  # curator wrote it that way; R10 asks us to keep curator text
                findings.append(_f("R12", cfg["level"], f"spelling variant “{m.group(0)}” — house spelling is “{entry['use']}”",
                                   field=name, span=(m.start(), m.end()) if name in ("body", "title") else None))
    return ("fail" if findings else "pass"), findings[:30], {}


def _r13(c: _Ctx):
    cfg = c.cfg["R13"]
    pw = len(WORD.findall(_pack_text(c.pack, cfg["pack_fields"])))
    if pw == 0:
        return "n/a", [], {}
    metrics = {"pack_words": pw, "draft_words": c.nw}
    if pw >= cfg["pack_words_below"] or c.nw <= cfg["max_ratio"] * pw:
        return "pass", [], metrics
    return "fail", [_f("R13", cfg["level"], f"draft is {c.nw} words from a {pw}-word submission (more than {cfg['max_ratio']}x): padding — ask the curator for more instead of filling")], metrics


def _r14(c: _Ctx):
    cfg = c.cfg["R14"]
    if c.nw >= cfg["below_words"]:
        return "n/a", [], {}
    blocks = re.split(r"\n\s*\n", c.body)
    # the headline is the first block of the body (also when a separate `title` key repeats it)
    first = blocks[0].strip().casefold()
    start = 1 if (c.d["title_in_body"] or first == c.title.casefold()) else 0
    heads = 0
    for blk in blocks[start:]:
        b = blk.strip()
        if not b:
            continue
        if b.startswith("#"):
            heads += 1
        elif 0 < len(WORD.findall(b)) <= cfg["heading_max_words"] and not b.startswith("[[") and not re.search(r"[.!?”\"]\s*$", b):
            heads += 1
    metrics = {"headings": heads}
    if heads <= cfg["max_headings"]:
        return "pass", [], metrics
    return "fail", [_f("R14", cfg["level"], f"{heads} headings in a {c.nw}-word post (at most {cfg['max_headings']} under {cfg['below_words']} words)")], metrics


def _r15(c: _Ctx):
    cfg = c.cfg["R15"]
    text = c.body + ("\n" + c.extra_title if c.extra_title else "")
    n = len(re.findall(cfg["pattern"], text))
    if n <= cfg["max_emoji"]:
        return "pass", [], {"emoji": n}
    return "fail", [_f("R15", cfg["level"], f"{n} emoji (at most {cfg['max_emoji']}; a single 🙏 at the close is the house habit)")], {"emoji": n}


def _r16(c: _Ctx):
    cfg = c.cfg["R16"]
    findings: list[dict[str, Any]] = []
    kw = c.d["seo"].get("focus_keyword")
    if isinstance(kw, str) and kw.strip():
        n = len(re.findall(re.escape(kw.strip()), c.prose, re.I))
        if n > cfg["max_focus_keyword"]:
            findings.append(_f("R16", cfg["level"], f"focus keyword “{_excerpt(kw, 40)}” appears {n} times (at most {cfg['max_focus_keyword']})"))
    stop = set(_split_words(cfg["stopwords"], "stopwords"))
    toks = [w.lower().strip("’'-") for w in c.words]
    reported: list[str] = []
    for n in range(cfg["ngram_max"], cfg["ngram_min"] - 1, -1):
        counts = Counter(tuple(toks[i:i + n]) for i in range(len(toks) - n + 1))
        for gram, k in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
            if k < cfg["ngram_repeat"]:
                break
            if all(g in stop or not g for g in gram):
                continue
            text = " ".join(gram)
            if any(text in r for r in reported):
                continue
            reported.append(text)
            if len(reported) <= 5:
                findings.append(_f("R16", cfg["level"], f"the phrase “{text}” is repeated {k} times (at most {cfg['ngram_repeat'] - 1})"))
    return ("fail" if findings else "pass"), findings, {}


_RUNNERS = (("R1", _r1), ("R2", _r2), ("R3", _r3), ("R4", _r4), ("R6", _r6), ("R7", _r7), ("R8", _r8),
            ("R10", _r10), ("R11", _r11), ("R12", _r12), ("R13", _r13), ("R14", _r14), ("R15", _r15), ("R16", _r16))


def lint_blog(draft: Any, pack: Any = None, rules: Any = None) -> dict[str, Any]:
    """Lint one blog draft against the submission. Never raises for odd *input* (any Python
    value is accepted); raises :class:`LintRulesError` only when the rules are unusable.
    A rule that crashes is reported as a ``LINT_ERROR`` warn finding and listed in ``errors``."""
    rules = _resolve_rules(rules)
    d = normalize_draft(draft)
    pk = _pack_dict(pack)
    limit = rules["limits"]["max_chars"]
    findings: list[dict[str, Any]] = []
    statuses: dict[str, str] = {}
    metrics: dict[str, Any] = {}
    skipped: list[dict[str, str]] = []
    errors: list[str] = []
    if d["shape"] == "missing" or not d["body"].strip():
        findings.append(_f("C1", "hard", "the draft has no blog text (no `blog` object with a non-empty `body`, and no string)"))
        return _report(rules, d, findings, statuses, metrics, skipped, errors, pk)
    if len(d["body"]) > limit:
        findings.append(_f("C2", "warn", f"the draft is {len(d['body'])} characters; only the first {limit} were checked"))
        d = {**d, "body": d["body"][:limit]}
    c = _Ctx(rules, d, pk)
    for rid, fn in _RUNNERS:
        if not c.cfg[rid].get("enabled", True):
            statuses[rid] = "off"
            continue
        try:
            status, found, m = fn(c)
        except Exception as exc:  # a linter must never take a pipeline down
            errors.append(f"{rid}: {type(exc).__name__}: {exc}")
            findings.append(_f("LINT_ERROR", "warn", f"rule {rid} could not run ({type(exc).__name__})"))
            statuses[rid] = "error"
            continue
        if status == "skipped":
            skipped.append({"id": rid, "reason": m.get("skip_reason", "")})
            status = "n/a"
        statuses[rid] = status
        findings.extend(found)
        metrics.update({k: v for k, v in m.items() if k != "skip_reason"})
    metrics["words"] = c.nw
    return _report(rules, d, findings, statuses, metrics, skipped, errors, pk)


def _report(rules, d, findings, statuses, metrics, skipped, errors, pk) -> dict[str, Any]:
    return {
        "ok": not any(f["level"] == "hard" for f in findings),
        "hard": sum(1 for f in findings if f["level"] == "hard"),
        "warn": sum(1 for f in findings if f["level"] == "warn"),
        "findings": findings,
        "rules": statuses,
        "metrics": metrics,
        "skipped": skipped,
        "errors": errors,
        "rules_version": rules.get("version"),
        "shape": d["shape"],
        "title": d["title"],
        "body": d["body"],
    }


# ---------------------------------------------------------------------------------------------
# command line
# ---------------------------------------------------------------------------------------------

def _read(path: str) -> str:
    return sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")


def _json_or_text(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        return text


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="blog_lint", description="Lint a Stylus blog draft (deterministic; no network).")
    ap.add_argument("draft", help="file with the draft: JSON (blog object / string / full completion) or plain text; - = stdin")
    ap.add_argument("--pack", help="file with the submission: JSON object or 'Date:/Description:/Quote:' text")
    ap.add_argument("--rules", help="rules file (default: lint_rules.yaml beside this module)")
    ap.add_argument("--json", action="store_true", help="print the full report as JSON")
    args = ap.parse_args(argv)
    try:
        draft = _json_or_text(_read(args.draft))
        pack = _json_or_text(_read(args.pack)) if args.pack else None
        report = lint_blog(draft, pack, args.rules)
    except (OSError, LintRulesError) as exc:
        print(f"blog_lint: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"{'OK' if report['ok'] else 'HARD FINDINGS'}: {report['hard']} hard, {report['warn']} warn "
              f"({report['metrics'].get('words', 0)} words, rules v{report['rules_version']})")
        for f in report["findings"]:
            print(f"  [{f['level'].upper():4}] {f['id']}: {f['message']}")
        for s in report["skipped"]:
            print(f"  [skip] {s['id']}: {s['reason']}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
