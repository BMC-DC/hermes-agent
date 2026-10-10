"""W04 — deterministic blog linter (``blog_lint.py`` + ``lint_rules.yaml``).

Sections: golden drafts vs. RUBRIC.md §2, per-rule behaviour, rules loading, robustness /
property tests, CLI, and "behaviour-neutral" (nothing in the pipeline imports the linter yet).

Fixtures (``fixtures/event_post_pipeline/blog_lint/*.json``) are minimal copies of the nine real
Stylus drafts studied in ``extra/plans/socialpost/enrich/style-study/data/drafts/`` with URLs
redacted; their submissions are untrusted data and are only ever fed to the linter.
"""

from __future__ import annotations

import copy
import json
import random
import subprocess
import sys
import time
from pathlib import Path

import pytest

from plugins.platforms.event_post_pipeline import blog_lint
from plugins.platforms.event_post_pipeline.blog_lint import LintRulesError, lint_blog, load_rules

FIXTURES = Path(__file__).parent / "fixtures" / "event_post_pipeline" / "blog_lint"
MODULE = Path(blog_lint.__file__)
PLUGIN_DIR = MODULE.parent


def fixture(name):
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def ids(report, level=None):
    return [f["id"] for f in report["findings"] if level in (None, f["level"])]


def lint(body, pack=None, *, title="A Quiet Evening of Practice", **extra):
    return lint_blog({"title": title, "body": body, **extra}, pack)


# A body that trips nothing: specific opening and ending, one heading-free flow, no filler.
PARA1 = ("On Saturday, October 3, 2026, forty-two people sat together at the Buddha Meditation Center from nine until noon, "
         "led by Rev. Amitha Khema through three sittings and two walking periods.")
PARA2 = ("Rain fell on the roof for most of the second sitting, and nobody moved. Tea and rice were served at 12:30 in the hall, "
         "and Maria brought lemon cake that disappeared in minutes.")
PARA3 = "The next day-long retreat is on November 7; seats open on Monday."
CLEAN = "\n\n".join([PARA1, PARA2, PARA3])


# ---------------------------------------------------------------------------------------------
# golden: the nine real drafts vs RUBRIC.md §2 (D columns)
# ---------------------------------------------------------------------------------------------

DRAFTS = ["6P", "Vol", "MG", "CG0", "CG1", "Wx", "4E1", "4E2", "Ret"]
# RUBRIC.md §2, Stylus-draft columns, in DRAFTS order. True = pass, False = fail, None = not scored there.
TABLE = {
    "R1":  [True, True, True, False, True, False, True, True, False],
    "R2":  [True, False, True, True, True, True, True, True, True],
    "R3":  [True, False, True, True, True, True, False, False, False],
    "R4":  [False, True, False, False, False, False, True, True, False],
    "R6":  [True, False, False, False, False, False, False, False, False],
    "R7":  [True, False, False, False, False, False, False, False, False],
    "R10": [False, True, False, True, True, False, True, True, True],
    "R11": [True, True, True, False, False, True, True, True, False],
    "R12": [True] * 9,
    "R13": [True, False, True, False, False, True, False, True, False],
    "R14": [True, True, False, True, True, False, True, True, True],
    "R15": [True] * 9,
    "R16": [True, True, None, None, True, True, True, True, True],
}
# Where the deterministic result is *expected* to differ from the hand-scored table (each investigated):
DIVERGENCES = {
    # RUBRIC marks D-Ret R11 as failing because "'Beautiful fall weather' was a simple reflection from the day"
    # presents a non-quote as a quote. The quote IS reproduced verbatim and no speaker is invented; judging
    # whether a fragment is a quotation is the LLM critic's job (RUBRIC marks R11 [C]).
    ("Ret", "R11"): "pass",
}
# The table scored R16 only where a focus_keyword object field existed; the linter also reads it from the
# trailing "focus_keyword:" line of string-form drafts (MG, CG0), where it passes.
STATUS = {True: ("pass", "n/a"), False: ("fail",)}


@pytest.fixture(scope="module")
def reports():
    out = {}
    for name in DRAFTS:
        f = fixture(name)
        out[name] = lint_blog(f["draft"], f["pack"])
    return out


@pytest.mark.parametrize("item", sorted(TABLE))
def test_golden_matches_rubric_table(reports, item):
    for name, expected in zip(DRAFTS, TABLE[item]):
        got = reports[name]["rules"][item]
        if (name, item) in DIVERGENCES:
            assert got == DIVERGENCES[(name, item)], f"{name} {item}"
        elif expected is not None:
            assert got in STATUS[expected], f"{name} {item}: table says {expected}, linter says {got}"


def test_golden_metrics_match_the_prototype(reports):
    # words / specificity per 100 words as printed by style-study/scripts/rubric_score.py (RUBRIC.md §2, tables/19)
    words = [250, 239, 413, 287, 298, 376, 275, 195, 229]
    spec = [2.8, 0.0, 1.5, 1.4, 2.0, 2.1, 1.1, 1.0, 0.9]
    assert [reports[n]["metrics"]["words"] for n in DRAFTS] == words
    assert [reports[n]["metrics"]["specificity_per_100w"] for n in DRAFTS] == spec
    assert reports["6P"]["metrics"]["curator_prose_kept_pct"] == 27   # RUBRIC §4: "Stylus had kept 27% and 43%"
    assert reports["Wx"]["metrics"]["curator_prose_kept_pct"] == 43


def test_golden_overall_ok_and_levels(reports):
    assert reports["6P"]["ok"] is True                      # only warn findings
    for name in DRAFTS:
        if name != "6P":
            assert reports[name]["ok"] is False             # [[internal link opportunity: …]] placeholder: hard (R7)
            assert "R7" in ids(reports[name], "hard")
    for name in DRAFTS:
        assert all(f["level"] in ("hard", "warn") for f in reports[name]["findings"])
        assert not reports[name]["errors"] and not reports[name]["skipped"]
    assert [f["id"] for f in reports["6P"]["findings"] if f["level"] == "hard"] == []


def test_golden_shapes(reports):
    assert reports["MG"]["shape"] == "string" and reports["CG0"]["shape"] == "string"
    assert reports["6P"]["shape"] == "object"
    # string-form drafts keep SEO lines out of the body
    assert "focus_keyword:" not in reports["MG"]["body"]


def test_golden_specific_findings(reports):
    cg1 = reports["CG1"]
    assert any(f["id"] == "R11b" and "Bhante Pannakara" in f["message"] for f in cg1["findings"])
    assert not any(f["id"] == "R11a" for f in cg1["findings"])  # the quote is reproduced verbatim
    ret = reports["Ret"]
    assert "R13" in ids(ret) and "R1" in ids(ret) and "R7" in ids(ret, "hard")
    assert ids(reports["6P"]) .count("R4") == 3                 # three distinct bridge phrases


def test_golden_is_deterministic():
    f = fixture("Wx")
    assert lint_blog(f["draft"], f["pack"]) == lint_blog(copy.deepcopy(f["draft"]), copy.deepcopy(f["pack"]))
    json.dumps(lint_blog(f["draft"], f["pack"]))  # report is JSON-serialisable


# ---------------------------------------------------------------------------------------------
# the clean control draft
# ---------------------------------------------------------------------------------------------

def test_clean_draft_has_no_findings():
    pack = {"description": "Day-long retreat on Saturday October 3 at the center with Rev. Amitha Khema: three sittings, two walking periods, "
                           "forty-two people, tea and rice at 12:30, lemon cake from Maria, rain on the roof, and the next retreat on November 7.", "quote": ""}
    r = lint(CLEAN, pack, focus_keyword="day-long retreat")
    assert r["findings"] == [], r["findings"]
    assert r["ok"] is True and r["hard"] == 0 and r["warn"] == 0


# ---------------------------------------------------------------------------------------------
# R1
# ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("title", [
    "Retreat Recap | BMC DC", "Retreat at BMC DC", "Meditation in Washington DC", "Meditation in Greater Washington, DC",
    "Best Meditation Retreat", "Meditation Classes near me", "A Day at Buddha Meditation Center", "A Day at Buddha Meditation Center DC",
])
def test_r1_keyword_headlines_warn(title):
    r = lint(CLEAN, title=title)
    assert "R1" in ids(r, "warn") and r["rules"]["R1"] == "fail"


def test_r1_human_headline_passes_and_seo_title_is_not_judged():
    r = lint(CLEAN, title="It Rained on the National Mall. Nobody Left.", seo_title="Best Meditation | BMC DC")
    assert r["rules"]["R1"] == "pass"


def test_r1_string_draft_uses_first_line():
    r = lint_blog("Best Meditation Retreat in Washington DC\n\n" + CLEAN, {})
    assert "R1" in ids(r)


# ---------------------------------------------------------------------------------------------
# R2 / R3
# ---------------------------------------------------------------------------------------------

VAGUE = ("Meditation helps us slow down and notice what is here. When we pause and breathe, the mind becomes calmer and "
         "kinder toward itself and others. Practice is simple and open to everyone who wants to try it sometime.")


def test_r2_vague_opening_warns_and_scene_word_rescues():
    assert "R2" in ids(lint(VAGUE + "\n\n" + VAGUE))
    assert lint("It rained. " + VAGUE + "\n\n" + VAGUE)["rules"]["R2"] == "pass"
    assert lint("Forty people came. " + VAGUE)["rules"]["R2"] == "pass"
    assert lint("")["rules"] == {}  # empty body: contract finding only


def test_r2_no_prose_at_all():
    r = lint("# Heading\n\n# Another heading")
    assert "R2" in ids(r)


def test_r3_threshold_comes_from_the_rules_file():
    text = "\n\n".join([VAGUE] * 3)
    assert "R3" in ids(lint(text))
    rules = copy.deepcopy(load_rules())
    rules["rules"]["R3"]["min_per_100"] = 0.0
    assert "R3" not in ids(lint_blog({"title": "T", "body": text}, {}, rules))
    assert lint(CLEAN)["metrics"]["specificity_per_100w"] >= 3


def test_r3_short_text_is_not_rated():
    assert lint("Short note.")["rules"]["R3"] == "n/a"


# ---------------------------------------------------------------------------------------------
# R4
# ---------------------------------------------------------------------------------------------

def test_r4_counts_distinct_phrases_against_max_allowed():
    one = CLEAN + "\n\nThese simple moments matter."
    two = one + " In a world full of noise, we pause."
    assert lint(one)["rules"]["R4"] == "pass"
    r = lint(two)
    assert r["rules"]["R4"] == "fail" and ids(r).count("R4") == 2 and all(f["level"] == "warn" for f in r["findings"] if f["id"] == "R4")
    assert lint(two + " These simple moments return.")["rules"]["R4"] == "fail"
    rules = copy.deepcopy(load_rules())
    rules["rules"]["R4"]["max_allowed"] = 2
    assert lint_blog({"title": "T", "body": two}, {}, rules)["rules"]["R4"] == "pass"


def test_r4_phrase_list_is_data_not_code():
    rules = copy.deepcopy(load_rules())
    rules["rules"]["R4"]["phrases"] = ["zebra crossing"]
    r = lint_blog({"title": "T", "body": CLEAN + "\n\nA zebra crossing. Another zebra crossing."}, {}, rules)
    assert r["rules"]["R4"] == "pass"   # one distinct phrase <= max_allowed (1)
    rules["rules"]["R4"]["phrases"] = ["zebra crossing", "quiet street"]
    r = lint_blog({"title": "T", "body": CLEAN + "\n\nA zebra crossing on a quiet street."}, {}, rules)
    assert r["rules"]["R4"] == "fail"


# ---------------------------------------------------------------------------------------------
# R6
# ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("ending", [
    "Please follow our page and visit our website for more.",
    "May all beings be happy and free from suffering. 🙏",
])
def test_r6_stock_endings_warn(ending):
    r = lint(CLEAN + "\n\n" + ending)
    assert "R6" in ids(r, "warn")


def test_r6_ending_needs_a_specific():
    body = "\n\n".join([PARA1, VAGUE, VAGUE, VAGUE, "We are grateful to everyone who came and shared the day. May the calm continue into daily life."])
    r = lint(body)
    assert "R6" in ids(r) and "event-specific" in " ".join(f["message"] for f in r["findings"] if f["id"] == "R6")
    assert lint(body + " Next retreat: [November 7](https://example.org/retreat)")["rules"]["R6"] == "pass"


# ---------------------------------------------------------------------------------------------
# R7 (hard)
# ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("snippet", [
    "[[internal link opportunity: mindfulness meditation]]", "TODO add the date", "Dear {{name}},", "Lorem ipsum dolor sit amet", "lorem IPSUM x",
])
def test_r7_placeholders_are_hard(snippet):
    r = lint(CLEAN + "\n\n" + snippet)
    assert r["ok"] is False and "R7" in ids(r, "hard")
    f = next(f for f in r["findings"] if f["id"] == "R7")
    assert f["field"] == "body" and r["body"][f["span"][0]:f["span"][1]]


@pytest.mark.parametrize("snippet", ["Todos los asistentes", "Dolores brought tea", "things to do and see", "A to-do list", "Template engines use braces"])
def test_r7_ordinary_words_are_not_placeholders(snippet):
    assert "R7" not in ids(lint(CLEAN + "\n\n" + snippet))


def test_r7_checks_title_and_seo_fields():
    r = lint_blog({"title": "[[headline]]", "body": CLEAN, "seo_title": "TODO", "meta_description": "{{desc}}",
                   "url_slug": "ok", "supporting_keywords": ["fine", "[[kw]]"], "focus_keyword": "x"}, {})
    fields = {f["field"] for f in r["findings"] if f["id"] == "R7"}
    assert {"title", "seo_title", "meta_description", "supporting_keywords"} <= fields and not r["ok"]


def test_r7_level_comes_from_the_rules_file():
    rules = copy.deepcopy(load_rules())
    rules["rules"]["R7"]["level"] = "warn"
    assert lint_blog({"title": "T", "body": CLEAN + " [[x]]"}, {}, rules)["ok"] is True


# ---------------------------------------------------------------------------------------------
# R8
# ---------------------------------------------------------------------------------------------

def hard_or_warn(r, rid):
    return [f["level"] for f in r["findings"] if f["id"] == rid]


@pytest.mark.parametrize("snippet", [
    "See [the page]() now.", "See [](https://example.org/a) now.", "See [x](javascript:alert(1)) now.", "See [x](DATA:text/html,hi) now.",
    "See [x](https://example.org/a now.", "Broken ](https://example.org) markup", "See [x](  ) now.",
])
def test_r8a_malformed_links_are_hard(snippet):
    r = lint(CLEAN + "\n\n" + snippet)
    assert "hard" in hard_or_warn(r, "R8a") and not r["ok"], r["findings"]


def test_r8b_generic_anchor_and_r8c_duplicates_warn():
    r = lint(CLEAN + "\n\nSee [here](https://example.org/a), and [the retreat page](https://example.org/a/) and [website](https://example.org/b).")
    assert hard_or_warn(r, "R8b") == ["warn", "warn"]          # "here" and "website"; the duplicate target a / a/ is R8c
    assert hard_or_warn(r, "R8c") == ["warn"]
    assert r["ok"] is True


def test_r8_well_formed_links_and_images_pass():
    r = lint(CLEAN + "\n\nSee [the November retreat](https://example.org/retreat) and ![a photo](https://example.org/p.jpg).")
    assert r["rules"]["R8"] == "pass" and r["metrics"]["links"] == 1


# ---------------------------------------------------------------------------------------------
# R10
# ---------------------------------------------------------------------------------------------

CURATOR = " ".join(f"The {w} afternoon session explored kindness patience generosity and calm in ordinary family life together." for w in
                   ["first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth", "ninth", "tenth"])


def test_r10_keeps_curator_prose():
    pack = {"description": CURATOR, "quote": ""}
    assert lint(CURATOR + "\n\n" + PARA3, pack)["rules"]["R10"] == "pass"
    r = lint("Families gathered one afternoon to practise kindness. " * 12, pack)
    assert r["rules"]["R10"] == "fail" and r["metrics"]["curator_prose_kept_pct"] < 60


def test_r10_not_applied_to_short_packs_and_skipped_when_huge():
    assert lint(CLEAN, {"description": "short pack"})["rules"]["R10"] == "n/a"
    rules = copy.deepcopy(load_rules())
    rules["limits"]["r10_max_words"] = 50
    r = lint_blog({"title": "T", "body": CURATOR}, {"description": CURATOR}, rules)
    assert r["skipped"] == [{"id": "R10", "reason": "text too long for the run matcher"}] and r["rules"]["R10"] == "n/a"


# ---------------------------------------------------------------------------------------------
# R11
# ---------------------------------------------------------------------------------------------

QUOTE = "We are always trying to push our thoughts away, but our job is to teach the mind to stay still."


def q_body(text):
    return f"{PARA1}\n\n{text}\n\n{PARA3}"


def test_r11_verbatim_quote_passes_with_style_differences():
    pack = {"quote": QUOTE}
    assert lint(q_body(f"Bhante said: “{QUOTE}”"), {**pack, "speaker": "Bhante"})["rules"]["R11"] == "pass"
    curly = QUOTE.replace("We are", "We’re")
    assert lint(q_body(f'"{curly}"'), {"quote": "We're always trying to push our thoughts away, but our job is to teach the mind to stay still"})["rules"]["R11"] == "pass"


def test_r11_typo_fix_passes_but_a_rewrite_fails():
    typo = QUOTE.replace("always", "allways")
    assert lint(q_body(f"“{QUOTE}”"), {"quote": typo})["rules"]["R11"] == "pass"
    r = lint(q_body("“We keep pushing thoughts away, yet the mind should simply rest.”"), {"quote": QUOTE})
    assert r["rules"]["R11"] == "fail" and ids(r).count("R11a") == 1
    assert "R11a" in ids(lint(q_body("No quote here."), {"quote": QUOTE}))


def test_r11_quote_is_optional():
    assert lint(CLEAN, {"quote": ""})["rules"]["R11"] == "n/a"
    assert lint(CLEAN, {})["rules"]["R11"] == "n/a"


@pytest.mark.parametrize("sentence", [
    "Bhante Pannakara shared: “{q}”",
    "Rev. Amitha Khema reminded everyone, “{q}”",
    "“{q}” said Bhante Pannakara.",
    "“{q}” — Bhante Pannakara",
    "“{q}” Bhante Pannakara said afterwards.",
    "As Maria Lopez put it: “{q}”",
])
def test_r11b_flags_a_speaker_the_pack_does_not_name(sentence):
    r = lint(q_body(sentence.format(q=QUOTE)), {"quote": QUOTE})
    assert "R11b" in ids(r, "warn"), r["findings"]
    assert not any(f["id"] == "R11a" for f in r["findings"])


def test_r11b_accepts_a_speaker_the_pack_names():
    body = q_body(f"Bhante Pannakara shared: “{QUOTE}”")
    for pack in ({"quote": QUOTE, "speaker": "Bhante Pannakara"}, {"quote": QUOTE, "quote_source": "Pannakara, Walk for Peace"},
                 {"quote": QUOTE + " — Bhante Pannakara"}):
        assert "R11b" not in ids(lint(body, pack)), pack


def test_r11b_does_not_flag_non_people_or_plain_quotes():
    for sentence in ("The Buddha said: “{q}”", "Afterwards, “{q}”", "Everyone remembered the words: “{q}”", "This Sunday we heard “{q}”"):
        r = lint(q_body(sentence.format(q=QUOTE)), {"quote": QUOTE})
        assert "R11b" not in ids(r), (sentence, r["findings"])


# ---------------------------------------------------------------------------------------------
# R12 (never rewrites; hard for misspellings outside quotes; curator text exempt)
# ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("variant, level", [
    ("Bhanthe Pannakara", "hard"), ("Banthe Pannakara", "hard"), ("Bhante Panacara", "hard"), ("Mahamewnawa", "hard"),
    ("Buddhist Meditation Center", "hard"), ("Venerable Pannakara", "warn"), ("Venerable Bhante Pannakara", "warn"),
    ("Ven. Pannakara", "warn"), ("Pannakara Thero", "warn"), ("Venerable Amitha Khema", "warn"),
])
def test_r12_variants_are_reported_with_their_level(variant, level):
    r = lint(CLEAN + f"\n\nWe thank {variant} for coming.")
    assert level in hard_or_warn(r, "R12"), r["findings"]
    assert r["ok"] is (level == "warn")


def test_r12_house_spelling_is_fine_and_text_is_never_rewritten():
    body = CLEAN + "\n\nWe thank Bhante Pannakara and Rev. Amitha Khema for coming."
    r = lint(body)
    assert r["rules"]["R12"] == "pass"
    bad = CLEAN + "\n\nWe thank Bhanthe Pannakara for coming."
    r = lint_blog({"title": "T", "body": bad}, {})
    assert r["body"] == bad                      # reported, never rewritten
    f = next(f for f in r["findings"] if f["id"] == "R12")
    assert r["body"][f["span"][0]:f["span"][1]] == "Bhanthe"


def test_r12_not_applied_inside_quotation_marks_or_to_curator_text():
    assert "R12" not in ids(lint(CLEAN + "\n\nHe said, “Thank you Bhanthe Pannakara for coming.”"))
    assert "R12" not in ids(lint(CLEAN + '\n\nHe said, "Thank you Bhanthe Pannakara for coming."'))
    pack = {"description": "Thank you Bhanthe Pannakara for visiting us at the center for a meet and greet"}
    assert "R12" not in ids(lint(CLEAN + "\n\nThank you Bhanthe Pannakara for visiting us at the center.", pack))
    assert "R12" in ids(lint(CLEAN + "\n\nWe also met Bhanthe Pannakara there.", pack))


def test_r12_checks_seo_title_and_meta_description_too():
    r = lint(CLEAN, seo_title="Bhanthe talk", meta_description="A talk with Banthe Pannakara")
    assert {f["field"] for f in r["findings"] if f["id"] == "R12"} == {"seo_title", "meta_description"}


def test_r12_spellings_are_data():
    rules = copy.deepcopy(load_rules())
    rules["rules"]["R12"]["never_print"] = [{"pattern": r"\bzebra\b", "use": "horse"}]
    assert "R12" in ids(lint_blog({"title": "T", "body": CLEAN + " A zebra."}, {}, rules))
    assert "R12" not in ids(lint_blog({"title": "T", "body": CLEAN + " Bhanthe."}, {}, rules))


# ---------------------------------------------------------------------------------------------
# R13 / R14 / R15 / R16
# ---------------------------------------------------------------------------------------------

def test_r13_padding():
    thin = {"description": "Day log retreat", "quote": "Beautiful fall weather"}   # 6 words
    r = lint(CLEAN, thin)
    assert "R13" in ids(r, "warn") and r["metrics"]["pack_words"] == 6
    ok = lint(CLEAN, {"description": " ".join(["word"] * 100)})
    assert ok["rules"]["R13"] == "pass"
    assert lint(CLEAN, {})["rules"]["R13"] == "n/a" and lint(CLEAN, None)["rules"]["R13"] == "n/a"


def test_r14_headings_counted_for_markdown_and_plain_lines():
    plain_heads = "\n\n".join([PARA1, "An Evening of Practice", PARA2, "The Middle Way of Effort", PARA3, "Closing Words", "We thank Maria and Tom."])
    assert "R14" in ids(lint(plain_heads))
    md = "\n\n".join([PARA1, "## One", PARA2, "## Two", PARA3, "### Three", "We thank Maria and Tom."])
    assert "R14" in ids(lint(md))
    two = "\n\n".join([PARA1, "## One", PARA2, "## Two", PARA3])
    assert lint(two)["rules"]["R14"] == "pass"
    long_post = "\n\n".join([PARA1 + " " + PARA2] * 14 + ["## A", "## B", "## C"])
    assert lint(long_post)["rules"]["R14"] == "n/a"


def test_r14_first_block_headline_is_not_counted():
    body = "A Quiet Evening of Practice\n\n" + "\n\n".join([PARA1, "## One", PARA2, "## Two", PARA3])
    assert lint(body)["rules"]["R14"] == "pass"             # title key repeated in the body
    assert lint_blog(body, {})["rules"]["R14"] == "pass"    # string draft: first line is the headline


def test_r15_emoji():
    assert lint(CLEAN + "\n\nMay all beings be well. 🙏")["rules"]["R15"] == "pass"
    r = lint(CLEAN + "\n\nWe loved it ✨🌿🙏")
    assert "R15" in ids(r, "warn") and r["metrics"]["emoji"] == 3
    assert "R15" in ids(lint(CLEAN, title="Retreat 🌿🙏"))


def test_r16_focus_keyword_and_repeated_phrases():
    kw = "meditation retreat"
    body = CLEAN + "\n\n" + " ".join(["The meditation retreat was calm."] * 4)
    r = lint(body, focus_keyword=kw)
    assert "R16" in ids(r) and "appears" in next(f["message"] for f in r["findings"] if f["id"] == "R16")
    assert lint(CLEAN + " The meditation retreat ended.", focus_keyword=kw)["rules"]["R16"] == "pass"
    stuffed = CLEAN + "\n\n" + " ".join(["walk with us today"] * 6)
    r = lint(stuffed)
    assert any("walk with us today" in f["message"] for f in r["findings"] if f["id"] == "R16")
    assert len([f for f in r["findings"] if f["id"] == "R16"]) == 1   # sub-phrases are not reported again


def test_r16_ignores_all_stopword_repeats():
    body = CLEAN + "\n\n" + " ".join(["in the of"] * 10)
    assert lint(body)["rules"]["R16"] == "pass"


def test_r16_reads_focus_keyword_from_string_form_draft():
    body = CLEAN + "\n\n" + " ".join(["The day retreat was calm."] * 5) + "\n\nfocus_keyword: day retreat\nseo_title: x"
    r = lint_blog(body, {})
    assert "R16" in ids(r) and "focus_keyword" not in r["body"] and r["shape"] == "string"


# ---------------------------------------------------------------------------------------------
# draft / pack shapes
# ---------------------------------------------------------------------------------------------

def test_object_string_and_full_completion_shapes_agree():
    obj = {"body": "Headline Here\n\n" + CLEAN, "focus_keyword": "retreat"}
    a = lint_blog(obj, {})
    b = lint_blog("Headline Here\n\n" + CLEAN + "\n\nfocus_keyword: retreat", {})
    c = lint_blog({"event_title": "x", "fb": "f", "ig": "i", "blog": obj}, {})
    assert a["findings"] == b["findings"] == c["findings"]


def test_draft_without_blog_or_body_is_a_contract_failure():
    for draft in ({"event_title": "x", "fb": "f", "ig": "i"}, {"blog": 5}, {"body": None}, {"body": ["a"]}, None, 123, [], [CLEAN], "", "   \n ", b"bytes"):
        r = lint_blog(draft, {})
        assert [f["id"] for f in r["findings"]] == ["C1"] and r["ok"] is False and r["rules"] == {}, draft


def test_pack_may_be_dict_labelled_text_or_plain_text_or_none():
    text = "Date: 2026-10-03\nDescription: Day log retreat\nQuote: Beautiful fall weather\nFirst photo: x\nLast photo: y\nRest: []"
    assert blog_lint.parse_pack_text(text) == {"date": "2026-10-03", "description": "Day log retreat", "quote": "Beautiful fall weather",
                                               "first_photo": "x", "last_photo": "y", "rest": "[]"}
    assert blog_lint.parse_pack_text("just words") == {"description": "just words"}
    a = lint(CLEAN, text)
    b = lint(CLEAN, {"Date": "2026-10-03", "Description": "Day log retreat", "Quote": "Beautiful fall weather"})
    assert a["findings"] == b["findings"] and "R13" in ids(a)
    for pack in (None, 5, [], {"description": None, "quote": 7}):
        assert lint(CLEAN, pack)["rules"]["R13"] == "n/a"


# ---------------------------------------------------------------------------------------------
# rules loading
# ---------------------------------------------------------------------------------------------

def test_rules_file_ships_beside_the_module_and_loads():
    assert (PLUGIN_DIR / "lint_rules.yaml").is_file()
    rules = load_rules()
    assert rules["version"] == 1 and set(rules["rules"]) == set(blog_lint._RULE_IDS)
    assert lint_blog(CLEAN, {})["rules_version"] == 1


def test_no_phrase_lists_in_code():
    src = MODULE.read_text(encoding="utf-8")
    for needle in ("follow our page", "Bhanthe", "Banthe", "Panacara", "In a world", "abstract idea", "internal link opportunity"):
        assert needle not in src, needle


def _write(tmp_path, text):
    p = tmp_path / "rules.yaml"
    p.write_text(text, encoding="utf-8")
    return p


def test_bad_rules_fail_loudly(tmp_path):
    good = (PLUGIN_DIR / "lint_rules.yaml").read_text(encoding="utf-8")
    with pytest.raises(LintRulesError):
        load_rules(tmp_path / "missing.yaml")
    with pytest.raises(LintRulesError):
        load_rules(_write(tmp_path, "a: [unclosed"))
    with pytest.raises(LintRulesError):
        load_rules(_write(tmp_path, "- just a list"))
    with pytest.raises(LintRulesError, match="level"):
        load_rules(_write(tmp_path, good.replace("level: hard\n    enabled: true\n    patterns:\n      - '\\[\\['", "level: severe\n    enabled: true\n    patterns:\n      - '\\[\\['")))
    with pytest.raises(LintRulesError, match="bad regex"):
        load_rules(_write(tmp_path, good.replace("'\\bBest\\b'", "'(unclosed'")))
    with pytest.raises(LintRulesError, match="unknown rule"):
        load_rules(_write(tmp_path, good + "\n  R99:\n    level: warn\n"))
    with pytest.raises(LintRulesError):
        lint_blog(CLEAN, {}, rules={"version": 1})


def test_rules_path_argument_and_default_are_not_mutated(tmp_path):
    p = _write(tmp_path, (PLUGIN_DIR / "lint_rules.yaml").read_text(encoding="utf-8").replace("max_emoji: 1", "max_emoji: 5"))
    assert lint_blog(CLEAN + " ✨🌿🙏", {}, p)["rules"]["R15"] == "pass"
    assert lint_blog(CLEAN + " ✨🌿🙏", {})["rules"]["R15"] == "fail"
    before = copy.deepcopy(load_rules())
    lint_blog(CLEAN, {}, copy.deepcopy(before))
    assert load_rules() == before


def test_a_rule_can_be_switched_off():
    rules = copy.deepcopy(load_rules())
    rules["rules"]["R15"]["enabled"] = False
    r = lint_blog({"title": "T", "body": CLEAN + " ✨🌿🙏"}, {}, rules)
    assert r["rules"]["R15"] == "off" and "R15" not in ids(r)


def test_a_crashing_rule_is_reported_not_raised(monkeypatch):
    def boom(c):
        raise RuntimeError("nope")
    monkeypatch.setattr(blog_lint, "_RUNNERS", (("R1", boom),) + blog_lint._RUNNERS[1:])
    r = lint(CLEAN)
    assert r["rules"]["R1"] == "error" and r["errors"] and "LINT_ERROR" in ids(r, "warn") and r["ok"] is True


# ---------------------------------------------------------------------------------------------
# robustness / property tests
# ---------------------------------------------------------------------------------------------

ODD = [
    "", " ", "\n\n\n", "\x00", "\x00" * 1000, "🙏" * 5000, "🙏", "a" * 100_000, "word " * 20_000, "[" * 100_000, "](" * 50_000, "[x](" * 20_000,
    "[[" * 10_000, "{{" * 10_000, '"' * 100_000, "“" * 50_000, "#" * 100_000, "\n\n".join(["## h"] * 5000), "A. " * 30_000,
    "مرحبا بالعالم " * 2000, "שלום עולם " * 2000, "සිංහල පෙළ " * 2000, "é" * 5000, "​" * 10_000, "‮" * 100,
    "Ｂｈａｎｔｈｅ ＰＡＮＮＡＫＡＲＡ", "Bhanthe " * 5000, "TODO " * 5000, "!" * 100_000, "-" * 100_000, "’" * 50_000,
    "\U0001F468‍\U0001F469‍\U0001F467 " * 3000,
]


@pytest.mark.parametrize("text", ODD, ids=[f"odd{i}" for i in range(len(ODD))])
def test_odd_and_huge_inputs_never_raise_and_stay_fast(text):
    for draft in (text, {"title": text[:50], "body": text}, {"body": text, "focus_keyword": text[:30]}):
        t0 = time.monotonic()
        r = lint_blog(draft, {"description": text[:2000], "quote": text[:200]})
        assert time.monotonic() - t0 < 8, "lint took too long"
        assert isinstance(r["ok"], bool) and not r["errors"], r["errors"]
        assert all(f["level"] in ("hard", "warn") and isinstance(f["message"], str) for f in r["findings"])
        json.dumps(r)


def test_huge_text_is_truncated_with_a_notice():
    r = lint("word " * 100_000)
    assert "C2" in ids(r, "warn") and len(r["body"]) <= load_rules()["limits"]["max_chars"]


def test_random_mixtures_never_raise():
    rnd = random.Random(20261010)
    pieces = ["[[", "]]", "](", "[", "]", "(", ")", '"', "“", "”", "\n\n", "\n", "# ", "## ", "🙏", "✨", "Bhanthe", "Bhante", "TODO", "{{", "}}",
              "'", "’", "—", "…", "​", "\x00", "A", "b", "12", "Rev.", "said", ":", "focus_keyword: x", "http://x.y/z", "![i](u)", "[a](b)",
              "These simple moments", "follow our page and visit our website", "ا", "ש", "ස", "\t", " ", "word"]
    for _ in range(300):
        text = "".join(rnd.choice(pieces) for _ in range(rnd.randint(0, 120)))
        pack = {"description": "".join(rnd.choice(pieces) for _ in range(rnd.randint(0, 40))), "quote": rnd.choice(pieces + ["Peace comes from within"])}
        shape = rnd.choice(["str", "obj", "full"])
        draft = text if shape == "str" else {"title": rnd.choice(pieces), "body": text} if shape == "obj" else {"blog": {"body": text}}
        r = lint_blog(draft, pack)
        assert not r["errors"], (r["errors"], text)
        assert r["ok"] == (r["hard"] == 0)


def test_input_objects_are_not_mutated():
    draft = {"title": "T", "body": CLEAN + "\n\nfocus_keyword: x", "supporting_keywords": ["a"]}
    pack = {"description": "d", "quote": "q"}
    d0, p0 = copy.deepcopy(draft), copy.deepcopy(pack)
    lint_blog(draft, pack)
    assert draft == d0 and pack == p0


def test_rtl_and_non_latin_text_does_not_trip_name_or_spelling_rules():
    body = "\n\n".join(["مرحبا بالعالم. اجتمع الناس في المركز يوم السبت.", "שלום עולם. התכנסנו במרכז.", "සිංහල පෙළ. අපි එක්ව භාවනා කළෙමු."])
    r = lint(body, title="مرحبا")
    assert r["ok"] is True


# ---------------------------------------------------------------------------------------------
# command line / entry point
# ---------------------------------------------------------------------------------------------

def test_cli_main_exit_codes_and_output(tmp_path, capsys):
    f = fixture("Ret")
    d, p = tmp_path / "draft.json", tmp_path / "pack.json"
    d.write_text(json.dumps(f["draft"]), encoding="utf-8")
    p.write_text(json.dumps(f["pack"]), encoding="utf-8")
    assert blog_lint.main([str(d), "--pack", str(p)]) == 1
    out = capsys.readouterr().out
    assert "HARD FINDINGS" in out and "R7" in out
    assert blog_lint.main([str(d), "--pack", str(p), "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False
    clean = tmp_path / "clean.txt"
    clean.write_text("A Quiet Evening\n\n" + CLEAN, encoding="utf-8")
    assert blog_lint.main([str(clean)]) == 0
    assert blog_lint.main([str(tmp_path / "nope.json")]) == 2
    assert blog_lint.main([str(clean), "--rules", str(tmp_path / "nope.yaml")]) == 2


def test_cli_runs_as_a_standalone_script(tmp_path):
    clean = tmp_path / "clean.txt"
    clean.write_text("A Quiet Evening\n\n" + CLEAN, encoding="utf-8")
    proc = subprocess.run([sys.executable, "-I", str(MODULE), str(clean)], capture_output=True, text=True, cwd=str(tmp_path), timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("OK:")


# ---------------------------------------------------------------------------------------------
# behaviour-neutral: not wired into the pipeline yet (that is W05/W11)
# ---------------------------------------------------------------------------------------------

def test_linter_is_not_wired_into_the_pipeline_yet():
    for name in ("__init__.py", "adapter.py", "hooks.py", "pipeline.py", "pre_tool_call_guard.py", "tools.py", "models.py", "store.py"):
        assert "blog_lint" not in (PLUGIN_DIR / name).read_text(encoding="utf-8"), name
