"""Attach source tags to answer lines the model left uncited.

The model is asked to end every factual sentence, bullet and table row with its `[n]`, but a small local model does
this unreliably. After the answer is written, each uncited line is compared with the numbered passages by weighted
word overlap (numbers and rare words count most). A line gets a tag only on a clear match, and the tag is written
`[n*]` so the page can show it was matched automatically rather than cited by the model.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

from ..llm.prompts import clean_passage_text

CITED = re.compile(r"\[\d+\*?\]")
_WORD = re.compile(r"[a-z0-9][a-z0-9'’-]*")
# A sentence ends at . ! ? plus any citation tags the model put after the period ("... shop. [1] Next one"),
# provided a new sentence follows. Keeping those tags with the sentence is what stops it looking uncited.
_SENTENCE_END = re.compile(r"[.!?](?:\s*\[\d+\*?\])*(?=\s+[A-Z0-9(\"'*])")
OFFER_LINE = re.compile(  # the closing offer (current wording first, then the earlier wording)
    r"^(Want to hear about |If you're curious, I can |I can also tell you about |"
    r"If you like, I can|The chronicle has more on|There's a thread worth pulling on)"
)

STOP = frozenset("""
a about after all also an and any are as at be been being both but by can could did do does each for from had has
have he her his how if in into is it its just may more most much no not of on one only or other our out over she so
some such than that the their them then there these they this those through to too under up very was we were what
when where which while who will with would you your i me my been also per via
""".split())

MIN_CONTENT_TOKENS = 3  # shorter lines are framing ("Here is what I found") and are left alone
MIN_COVERAGE = 0.5  # share of the line's weighted content words that the passage must contain
CLEAR_MARGIN = 0.12  # ...and it must beat the runner-up by this much, unless it is a near-total match
NEAR_TOTAL = 0.8


@dataclass
class Attribution:
    text: str
    added: int  # lines that received an automatic tag
    uncited: int  # factual-looking lines left without any tag


_THOUSANDS = re.compile(r"(?<=\d),(?=\d{3}\b)")
_LIST_MARK = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+")


_NUM_JOIN = re.compile(r"(?<=\d)[-–—/:](?=\d)")  # "130-230", "2027-01-02", "12/18": each number counts on its own


def _content_tokens(text: str) -> list[str]:
    text = _NUM_JOIN.sub(" ", _THOUSANDS.sub("", text.lower()))
    toks = [re.sub(r"['’]s$", "", t.strip("-'’")) for t in _WORD.findall(text)]
    toks = [str(int(t)) if t.isdigit() else t for t in toks]  # "02" in a date is the 2 in "January 2"
    return [t for t in toks if t and t not in STOP and (len(t) > 2 or t.isdigit())]


def _stem(t: str) -> str:
    """Plural folding only ('stones' = 'stone', 'cities' = 'city'); verbs and nouns are left alone, since the
    numbers and rare names in a line carry the match."""
    if len(t) > 4 and t.endswith("ies"):
        return t[:-3] + "y"
    if len(t) > 4 and t.endswith(("xes", "zes", "ches", "shes")):
        return t[:-2]
    if len(t) > 3 and t.endswith("s") and not t.endswith(("ss", "us", "is")):
        return t[:-1]
    return t


def _bag(text: str) -> set[str]:
    return {_stem(t) for t in _content_tokens(text)}


def unit_words(unit: str) -> set[str]:
    """What a line of the answer claims, as a bag: without its citation tags or its list number ("2. Smelt the
    ingot" claims nothing about the number 2)."""
    return _bag(_LIST_MARK.sub("", CITED.sub(" ", re.sub(r"\[\d+\?\]", " ", unit))))


def _split_units(answer: str) -> list[tuple[int, int, str]]:
    """(start, end, text) for each citable unit: sentences within paragraphs, list items, table rows."""
    units: list[tuple[int, int, str]] = []
    pos = 0
    in_code = False
    for line in answer.split("\n"):
        start, end = pos, pos + len(line)
        pos = end + 1
        stripped = line.strip()
        if stripped.startswith("```"):
            in_code = not in_code
            continue
        if in_code or not stripped or stripped.startswith("#") or re.fullmatch(r"\|?[\s:|-]+\|?", stripped):
            continue  # code, blank lines, headings, table separator rows
        if stripped.startswith("|") or re.match(r"^\s*([-*•]|\d+[.)])\s+", line):
            units.append((start, end, line))  # a whole table row / list item is one unit
            continue
        last = 0
        for m in _SENTENCE_END.finditer(line):
            units.append((start + last, start + m.end(), line[last:m.end()]))
            last = m.end() + len(line[m.end():]) - len(line[m.end():].lstrip())  # skip the space between sentences
        units.append((start + last, end, line[last:]))
    return units


def _insert_point(unit: str) -> int:
    """Where in `unit` a tag goes: inside the last cell of a table row, else before the final period."""
    body = unit.rstrip()
    if body.endswith("|"):
        return len(body[:-1].rstrip())
    if body and body[-1] in ".!?":
        return len(body) - 1
    return len(body)


def _is_number(w: str) -> bool:
    return any(c.isdigit() for c in w)


class Evidence:
    """The numbered passages as bags of content words, with weights that favour rare words and numbers. Shared by
    attribution (tagging uncited lines) and verification (checking the lines the model did cite)."""

    def __init__(self, passage_texts: list[str]):
        self.bags = [_bag(clean_passage_text(t)) for t in passage_texts]
        self.n = len(self.bags)
        self.df: dict[str, int] = {}
        for b in self.bags:
            for w in b:
                self.df[w] = self.df.get(w, 0) + 1

    def weight(self, w: str) -> float:
        base = math.log(1 + self.n / self.df.get(w, 0.5))  # unseen words get the highest weight but can never match
        return base * (2.0 if _is_number(w) else 1.0)

    def coverage(self, words: set[str], bag: set[str]) -> float:
        """Share of the line's weighted content words that `bag` contains."""
        total = sum(self.weight(w) for w in words)
        return sum(self.weight(w) for w in words & bag) / total if total else 0.0

    @staticmethod
    def numbers_ok(words: set[str], bag: set[str], allowed: set[str] = frozenset()) -> bool:
        """Veto: a number the passage does not contain means the line says something the passage does not, however
        much else they share ("weighs 40 stones" vs a passage saying 35). `allowed` are numbers the user gave."""
        return all(w in bag or w in allowed for w in words if _is_number(w))


def attribute(answer: str, passage_texts: list[str]) -> Attribution:
    """Return the answer with `[n*]` added to uncited lines that clearly come from passage n (1-based)."""
    if not answer.strip() or not passage_texts:
        return Attribution(answer, 0, 0)
    ev = Evidence(passage_texts)
    bags = ev.bags

    edits: list[tuple[int, str]] = []
    uncited = 0
    for start, end, unit in _split_units(answer):
        if CITED.search(unit) or OFFER_LINE.match(unit.strip()):
            continue
        words = unit_words(unit)
        if len(words) < MIN_CONTENT_TOKENS:
            continue
        scores = sorted(  # ties go to the earlier (higher-ranked) passage
            ((ev.coverage(words, b), i) for i, b in enumerate(bags, 1)),
            key=lambda s: (-s[0], s[1]),
        )
        best, idx = scores[0]
        runner = scores[1][0] if len(scores) > 1 else 0.0
        overlap = len(words & bags[idx - 1])
        numbers_ok = ev.numbers_ok(words, bags[idx - 1])
        if (best >= MIN_COVERAGE and overlap >= 2 and numbers_ok
                and (best - runner >= CLEAR_MARGIN or best >= NEAR_TOTAL)):
            edits.append((start + _insert_point(unit), f" [{idx}*]"))
        else:
            uncited += 1
    out = answer
    for pos, tag in sorted(edits, reverse=True):
        out = out[:pos] + tag + out[pos:]
    return Attribution(out, len(edits), uncited)
