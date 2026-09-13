"""Hebrew product-name generation — prompt plus the gate that keeps model
discourse out of products.name_he.

A read-only scan of production on 13/09/2026 found 12 broken name_he values out
of 749. Eleven of them were source='user', i.e. they came from this path: a bare
one-line prompt, max_tokens=200, and `claude_text(msg).strip()` written straight
to the DB with nothing in between. The model answered with *discourse* instead of
a *string* — "הנה תרגום קצר:\n\n**...**", "אופס, תיקון:", "אני מתנצל - תרגום
תקין:" — and the 200-token ceiling then cut the real translation off mid-word.

Two defences, because either alone is insufficient:
  1. The prompt now forbids preambles and alternatives outright.
  2. sanitize_name_he() salvages what it can and rejects what it cannot. A
     prompt is a request; only the gate is a guarantee.

A rejected value returns "" and is regenerated on the next backfill run.
"""

import re

PROMPT_RULES = (
    "תרגם לעברית קצרה ומובנת (עד 7 מילים, שמור את שם המותג, ללא מרכאות).\n"
    "החזר שורה אחת בלבד — רק השם המתורגם.\n"
    "ללא הקדמה, ללא הסבר, ללא חלופות, ללא תיקון עצמי, ללא סימוני Markdown.\n"
    "אל תשאיר תווים סיניים/יפניים/קוריאניים/קיריליים/ערביים.\n\n"
)

MAX_TOKENS = 300


def build_prompt(name: str) -> str:
    return PROMPT_RULES + name


# Decorative CJK leakage — stripped rather than rejected, since it sits beside a
# usable translation ("謎 An Elegant Puzzle: מערכות ניהול הנדסה").
_CJK_RE = re.compile(r"[　-〿぀-ヿ一-鿿＀-￯㄀-ㄯ]")

# Cyrillic / Arabic / Greek are a different failure: the model substituted a
# foreign token *for* a Hebrew word ("впитываемость", "Pептид", "ולامع"). There
# is no salvage — stripping them would silently delete meaning.
_FOREIGN_RE = re.compile(r"[Ѐ-ӿ؀-ۿͰ-Ͽ]")

_HEBREW_RE = re.compile(r"[֐-׿]")

# A token carrying both alphabets is a word the model half-translated
# ("לסeniores", "וג'niוס", "впотיעה"). A brand beside Hebrew is fine; glued
# together is not.
_MIXED_TOKEN_RE = re.compile(r"\S*(?:[֐-׿][A-Za-zЀ-ӿ]|[A-Za-zЀ-ӿ][֐-׿])\S*")

# ...except the one case where glued is correct Hebrew: a single-letter
# conjunction/preposition prefixed onto a Latin brand — "וKMix", "לNapoleon".
# The capital is what separates it from a half-translated word: "לסeniores"
# continues in lowercase and stays a defect. Stripped for the test only.
_HEB_PREFIX_LATIN_RE = re.compile(r"(?<![֐-׿])([ובלכמהש])(?=[A-Z])")

_LEAK_RE = re.compile(
    r"(אופס|אני מתנצל|סליחה|תיקון:|הנה התרגום|הנה תרגום|תרגום קצר|תרגום תקין"
    r"|או באופן|או באפשרות|או לחלופין|^או:|\bTranslation:|Here is|Here's)",
    re.M,
)

_MAX_LEN = 200


# Only a *matched* pair is a quote. A lone trailing apostrophe is the Hebrew
# geresh in "אינץ'" / "יח'" — stripping it silently corrupts the name.
_QUOTED_RE = re.compile(r'^(["“‘\'])(.*)(["”’\'])$', re.S)


def _clean_line(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^\*\*(.*)\*\*$", r"\1", text).strip()
    text = text.strip("*").strip()
    m = _QUOTED_RE.match(text)
    if m:
        text = m.group(2).strip()
    return text


def sanitize_name_he(raw: str) -> str:
    """Return a usable one-line Hebrew name, or "" if the reply cannot be trusted."""
    if not raw:
        return ""

    text = _CJK_RE.sub("", raw).strip()

    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    if not paragraphs:
        return ""

    # A paragraph ending in ':' is a lead-in to the real answer ("הנה תרגום קצר:",
    # "The Legend of Uh - תרגום:"), never the answer itself.
    paragraphs = [p for p in paragraphs if not p.rstrip().endswith(":")] or paragraphs

    # The model sometimes echoes the English source first and translates below it.
    chosen = paragraphs[0]
    if not _HEBREW_RE.search(chosen):
        for p in paragraphs[1:]:
            if _HEBREW_RE.search(p):
                chosen = p
                break

    result = _clean_line(chosen.splitlines()[0])
    if not result:
        return ""

    if _LEAK_RE.search(result):
        return ""
    if _FOREIGN_RE.search(result):
        return ""
    if _MIXED_TOKEN_RE.search(_HEB_PREFIX_LATIN_RE.sub("", result)):
        return ""
    if len(result) > _MAX_LEN:
        return ""

    return result
