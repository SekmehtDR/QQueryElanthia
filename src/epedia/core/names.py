"""What a name means, from the facts `index/extract/names.py` read: a passage for the answer, written by code, so the
meaning and the page that states it are exact."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass

from ..index.extract.names import DOMAIN, LANGUAGES, bare_name
from ..util import page_url

_ASKS = re.compile(r"\b(?:means?|meaning|translat\w+|etymolog\w+|stands? for|name (?:come|comes|came) from|origin of the name)\b",
                   re.I)
_ONLY_MEANING = re.compile(r"^what (?:does|do|did) (?:the (?:word|name|term) )?(?P<what>.+?) mean\s*\??$|"
                           r"^what(?:'s|’s| is) the meaning of (?:the (?:word|name|term) )?(?P<of>.+?)\s*\??$", re.I)
_DETERMINER = re.compile(r"^(?:a|an|the)\s+", re.I)
MAX_MEANINGS = 3
_ROW = "SELECT f.entity, f.grp, f.value_text, f.section, p.title, p.ns FROM facts f JOIN pages p ON p.page_id=f.page_id "


@dataclass(frozen=True)
class Meaning:
    entity: str
    language: str
    meaning: str
    title: str  # the page that says so
    section: str


def _same(meaning: str) -> str:
    """'throwing beater' and 'throw(ing) beat(er)' are one meaning, written twice."""
    return re.sub(r"[\W_]+", "", meaning).casefold()


def meanings(conn: sqlite3.Connection, titles: Iterable[str]) -> list[Meaning]:
    """Every distinct stated meaning of what these pages are called, preferring the page's own wording, then articles
    over item and post pages. A weapon's long title carries the word it is ("...ironwood svidaw sis")."""
    out: list[Meaning] = []
    seen: set[tuple[str, str]] = set()

    def take(rows) -> None:
        for r in rows:
            key = (r[0].casefold(), _same(r[2]))
            if key not in seen:
                seen.add(key)
                out.append(Meaning(r[0], r[1], r[2], r[4], r[3] or ""))

    for title in titles:
        take(conn.execute(_ROW + "WHERE f.domain=? AND f.entity=? COLLATE NOCASE ORDER BY p.title=? DESC, p.ns, p.title",
                          (DOMAIN, bare_name(title), title)).fetchall())
        low = bare_name(title).casefold()
        take(r for r in conn.execute(_ROW + "WHERE f.domain=? AND p.title=?", (DOMAIN, title)).fetchall()
             if re.search(r"(?<!\w)" + re.escape(r[0].casefold()) + r"(?!\w)", low))
    return out


def named_in(conn: sqlite3.Connection, question: str) -> list[str]:
    """Words the wiki explains that the question uses, longest first, whether or not they have a page of their own
    (nehdalata, uaro's'sugi)."""
    low = question.replace("’", "'").casefold()
    found = [r[0] for r in conn.execute(
        "SELECT DISTINCT entity FROM facts WHERE domain=? AND length(entity) >= 4 AND instr(?, lower(entity)) > 0", (DOMAIN, low))]
    return sorted((e for e in found if re.search(r"(?<!\w)" + re.escape(e.casefold()) + r"(?!\w)", low)), key=len, reverse=True)


def asks_meaning(question: str) -> bool:
    return bool(_ASKS.search(question))


def passages(conn: sqlite3.Connection, question: str, titles: Iterable[str], base_url: str) -> list[dict]:
    """A short passage per stated meaning, for a question asking what something means; a question about a weapon's weight
    is not told what its name means."""
    if not asks_meaning(question):
        return []
    words = named_in(conn, question)
    found = meanings(conn, words) if words else meanings(conn, titles)
    return [passage(m, base_url) for m in found[:MAX_MEANINGS]]


def answer(conn: sqlite3.Connection, question: str, base_url: str) -> list[dict]:
    """A question that only asks what a word means is answered by code, so the model cannot pad a correct meaning with
    what it imagines the word says about the thing. Empty for anything else, which goes to the model as usual."""
    asked = _ONLY_MEANING.match(question.strip())
    if not asked:
        return []
    word = _DETERMINER.sub("", (asked["what"] or asked["of"]).strip(" \"'"))
    found = meanings(conn, [word])[:MAX_MEANINGS]
    if not found:
        return []
    out = [passage(m, base_url) for m in found]
    out[0]["answer"] = " ".join(f'"{m.entity}" is {m.language} for "{m.meaning}" [{n}].' for n, m in enumerate(found, 1))
    return out


def passage(m: Meaning, base_url: str) -> dict:
    about = LANGUAGES.get(m.language)
    text = f'"{m.entity}" is {m.language} for "{m.meaning}".' + (f" (The language: {about}.)" if about else "")
    return {"title": m.title, "section": "What the name means", "url": page_url(base_url, m.title, None), "text": text,
            "chunk_id": None}
