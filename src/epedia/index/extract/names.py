"""What a name means, where a page says so: `The svidaw sis (Rakash for "throwing beater")`, `Name means "White Fire" in
Gamgweth`, `the Khalaen, a Gamgweth word meaning "leaders"`.

Only these fixed phrasings count, and each must name a language, so an everyday "that means" is never a fact. Precision
beats reach here: when a sentence does not say what it is naming, nothing is stored. Role-play logs and discussion are
not read, because a character's guess at a word is not what the word means.
"""

from __future__ import annotations

import re
import sqlite3

from . import Fact, register_pages

DOMAIN = "name_meaning"

# How pages write a language -> the page about that language. Pages write several of these without having a page of that
# name ("Elven", "S'Kra"), so they cannot be read from titles; `epedia doctor` reports a language the wiki adds.
LANGUAGES = {
    "Asaran": "Asaran",
    "Demonic": "Demonic Language",
    "Dwarven": "Haakish",
    "Elothean": "Gerenshuge",
    "Elven": "Ilithic",
    "Elvish": "Ilithic",
    "Eth'ral'khh": "Eth'ral'khh",
    "Gamgweth": "Gamgweth",
    "Gerenshuge": "Gerenshuge",
    "Gor'Tog": "Toggish",
    "Gorbesh": "Gorbesh",
    "Haakish": "Haakish",
    "High Gamgweth": "Gamgweth",
    "Ilithic": "Ilithic",
    "Olvi": "Olvi (language)",
    "Olvio": "Olvi (language)",
    "Prydaenese": "Prydaenese",
    "Rakash": "Rakash (language)",
    "S'Kra": "S'Kra Mur (language)",
    "S'Kra Mur": "S'Kra Mur (language)",
    "Toggish": "Toggish",
}
_SPELLING = {s.casefold(): s for s in LANGUAGES}
_LANG = r"(?<![\w'])(?P<lang>(?i:" + "|".join(re.escape(s) for s in sorted(LANGUAGES, key=len, reverse=True)) + r"))(?![\w'])"
_QUOTED = r"""(?P<q>["'])(?P<meaning>[^\n]{2,60}?)(?P=q)(?!\w)"""

_FOR = re.compile(rf"(?P<named>name is )?{_LANG} for,? {_QUOTED}")  # X is Rakash for "..."; X (Rakash for "...")
_MEANS = re.compile(rf"(?:means?|meaning),? {_QUOTED},? in (?:the )?{_LANG}")  # X means "..." in Rakash
_WORD = re.compile(rf"{_LANG} (?:word|name|term) (?:that )?mean(?:s|ing),? {_QUOTED}")  # X, a Gamgweth word meaning "..."

_BOUNDARY = re.compile(r"""(?<=[a-z0-9)]{3}[.!?])["']?\s+|(?<=[.!?]["'])\s+|[;\n|]""")
_FILLER = re.compile(r"(?:,|\b(?:which|that|literally|actually|roughly|simply|also))\s*$", re.I)
_QUOTED_NAME = re.compile(r'"([^"\n]{2,40})"')
_THE_WORD = re.compile(r"^(?:the |this )?(?:word|name|term|phrase) (.{2,40})$", re.I)
# "Its name", "Name", "Dawan's name", "Avrenka, whose name", "spell name": the sentence is about the page (or its section)
_THE_PAGE = re.compile(r"(?:^|\s)(?:(?:its|his|her|their|whose|the|this|spell|last|first|family|given|[\w']+'s)\s+)*name$", re.I)
_BARE_DETERMINER = re.compile(r"^(?:the|its|his|her|their)?$", re.I)
_ARTICLE = re.compile(r"^(?:the|an|a)\s+", re.I)
_NOT_ONE_NAME = re.compile(r"\b(?:is|are|was|were|by|for|from|with|to|and|or|as|in|on|called|known)\b", re.I)
_CAPITALS = re.compile(r"((?:[A-Z][\w'-]*)(?: (?:[A-Z][\w'-]*|ai|of|va|the))*)$")
_NS = re.compile(r"^[A-Za-z]+:(?=\S)")
_SUFFIX = re.compile(r"\s*\([^)]*\)$")

_NOT_A_NAME = frozenset({"it", "its", "this", "that", "these", "those", "he", "she", "they", "which", "who", "what", "here",
                        "inscription", "motto", "phrase", "saying", "term", "word"})
# A post or a book mentions names in passing, and an item's title describes it: none is the thing a bare "name" refers to
_NOT_THE_NAMED_THING = re.compile(r"^(?:Post|Item|Weapon|Armor|Shield):|\(book\)$")
_LEAD_IN = re.compile(r"^(?:all|every|named|called|known as)\s+", re.I)
_NAME_AT_END = re.compile(r"\b(?:name|named|called) ([A-Z][\w'-]*(?: [A-Z][\w'-]*)*)$")
_PLAIN = {"\u2019": "'", "\u2018": "'", "\u201c": '"', "\u201d": '"', "\u00a0": " "}
_TYPOGRAPHY = re.compile("[" + "".join(_PLAIN) + "]")
_BULLETS = " -*\u2022\t"


def bare_name(title: str) -> str:
    """What a page is called, without its namespace or disambiguation: 'Asaren celpeze (2)' -> 'Asaren celpeze'."""
    return _SUFFIX.sub("", _NS.sub("", title))


def _subject(sentence: str) -> str | None:
    """Who the words before a meaning are naming: a name, "" for the page itself, None when they do not say."""
    s = sentence
    while (stripped := _FILLER.sub("", s).strip()) != s:
        s = stripped
    at_end = _NAME_AT_END.search(s)  # her "lucid" side with the name Dzive
    if at_end:
        return at_end[1]
    quoted = _QUOTED_NAME.findall(s)
    if quoted:
        return quoted[-1]
    if not s or s.lower() == "it" or _THE_PAGE.search(s):
        return ""
    word = _THE_WORD.match(s)
    if word:
        return word[1]
    name = _ARTICLE.sub("", s)
    if len(name.split()) <= 4 and not _NOT_ONE_NAME.search(name) and (name != s or name[0].isupper()):
        return name
    return None


def _for_subject(sentence: str, named: bool) -> str | None:
    """The same for `... Rakash for "..."`, which only states a meaning right after a name: `X is`, `X (`, or nothing."""
    s = sentence
    if s.endswith("("):
        s = s[:-1].strip()
        found = _subject(s)
        if found is None or len(s.split()) > 4:
            capitals = _CAPITALS.search(s)
            return capitals[1] if capitals else found
        return found
    if named and _BARE_DETERMINER.match(s):  # "The name is Elven for ..."
        return ""
    if not s:
        return ""
    if re.search(r"\b(?:is|was)$", s):
        return _subject(re.sub(r"\s*\b(?:is|was)$", "", s))
    return None  # "made by Rakash for ..." is not about a word


def _word_subject(sentence: str) -> str | None:
    """The same for `the Khalaen, a Gamgweth word meaning ...`: the name just before the apposition."""
    s = re.sub(r",?\s*\b(?:a|an|the)$", "", sentence).strip()
    capitals = _CAPITALS.search(s)
    if capitals and len(capitals[1].split()) <= 4:
        return capitals[1]
    return _subject(s)


def _clean(name: str) -> str | None:
    name = name.strip(" \"'.,:-*").rsplit(",", 1)[-1]  # "Additionally, all grusana kaft"; "Their surname, Heru'Armiarma"
    name = _LEAD_IN.sub("", _ARTICLE.sub("", name.strip())).strip()
    if len(name) < 3 or name.casefold() in _NOT_A_NAME:
        return None
    return name[0].upper() + name[1:]


def read_meanings(chunks: list[tuple[str, str]], title: str) -> list[Fact]:
    """(entity, language as the wiki writes it, "means", None, the meaning, section) for each stated meaning on a page."""
    bare = bare_name(title)
    found: list[tuple[str, bool, str, str, str]] = []  # subject, opens its section, language, meaning, section
    for section, raw in chunks:
        text = _TYPOGRAPHY.sub(lambda m: _PLAIN[m[0]], raw)
        for rx in (_FOR, _MEANS, _WORD):
            for m in rx.finditer(text):
                parts = _BOUNDARY.split(text[:m.start()])
                sentence = parts[-1].strip(_BULLETS)
                if sentence.rstrip(" (").casefold().endswith(bare.casefold()):  # Cashek gand Aldam (Gamgweth for ...)
                    subject = bare
                elif rx is _FOR:
                    subject = _for_subject(sentence, bool(m["named"]))
                elif rx is _WORD:
                    subject = _word_subject(sentence)
                else:
                    subject = _subject(sentence)
                if subject is None:
                    if bare.casefold() not in sentence.casefold():  # "Ilkas Veridun, Chui'lana or Ilithic for ..."
                        continue
                    subject = ""
                found.append((subject, len(parts) == 1, _SPELLING[m["lang"].casefold()], m["meaning"], section))
    # A bare "Name means ..." opening several sections lists several things, named by their headings (Grazhir's Legacy >
    # Aliaes, > Mintais). Opening just one, the heading is only a heading (Dawan > Of Note): it means the page.
    listing = len({f[4] for f in found if f[0] == "" and f[1] and f[4]}) > 1
    out: dict[tuple[str, str], Fact] = {}
    for subject, opens, lang, meaning, section in found:
        if subject == "":
            if _NOT_THE_NAMED_THING.search(title):
                continue
            subject = section.split(">")[-1].strip() if listing and opens else bare
        entity = _clean(subject)
        meaning = meaning.replace('" or "', " or ").strip(" .,;:!?")
        if entity and len(meaning) >= 2 and '"' not in meaning:
            out.setdefault((entity.casefold(), lang), (entity, lang, "means", None, meaning, section))
    return list(out.values())


def _phrase(words: str) -> str:
    return '"' + re.sub(r"[^\w]+", " ", words) + '"'


def candidate_pages(conn: sqlite3.Connection) -> list[str]:
    """Pages using one of the phrasings, by one full-text query, plus those that had a meaning (it may have been edited out)."""
    langs = " OR ".join(_phrase(s) for s in LANGUAGES)
    fors = " OR ".join(_phrase(s + " for") for s in LANGUAGES)
    rows = conn.execute(
        "SELECT DISTINCT p.title FROM chunks_fts f JOIN chunks c ON c.chunk_id=f.rowid JOIN pages p ON p.page_id=c.page_id "
        "WHERE chunks_fts MATCH ? AND p.is_redirect=0 AND p.is_log=0 AND coalesce(p.page_kind, '') NOT IN "
        "('log','script','talk','user','file','site','soft_redirect','category','disambig','meta')",
        (f"{fors} OR (text:mean AND ({langs}))",)).fetchall()
    had = conn.execute("SELECT DISTINCT p.title FROM facts f JOIN pages p ON p.page_id=f.page_id WHERE f.domain=?",
                       (DOMAIN,)).fetchall()
    return sorted({r[0] for r in rows} | {r[0] for r in had})


register_pages(DOMAIN, candidate_pages)(read_meanings)
