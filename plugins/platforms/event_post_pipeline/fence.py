"""Defusing of the ``<untrusted_submission>`` fence inside text that is placed in
(or next to) a fenced Kanban task body.

The pipeline marks public-form text as data by wrapping it in
``<untrusted_submission>`` ... ``</untrusted_submission>`` (pipeline.py). Before
this module, a literal closing tag inside the submission ended the fence early,
so whatever followed it was presented to Stylus (an LLM that can run tools) as
trusted instructions. Every place that interpolates user-controlled text into a
task body or task comment now passes it through :func:`defuse_fence_tags`.

Method (one method, applied everywhere): when a fence tag is detected, the
**opening angle bracket of that tag only** is replaced by the visible text
``&lt;``, so ``</untrusted_submission>`` becomes ``&lt;/untrusted_submission>``.
Nothing else is touched — text that contains no fence tag is returned
**byte-for-byte unchanged** (the same ``str`` object), which is what keeps
ordinary submissions rendering exactly as before. The function is idempotent
(a defused tag no longer matches), deterministic, standard-library only, and
linear in the length of the text.

What counts as a fence tag (detection runs on a *projection* of the text, never
on the text that is returned):

* the name ``untrusted_submission``, any letter case;
* an opening ``<`` or a Unicode look-alike (fullwidth ``＜``, ``〈``, ``‹``,
  ``﹤``, ``❮``, ``⟨``, ``《`` ...), optionally followed by whitespace, an
  optional slash (``/``, ``／``, ``⁄``, ``∕``, ``\\``) and more whitespace —
  so ``</ untrusted_submission >`` and ``< /UNTRUSTED_SUBMISSION>`` match;
* invisible characters anywhere inside the tag (zero-width space/joiner,
  soft hyphen, BOM, bidi controls, control characters, combining marks,
  variation selectors, Hangul/Braille fillers) are ignored;
* compatibility forms are folded with NFKC (fullwidth letters and underscore,
  ``ſ``→``s`` ...), plus a small table of Cyrillic/Greek/Latin letters that look
  like the Latin letters in the tag name.

Known limits (documented, not hidden): the look-alike letter table is
best-effort, not the full Unicode confusables set; a tag name spelled with a
homoglyph outside the table is not detected. This is defence in depth for a
fence an LLM reads — it is not a substitute for keeping Stylus's toolset small
(see PLUGIN_DESIGN §9 risk 1). The closing ``>`` is not required for a match
(``</untrusted_submission`` followed by anything is defused), because an LLM
reads the truncated form as a tag too.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

# ``site_candidates`` / ``site_context`` (research step, DESIGN_11): the plugin-written blocks that carry
# site material into a WebMaster task body and a research brief into a Stylus task comment. Text that
# is placed inside them is defused exactly like text inside the submission fence.
FENCE_TAG_NAMES = ("untrusted_submission", "site_candidates", "site_context")
DEFUSED_OPEN = "&lt;"

# Angle-bracket look-alikes (all folded to "<" in the projection).
_OPEN_LOOKALIKES = "＜〈‹﹤❮⟨《〈⟪❬⧼"
_SLASH_LOOKALIKES = {"／": "/", "⁄": "/", "∕": "/", "\\": "/", "⧸": "/", "╱": "/"}
# Invisible characters that are not in Unicode category Cf/Cc/Mn.
_EXTRA_INVISIBLE = frozenset("ㅤᅟᅠﾠ⠀឴឵")
# Letters that look like the Latin letters of the fence tag name.
_HOMOGLYPHS = {
    "е": "e", "о": "o", "і": "i", "ѕ": "s", "ԁ": "d", "а": "a", "р": "p", "с": "c", "ԍ": "g",
    "ο": "o", "υ": "u", "ι": "i", "ε": "e", "ɡ": "g", "ı": "i", "ո": "n", "ս": "u", "ⅰ": "i",
    "ｕ": "u", "ᴜ": "u", "ɴ": "n", "ᴛ": "t", "ʀ": "r", "ᴅ": "d", "ꜱ": "s", "х": "x",
}
_TRANSLATE = {ord(c): "<" for c in _OPEN_LOOKALIKES}
_TRANSLATE.update({ord(k): v for k, v in _SLASH_LOOKALIKES.items()})
_TRANSLATE.update({ord(k): v for k, v in _HOMOGLYPHS.items()})


def _tag_re(names: tuple[str, ...]) -> "re.Pattern[str]":
    alternation = "|".join(re.escape(n) for n in names)
    return re.compile(rf"<\s*/?\s*(?:{alternation})")


_DEFAULT_RE = _tag_re(FENCE_TAG_NAMES)
# A fence tag needs an opening bracket (ASCII, or a look-alike / a character NFKC folds to "<");
# text without one cannot contain a tag, so it skips the per-character projection entirely.
_BRACKET_PREFILTER = re.compile("[<%s\uff1c\ufe64]" % _OPEN_LOOKALIKES)


def _is_invisible(ch: str) -> bool:
    if ch in _EXTRA_INVISIBLE:
        return True
    cat = unicodedata.category(ch)
    if cat in ("Cf", "Mn", "Me"):
        return True
    return cat == "Cc" and not ch.isspace()


def _project(text: str) -> tuple[str, list[int]]:
    """Return ``(projection, origin)``: the folded, invisible-free, case-folded text and,
    for every projection character, the index in ``text`` it came from."""
    chars: list[str] = []
    origin: list[int] = []
    for i, ch in enumerate(text):
        if _is_invisible(ch):
            continue
        for out in unicodedata.normalize("NFKC", ch).translate(_TRANSLATE).casefold():
            if _is_invisible(out):
                continue
            chars.append(out)
            origin.append(i)
    return "".join(chars), origin


def contains_fence_tag(text: Any) -> bool:
    """True when ``text`` contains something :func:`defuse_fence_tags` would defuse."""
    s = text if isinstance(text, str) else str(text)
    if not _BRACKET_PREFILTER.search(s):
        return False
    projected, _ = _project(s)
    return _DEFAULT_RE.search(projected) is not None


def defuse_fence_tags(text: Any) -> str:
    """Return ``text`` with every fence tag defused (see module docstring).

    Non-``str`` input is rendered with ``str()`` first (the same rendering an
    f-string interpolation gives), so call sites can pass raw values. Text without
    a fence tag is returned unchanged."""
    s = text if isinstance(text, str) else str(text)
    if not _BRACKET_PREFILTER.search(s):
        return s
    projected, origin = _project(s)
    starts = sorted({origin[m.start()] for m in _DEFAULT_RE.finditer(projected)})
    if not starts:
        return s
    out: list[str] = []
    prev = 0
    for idx in starts:
        out.append(s[prev:idx])
        out.append(DEFUSED_OPEN)
        prev = idx + 1
    out.append(s[prev:])
    return "".join(out)
