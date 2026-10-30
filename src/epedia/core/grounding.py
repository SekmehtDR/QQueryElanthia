"""Is there anything in the wiki to answer this from? Decided in code, before the model is asked.

Measured on the evaluation set, the refusal set and ~120 real questions from the conversation log (2026-10-29):

- Vector distance does not tell answerable from unanswerable. "What does the spell Moonwraith Veil do?" (invented) is
  as close to its nearest chunk as "Who is Binu?" is to his page, because the question's ordinary words find ordinary
  passages. It is recorded for every turn, never used to decide.
- What does tell: the question names something that occurs nowhere in the wiki. A capitalised name that never occurs
  as a phrase ("Zarnak the Unbound", "Mistral Cove") marked all eleven invented names and no real question but one, which
  also named things that do exist; a word found in no page at all ("moonwraith", "pyrewing") marked nine of them.

So the gate declines only when every name the question gives is missing and the question asks little else, or when a
third or more of its content words are missing; anything less is answered as usual, with the missing names recorded on
the turn (the verifier then catches any name the model makes up).
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import asdict, dataclass, field

from ..retrieval.search import STOPWORDS
from ..retrieval.titles import STOP_NAMES, tokens
from .attribution import STOP

MISSING_SHARE = 1 / 3  # decline when at least this share of the question's content words is in no page at all
MAX_OTHER_WORDS = 2  # ...or when its names are all missing and it asks little else (invented questions had 0 to 2)

# Capitalised names in the question: runs of capitalised words, allowing "of the" inside ("Zarnak the Unbound").
_NAME = re.compile(r"(?<![\w'’-])[A-Z][\w'’-]*(?:\s+(?:of\s+the\s+|of\s+|the\s+)?[A-Z][\w'’-]*)*")
# Words that open a question; capitalised, they are not part of a name ("Who", "Tell", "Write").
_OPENERS = {"who", "what", "where", "how", "which", "why", "when", "is", "are", "was", "were", "do", "does", "did",
            "can", "could", "would", "should", "will", "tell", "write", "make", "imagine", "simulate", "roleplay",
            "explain", "describe", "list", "give", "show", "find", "i", "i'm", "im", "if", "and", "but", "so", "or",
            "hey", "hi", "hello", "please", "a", "an", "the"}


@dataclass
class Gate:
    level: str = "answer"  # "answer", or "none": nothing in the wiki to answer from, so it is declined in code
    missing_names: list[str] = field(default_factory=list)  # capitalised names the wiki never mentions
    missing_terms: list[str] = field(default_factory=list)  # content words in no page at all
    names: list[str] = field(default_factory=list)  # every capitalised name the question gives
    content: int = 0  # how many content words the question has

    def as_dict(self) -> dict:
        return asdict(self)


def _count(conn: sqlite3.Connection, table: str, phrase: str) -> int:
    q = '"' + re.sub(r"[^\w\s'’-]", " ", phrase).replace('"', "").strip() + '"'
    if q == '""':
        return 1
    try:
        return conn.execute(f"SELECT count(*) FROM (SELECT 1 FROM {table} WHERE {table} MATCH ? LIMIT 1)",
                            (q,)).fetchone()[0]
    except sqlite3.OperationalError:
        return 1  # a phrase FTS cannot parse is not evidence of absence


def in_wiki(conn: sqlite3.Connection, phrase: str) -> bool:
    """Does this word or phrase occur anywhere in the wiki: any page's text, title or redirect?"""
    return bool(_count(conn, "chunks_fts", phrase) or _count(conn, "titles_fts", phrase))


def names_in(question: str) -> list[str]:
    """Capitalised names in a question, without the words that open it ("Who is Zarnak the Unbound?" -> Zarnak the
    Unbound). The first word alone is never a name: questions start with a capital."""
    out: list[str] = []
    for m in _NAME.finditer(question):
        words = m.group(0).split()
        while words and words[0].lower().strip("'’") in _OPENERS:
            words = words[1:]
        if not words or (m.start() == 0 and len(words) == 1 and len(m.group(0).split()) == 1):
            continue
        name = " ".join(words).rstrip("'’")
        name = re.sub(r"['’]s$", "", name)  # "Tirost's message" names Tirost
        if name.lower() not in STOP_NAMES and name not in out:
            out.append(name)
    return out


def assess(conn: sqlite3.Connection, question: str) -> Gate:
    """What the wiki has on the names and words of `question` (the user's own words, not pasted text). With nothing
    indexed yet there is nothing to judge absence against, so the gate stands down."""
    gate = Gate(names=names_in(question))
    if conn.execute("SELECT 1 FROM chunks LIMIT 1").fetchone() is None:
        return gate
    gate.missing_names = [n for n in gate.names if not in_wiki(conn, n)]
    bases = (re.sub(r"['’](?:s|t|re|ll|ve|d|m)$", "", t) for t in tokens(question))  # "it's" -> it, "Tirost's" -> tirost
    content = [t for t in dict.fromkeys(bases)
               if t not in STOPWORDS and t not in STOP_NAMES and t not in STOP and len(t) > 2 and not t.isdigit()]
    gate.content = len(content)
    gate.missing_terms = [t for t in content if not in_wiki(conn, t)]
    # A missing name declines only when it is what the question is about: little else is asked ("Where is Mistral
    # Cove?", "How do I join the Shadowweaver guild?"), not "My character Jobrien asks: where do musk hogs live?".
    name_words = {t for n in gate.names for t in tokens(n)}
    others = [t for t in content if t not in name_words]
    all_names_missing = bool(gate.names) and len(gate.missing_names) == len(gate.names) and len(others) <= MAX_OTHER_WORDS
    many_words_missing = bool(content) and len(gate.missing_terms) / len(content) >= MISSING_SHARE
    if all_names_missing or many_words_missing:
        gate.level = "none"
    return gate


def decline(gate: Gate, persona: bool) -> str:
    """What to say when the wiki has nothing to answer from. Written by code, so it names exactly what was missing."""
    missing = gate.missing_names or gate.missing_terms
    what = " or ".join(f"“{m}”" for m in missing[:3]) or "that"
    if not persona:
        return f"The wiki has no page that mentions {what}."
    return (f"I have nothing in my notes on {what}, and I'd rather not guess. If it goes by another name or is spelled "
            "differently, try me again; or ask me about something else.")
