"""Intake v2 (W08): parse the optional ``intake`` object of the portal's webhook payload,
render it into the fenced pack Stylus reads, and map it to the site service's pack keys.

Spec: ``extra/plans/socialpost/enrich/design/INTAKE_FORM_SPEC.md`` §1, §6, §6b (as patched by
04B / owner decision D4). Three rules shape everything here:

* **Names are data, never looked up.** ``ledBy``, ``quoteSpeaker``, ``storyAuthor`` and the
  program text are carried exactly as the publisher typed them — nothing is validated against a
  list, canonicalised or corrected (D4).
* **Everything is untrusted text.** Each value is re-sanitised here (the portal already did, but
  the webhook body is the trust boundary of *this* process), capped, and every string that reaches
  a task body goes through ``defuse_fence_tags`` at render time.
* **Old payloads are untouched.** No ``intake`` key (or an unusable one) parses to ``None`` and the
  callers keep emitting the pre-v2 text byte for byte.

Caps are a defensive second check: the portal *rejects* over-long input, this module *truncates*
(never rejects a whole real submission because one field is long) and logs what it cut.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from typing import Any, Callable, Optional

logger = logging.getLogger("plugins.platforms.event_post_pipeline")

INTAKE_VERSION = 2

# Per-field caps — the numbers in INTAKE_FORM_SPEC §1/§2; mirrored in the portal's lib/intake.ts.
PROGRAM_TEXT_MAX = 80
PROGRAM_ID_MAX = 64
VENUE_MAX = 120
LED_BY_MAX = 400
WHAT_HAPPENED_MAX_ITEMS = 8
WHAT_HAPPENED_ITEM_MAX = 200
COUNTS_MAX_ITEMS = 4
COUNT_LABEL_MAX = 40
COUNT_VALUE_MAX = 60
MOMENT_MAX = 400
QUOTE_SPEAKER_MAX = 80
PARTNERS_MAX_ITEMS = 5
PARTNER_MAX = 80
THANKS_MAX = 400
WHATS_NEXT_MAX = 200
TEACHING_SOURCE_MAX = 200
PHOTO_NOTES_MAX = 300
STORY_MAX = 6000
STORY_AUTHOR_MAX = 80
OPTIONS_VERSION_MAX = 40
QUOTE_SOURCES = ("talk", "participant", "sign", "phrase")

NO_TEACHER_WHO_LED = "no teacher (self-guided)"  # §6b: satisfies the service's "who led" rule

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# Control characters except \t \n (\r is folded into \n first); also drops C1 controls.
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
_MULTILINE_BLANKS_RE = re.compile(r"\n{3,}")


def _clean(value: Any, max_len: int, *, multiline: bool = False, field: str = "") -> str:
    """NFC, control characters removed, trimmed, capped. Single-line fields have every run of
    whitespace (newlines included) folded to one space so a value can never start a new
    ``Label:`` line of its own; multi-line fields keep ``\\n`` (blank-line runs collapsed)."""
    if not isinstance(value, str):
        return ""
    text = unicodedata.normalize("NFC", value).replace("\r\n", "\n").replace("\r", "\n")
    text = _CONTROL_RE.sub("", text)
    if multiline:
        text = "\n".join(line.rstrip() for line in text.split("\n"))
        text = _MULTILINE_BLANKS_RE.sub("\n\n", text).strip()
    else:
        text = " ".join(text.split())
    if len(text) > max_len:
        logger.warning("event_post_pipeline: intake field %r truncated from %d to %d chars", field, len(text), max_len)
        text = text[:max_len].rstrip()
    return text


def _clean_list(value: Any, max_items: int, max_len: int, *, field: str) -> list[str]:
    if not isinstance(value, list):
        return []
    out = [c for c in (_clean(item, max_len, field=field) for item in value) if c]
    if len(out) > max_items:
        logger.warning("event_post_pipeline: intake field %r had %d items; kept %d", field, len(out), max_items)
        out = out[:max_items]
    return out


def parse_intake(payload: Any) -> Optional[dict[str, Any]]:
    """The validated ``intake`` object of a webhook ``payload``, or ``None`` for a v1 payload.

    ``None`` when ``payload`` is not a dict, has no ``intake`` dict, or the cleaned object carries
    nothing at all. Only empty fields are omitted from the result, so ``{}``-like intakes never
    produce a block of blank labels. Unknown keys are dropped."""
    if not isinstance(payload, dict):
        return None
    raw = payload.get("intake")
    if not isinstance(raw, dict):
        return None

    out: dict[str, Any] = {}

    program = raw.get("program")
    if isinstance(program, dict):
        text = _clean(program.get("text"), PROGRAM_TEXT_MAX, field="program.text")
        pid = _clean(program.get("id"), PROGRAM_ID_MAX, field="program.id")
        if text or pid:
            out["program"] = {"id": pid or None, "text": text}

    scalar_single = (("venue", VENUE_MAX), ("ledBy", LED_BY_MAX), ("quoteSpeaker", QUOTE_SPEAKER_MAX),
                     ("whatsNext", WHATS_NEXT_MAX), ("teachingSource", TEACHING_SOURCE_MAX),
                     ("photoNotes", PHOTO_NOTES_MAX), ("storyAuthor", STORY_AUTHOR_MAX),
                     ("optionsVersion", OPTIONS_VERSION_MAX))
    for key, cap in scalar_single:
        if value := _clean(raw.get(key), cap, field=key):
            out[key] = value
    for key, cap in (("moment", MOMENT_MAX), ("thanks", THANKS_MAX), ("story", STORY_MAX)):
        if value := _clean(raw.get(key), cap, multiline=True, field=key):
            out[key] = value

    if raw.get("noTeacher") is True:
        out["noTeacher"] = True
        out.pop("ledBy", None)  # spec §2 row 4: the box excludes text

    if items := _clean_list(raw.get("whatHappened"), WHAT_HAPPENED_MAX_ITEMS, WHAT_HAPPENED_ITEM_MAX, field="whatHappened"):
        out["whatHappened"] = items
    if items := _clean_list(raw.get("partners"), PARTNERS_MAX_ITEMS, PARTNER_MAX, field="partners"):
        out["partners"] = items

    counts: list[dict[str, str]] = []
    if isinstance(raw.get("counts"), list):
        for row in raw["counts"]:
            if not isinstance(row, dict):
                continue
            label = _clean(row.get("label"), COUNT_LABEL_MAX, field="counts.label")
            value = _clean(row.get("value"), COUNT_VALUE_MAX, field="counts.value")
            if label and value:
                counts.append({"label": label, "value": value})
    if counts:
        out["counts"] = counts[:COUNTS_MAX_ITEMS]

    source = raw.get("quoteSource")
    if isinstance(source, str) and source in QUOTE_SOURCES:
        out["quoteSource"] = source

    end = raw.get("eventDateEnd")
    if isinstance(end, str) and _DATE_RE.match(end.strip()):
        out["eventDateEnd"] = end.strip()

    return out or None


def render_intake_lines(intake: Optional[dict[str, Any]], defuse: Callable[[Any], str]) -> list[str]:
    """The labelled lines (spec §6) for a parsed intake, only for non-empty fields; ``[]`` for
    ``None``. ``defuse`` is the fence-tag defuser, applied to every interpolated value here so no
    caller can forget it. Names/roles are printed exactly as typed."""
    if not intake:
        return []
    lines: list[str] = []
    if program := intake.get("program"):
        text = defuse(program.get("text") or "")
        pid = program.get("id")
        lines.append(f"Program: {text} (id: {defuse(pid)})" if pid and text else
                     f"Program: {defuse(pid)}" if pid else f"Program: {text}")
    if venue := intake.get("venue"):
        lines.append(f"Venue: {defuse(venue)}")
    if end := intake.get("eventDateEnd"):
        lines.append(f"Last day: {defuse(end)}")
    if intake.get("noTeacher"):
        lines.append("Nobody led: yes")
    elif led_by := intake.get("ledBy"):
        lines.append(f"Led by: {defuse(led_by)}")
    if happened := intake.get("whatHappened"):
        lines.append("What happened (in order):")
        lines.extend(f"- {defuse(item)}" for item in happened)
    if counts := intake.get("counts"):
        lines.append("Numbers: " + "; ".join(f"{defuse(c['label'])}: {defuse(c['value'])}" for c in counts))
    if moment := intake.get("moment"):
        lines.append(f"A moment: {defuse(moment)}")
    if source := intake.get("quoteSource"):
        speaker = intake.get("quoteSpeaker")
        detail = f" (speaker: {defuse(speaker)})" if speaker else ""
        if source == "phrase":
            detail = " (just a phrase, not a quote from anyone)" + detail
        lines.append(f"Quote source: {source}{detail}")
    if partners := intake.get("partners"):
        lines.append("Partners: " + "; ".join(defuse(p) for p in partners))
    if thanks := intake.get("thanks"):
        lines.append(f"Thanks: {defuse(thanks)}")
    if whats_next := intake.get("whatsNext"):
        lines.append(f"What's next: {defuse(whats_next)}")
    if teaching := intake.get("teachingSource"):
        lines.append(f"Teaching source: {defuse(teaching)}")
    if photo_notes := intake.get("photoNotes"):
        lines.append(f"Photo notes: {defuse(photo_notes)}")
    if story := intake.get("story"):
        by = intake.get("storyAuthor")
        header = "Story (curator's own words — keep them where possible" + (f"; by: {defuse(by)}" if by else "") + "):"
        lines.append(header)
        lines.append(defuse(story))
    return lines


def to_service_pack(event_pack: dict[str, Any], intake: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Map a submission to the site service's flat snake_case pack keys (§6b; keys verified against
    ``bmcsitegraph/src/bmcsitegraph/search/pack.py`` ``TEXT_FIELDS``/``LIST_FIELDS``). Pure; only
    non-empty values are included. ``real_date`` is deliberately never sent (no duplicate date
    field, D3/Q6). ``storyAuthor``, ``photoNotes``, ``eventDateEnd`` and ``quoteSource`` are not
    retrieval inputs and are not mapped."""
    pack: dict[str, Any] = {}
    for key in ("date", "description", "quote"):
        value = event_pack.get(key)
        if isinstance(value, str) and value.strip():
            pack[key] = value
    if not intake:
        return pack
    if program := intake.get("program"):
        if program.get("text"):
            pack["program"] = program["text"]
    for src, dst in (("venue", "venue"), ("moment", "moment"), ("story", "story"),
                     ("teachingSource", "teaching_source"), ("quoteSpeaker", "quote_speaker"),
                     ("whatsNext", "whats_next")):
        if intake.get(src):
            pack[dst] = intake[src]
    if intake.get("noTeacher"):
        pack["who_led"] = NO_TEACHER_WHO_LED
    elif intake.get("ledBy"):
        pack["who_led"] = intake["ledBy"]
    if intake.get("whatHappened"):
        pack["what_happened"] = list(intake["whatHappened"])
    if intake.get("partners"):
        pack["partners"] = list(intake["partners"])
    if intake.get("counts"):
        pack["numbers"] = [f"{c['label']}: {c['value']}" for c in intake["counts"]]
    if intake.get("thanks"):
        pack["thanks"] = [intake["thanks"]]
    return pack
