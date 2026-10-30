"""Check a written answer against the passages it was written from. Pure code, no model, a few milliseconds.

What the real conversation log showed the 8B model doing, and what each check catches:

- citing a passage for a line that passage does not say (`check_citations`: the line's words and every number in it must
  be found in the passages it cites; otherwise its `[n]` becomes `[n?]`);
- naming creatures, people and places that are in none of the passages ("Shadow Serpents and Moonwraiths" for a Moon
  Mage's hunting) (`unknown_entities`);
- narrating actions it cannot take ("Let me take a closer look at that edit"), then inventing what it "found"
  (`fake_actions`): everything the model will ever see is already in its prompt.

The thresholds are calibrated on stored answers (`epedia eval --full` reports them per run).
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

from ..llm import prompts
from .attribution import (
    CITED,
    MIN_CONTENT_TOKENS,
    OFFER_LINE,
    STOP,
    Evidence,
    _bag,
    _split_units,
    _stem,
    unit_words,
)

MODEL_CITE = re.compile(r"\[(\d+)\]")  # a citation the model wrote; "[n*]" (matched by code) and "[n?]" are not
SUPPORT_COVERAGE = 0.35  # share of a cited line's weighted words its passages must contain (paraphrase is allowed)
RARE_SHARED = 2  # ...or at least this many words it shares with them that few passages use
UNCITED_FACTUAL_MIN = 3  # the uncited ratio says nothing about an answer with fewer citable lines than this

# A promise to go and look something up, or a claim to have done so. "Let me know", "let me explain" are not.
FAKE_ACTION = re.compile(
    r"\b(?:let me|i'll|i will|i(?:'d| would) (?:have|need) to|i'm going to|i am going to|allow me to|give me a (?:moment|second|minute)(?: to)?|"
    r"(?:maybe |perhaps )?i (?:should|need to|have to|must|ought to|could)|i'll need to|i'm (?:now )?(?:going|about) to)"
    r"\s+(?:have to\s+|need to\s+|try to\s+|be sure to\s+)?(?:just\s+|quickly\s+|now\s+|first\s+|go\s+(?:and\s+)?)?"
    r"(?:take an?(?: closer| quick| deeper| second| another)? look|have a(?: closer| quick)? look|check|look(?: at| into| up| through| over| again| back)"
    r"|search|dig|pull up|consult|go (?:through|over|back)|review|flip|re-?read|see what|find out|verify|double[- ]check|scan|"
    r"browse|open|fetch|confirm)\b"
    r"|\bi(?:'ve| have)? (?:just )?(?:checked|looked (?:at|into|through|up)|reviewed|searched|dug (?:into|through)|consulted|"
    r"pulled up|browsed)\b",
    re.I,
)

# Capitalised words that need no passage behind them: the persona's own world and framing, calendar words, markers.
_CALENDAR = ("January February March April May June July August September October November December Monday Tuesday "
             "Wednesday Thursday Friday Saturday Sunday Jan Feb Mar Apr Jun Jul Aug Sep Sept Oct Nov Dec Mon Tue Wed "
             "Thu Fri Sat Sun")
_FRAMING = "OOC DR AV GM GMs Fennick Halfling Bard Elanthia Elanthian Elanthipedia DragonRealms Simutronics Play Net"
_CAP_WORD = re.compile(r"[A-Z][A-Za-z'’]*(?:-[A-Za-z'’]+)*")
ALLOWED = frozenset(  # ALL-CAPS words in the prompt are commands (READ CATALOG), not names
    _stem(w.casefold().strip("'’")) for w in _CAP_WORD.findall(
        " ".join((prompts.PERSONA_BLOCK, prompts.META_BLURB, _CALENDAR, _FRAMING))) if not w.isupper() or len(w) <= 3)

_SPAN = re.compile(r"(?<![\w'’-])[A-Z][a-z][A-Za-z'’]*(?:-[A-Za-z'’]+)*(?:\s+[A-Z][a-z][A-Za-z'’]*(?:-[A-Za-z'’]+)*)*")
_SENTENCE_START = re.compile(r"(?:(?:\A|[.!?:;]\s|\n)\s*(?:[-*•>]|\d+[.)])?\s*|\|\s*|\(\s*|[\"“]\s*|[–—]\s*)\Z")
_MARKUP = re.compile(r"`[^`]*`|\*\*|__|(?<!\w)[*_](?!\s)|(?<=\w)[*_](?!\w)|^#+\s.*$", re.M)
_WORDS = re.compile(r"[a-z0-9][a-z0-9'’-]*")


def _fold(text: str) -> list[str]:
    toks = (re.sub(r"['’]s$", "", t.strip("'’-")) for t in _WORDS.findall(text.casefold()))
    return [_stem(t) for t in toks if t]


@dataclass
class Verification:
    text: str  # the answer with unsupported citations marked [n?]
    factual_units: int = 0  # lines long enough to state a fact
    cited_units: int = 0
    uncited: int = 0  # factual lines with no tag at all
    unsupported: list[str] = field(default_factory=list)  # lines whose citations the passages do not bear out
    numbers_bad: int = 0  # ...of which: a number the cited passages do not contain
    unknown_entities: list[str] = field(default_factory=list)
    fake_actions: list[str] = field(default_factory=list)

    @property
    def ratio_uncited(self) -> float:
        return self.uncited / self.factual_units if self.factual_units >= UNCITED_FACTUAL_MIN else 0.0

    @property
    def fabricated(self) -> bool:
        """Something the passages cannot account for: a name none of them has, or a pretended lookup."""
        return bool(self.unknown_entities or self.fake_actions)

    def as_dict(self) -> dict:
        d = asdict(self)
        d.pop("text")
        d["ratio_uncited"] = round(self.ratio_uncited, 3)
        return d


def check_citations(answer: str, ev: Evidence, allowed_numbers: set[str] = frozenset()) -> tuple[str, list[str], int]:
    """(answer with unsupported model citations rewritten [n?], the unsupported lines, how many failed on a number)."""
    edits: list[tuple[int, int, str]] = []
    unsupported: list[str] = []
    numbers_bad = 0
    for start, end, unit in _split_units(answer):
        nums = [int(n) for n in MODEL_CITE.findall(unit)]
        if not nums:
            continue
        words = unit_words(unit)
        if len(words) < MIN_CONTENT_TOKENS:
            continue  # "Here's what I found [1]": nothing to check
        valid = [n for n in nums if 1 <= n <= ev.n]
        bag: set[str] = set().union(*(ev.bags[n - 1] for n in valid)) if valid else set()
        numbers_ok = ev.numbers_ok(words, bag, allowed_numbers)
        rare = sum(1 for w in words & bag if ev.df.get(w, 0) <= max(1, ev.n // 4))
        supported = bool(valid) and numbers_ok and (ev.coverage(words, bag) >= SUPPORT_COVERAGE or rare >= RARE_SHARED)
        if supported and len(valid) == len(nums):
            continue
        if not supported:
            unsupported.append(unit.strip())
            numbers_bad += bool(valid) and not numbers_ok
            marked = MODEL_CITE.sub(lambda m: f"[{m.group(1)}?]", unit)
        else:  # the line is fine, one of its numbers points at no passage: mark just that one
            marked = MODEL_CITE.sub(lambda m: m.group(0) if 1 <= int(m.group(1)) <= ev.n else f"[{m.group(1)}?]", unit)
        edits.append((start, end, marked))
    out = answer
    for start, end, marked in sorted(edits, reverse=True):
        out = out[:start] + marked + out[end:]
    return out, unsupported, numbers_bad


def unknown_entities(answer: str, sources: list[str], question: str = "", message: str = "",
                     context: str = "") -> list[str]:
    """Capitalised names in the answer that appear in none of the passages, their titles, the question, the message or
    `context` (what the user said earlier in the conversation; never the model's own earlier answers).

    A name is unknown when any of its words is in none of them. (Requiring the words of a name to stand together as
    well flagged "Human Necromancer" from an infobox listing race and guild on separate lines, and caught nothing more
    in the stored answers.) A single capitalised word at the start of a sentence is ordinary English unless the answer
    also capitalises it mid-sentence."""
    known_text = " \n ".join([*sources, question, message, context])
    vocab = set(_fold(known_text)) | ALLOWED
    text = _MARKUP.sub(" ", CITED.sub(" ", re.sub(r"\[\d+\?\]", " ", answer)))
    mid_caps: set[str] = set()
    spans: list[tuple[str, bool]] = []
    for m in _SPAN.finditer(text):
        at_start = bool(_SENTENCE_START.search(text[max(0, m.start() - 12):m.start()]
                                               if m.start() > 12 else text[:m.start()]))
        spans.append((m.group(0), at_start))
        if not at_start:
            mid_caps.update(_fold(m.group(0))[:1])
    out: list[str] = []
    for span, at_start in spans:
        words = span.split()
        if at_start and len(words) > 1 and (words[0].casefold() in STOP or _fold(words[0])[:1] != _fold(words[0])
                                            or _fold(words[0])[0] not in vocab):
            words, at_start = words[1:], False  # "Then Riverhaven", "Read The Guide": the opener is ordinary English
        while len(words) > 1 and words[0].casefold() in STOP:
            words, at_start = words[1:], False  # "The No-Nonsense Guide", "Through Idon's Theft"
        span = " ".join(words)
        toks = _fold(span)
        if at_start and len(toks) == 1 and toks[0] not in mid_caps:
            continue  # "Shadows fall..." at a sentence start is just a word
        if not toks or (len(toks) == 1 and toks[0] in STOP):
            continue
        toks = [t for t in toks if t not in ALLOWED or len(toks) > 1]
        if not toks:
            continue
        if any(t not in vocab and t not in STOP for t in toks) and span not in out:
            out.append(span)
    return out


def fake_actions(answer: str) -> list[str]:
    """Lines where the model says it will look something up (or has), which it cannot do."""
    return [unit.strip() for _, _, unit in _split_units(answer) if FAKE_ACTION.search(unit)]


def strip_fake_actions(answer: str) -> str:
    """The answer without the lines that pretend to look something up. Such a line carries no fact (a line that cites a
    passage is kept: it says something the passage backs)."""
    edits: list[tuple[int, int, str]] = []
    for s, e, unit in _split_units(answer):
        m = FAKE_ACTION.search(unit)
        if not m or re.search(r"\[\d+[*?]?\]", unit):
            continue
        # "Let me re-read the passages: the sword weighs 10 stones." keeps what follows the pretence
        rest = re.split(r"[:;,–—]\s+|\s+and\s+(?=the\b|it\b|they\b|he\b|she\b)", unit[m.end():], maxsplit=1)
        tail = rest[1].strip() if len(rest) > 1 else ""
        keep = tail and len(unit_words(tail)) >= MIN_CONTENT_TOKENS and not FAKE_ACTION.search(tail)
        edits.append((s, e, unit[:len(unit) - len(unit.lstrip())] + tail[:1].upper() + tail[1:] if keep else ""))
    out = answer
    for start, end, text in sorted(edits, reverse=True):
        out = out[:start] + text + out[end:]
    out = re.sub(r"[ \t]{2,}", " ", out)
    out = re.sub(r"^[ \t]*(?:[-*•]|\d+[.)])?[ \t]*$", "", out, flags=re.M)  # a list item left empty
    return re.sub(r"\n{3,}", "\n\n", out).strip()


# One closing note, written by code, for what a reader most needs to know about how well the answer is backed. Measured
# on the stored answers (2026-10-29): a note on every [n?] or every half-uncited answer would sit under two answers in
# three; the marks themselves explain single lines, so a note is kept for names, figures and answers that rest on nothing.
UNCITED_NOTE_SHARE = 0.8
HONESTY_NOTES = {
    "names": ("One caution: I can't find {names} anywhere in the pages I had for this, so don't take my word on {them}.",
              "Note: {names} {do} not appear in the pages this answer was written from; treat {them} as unverified."),
    "numbers": ("One caution: a figure marked with a ? doesn't match the page it cites, so trust the page's own number.",
                "Note: a figure marked [n?] does not match its cited page; check the page."),
    "uncited": ("One caution: most of this isn't tied to a single page in my notes, so check the pages before relying on it.",
                "Note: most of this answer could not be tied to a cited page; check the pages before relying on it."),
}


def honesty_note(v: Verification, persona: bool = True, uncited: bool = True) -> str:
    """The note for the most important problem left in an answer, or ''. `uncited` False leaves out the note about
    uncited lines (small talk about Fennick himself cites nothing)."""
    style = 0 if persona else 1
    if v.unknown_entities:
        names = [f"“{n}”" for n in v.unknown_entities[:3]]
        shown = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " or " + names[-1]
        one = len(names) == 1
        return (HONESTY_NOTES["names"][style].replace("{names}", shown).replace("{them}", "it" if one else "them")
                .replace("{do}", "does" if one else "do"))
    if v.numbers_bad:
        return HONESTY_NOTES["numbers"][style]
    if uncited and v.factual_units >= UNCITED_FACTUAL_MIN and v.ratio_uncited >= UNCITED_NOTE_SHARE:
        return HONESTY_NOTES["uncited"][style]
    return ""


def run(answer: str, passages: list[dict], question: str = "", message: str = "", context: str = "") -> Verification:
    """All checks at once. `passages` are the numbered passages (dicts with title and text), in prompt order;
    `context` is what the user said earlier in the conversation."""
    texts = [p["text"] for p in passages]
    ev = Evidence(texts)
    allowed_numbers = {w for w in _bag(f"{question} {message} {context}") if any(c.isdigit() for c in w)}
    text, unsupported, numbers_bad = check_citations(answer, ev, allowed_numbers)
    factual = cited = uncited = 0
    for _, _, unit in _split_units(text):
        if OFFER_LINE.match(unit.strip()) or len(unit_words(unit)) < MIN_CONTENT_TOKENS:
            continue
        factual += 1
        if re.search(r"\[\d+[*?]?\]", unit):
            cited += 1
        else:
            uncited += 1
    sources = [f"{p.get('title', '')}\n{p.get('section', '')}\n{p['text']}" for p in passages]
    return Verification(text, factual, cited, uncited, unsupported, numbers_bad,
                        unknown_entities(answer, sources, question, message, context), fake_actions(answer))
