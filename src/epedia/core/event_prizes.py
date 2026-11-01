"""'What's being raffled at this year's Hollow Eve, and who won?' answered in code from the festival's own table.

Items are grouped, so ten people winning the same thing is one line. A raffle question reads only raffle pages and an auction question
only auction pages. The rules were set with the owner after a real thumbs-down, where the model said the prizes were "not listed" twice
because the search found the page's editing instructions instead of its tables, then invented a raffle from the blank template.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass

from ..index.extract.event_prizes import AUCTION, DOMAIN, RAFFLE, kind_of
from ..retrieval.resolve import explicit_pages
from ..util import page_url

MAX_ITEMS = 25  # past this a chat answer is no use: the most won lead, and the page has the rest
EARLIER_QUESTIONS = 2  # how far back "who won these?" looks for what "these" were

_RAFFLE = re.compile(r"\braffl\w*", re.I)
_AUCTION = re.compile(r"\bauction\w*", re.I)
_WANTS = re.compile(r"\b(items?|prizes?|won|wins?|winners?|winnings|bought|buyers?|sold|outcomes?|results?|lists?|show|raffled|auctioned|"
                    r"what(?:'s| is| was| were| are)|who)\b", re.I)
_HOW = re.compile(r"\b(how (?:do|does|can|to|much|many)|when|where|why|cost|tickets?|timer|rules?)\b", re.I)
_NUMBERED = re.compile(r"\braffle\s*#?\s*(\d{1,4})\b", re.I)
_YEAR = re.compile(r"(?<![\w#])(\d{3})(?!\w)")
_PERSON = re.compile(r"\bwhat (?:did|has|have) ([A-Za-z'-]+) (?:win|won|buy|bought|get|got)\b|\bdid ([A-Za-z'-]+) (?:win|buy|get)\b|"
                     r"\b([A-Za-z'-]+?)(?:'s)? (?:wins|winnings)\b", re.I)
_NOT_A_NAME = frozenset("i you he she we they it anyone someone people everyone who the my our his her their".split())
_WHO_WON = re.compile(r"\bwho (?:won|bought|got|has|ended up with)\s+(?:the |a |an )?(.+?)\s*(?:\?|$)", re.I)
_NOT_AN_ITEM = re.compile(r"^(?:these|those|them|it|each|everything|all|any|what|this|that|things?|stuff)\b|^(?:items?|prizes?|raffles?|auctions?)\b", re.I)
_TRAILING = re.compile(r"\s+(?:at|in|from|during|on)\s+.*$|\s+this year.*$|\s+last year.*$", re.I)
_FOLLOW_UP = re.compile(r"\bwho (?:won|bought|got)\b|\bwinners?\b|\bbuyers?\b", re.I)

# What players call a festival, beyond its name. Upper case is exact: "he" is usually only a pronoun.
_CALLED = {"Hollow Eve Festival": ("hollow eve",), "Guildfest": ("guild fest",), "Drathrok's Duskruin": ("duskruin",),
           "Estate Holder Mini-Fest": ("estate holder", "mini-fest", "minifest", "mini fest"), "Taisidon Safari": ("taisidon", "safari"),
           "Bacon Man's Birthday Bash": ("bacon man",), "Romancing the Seas": ("romancing",)}
_CALLED_EXACT = {"Hollow Eve Festival": ("HE",), "Guildfest": ("GF",), "Drathrok's Duskruin": ("DF",), "Estate Holder Mini-Fest": ("EH",)}


@dataclass(frozen=True)
class Page:
    title: str
    event: str
    year: int
    kind: str


@dataclass(frozen=True)
class Prize:
    page: str
    item: str
    who: str
    notes: str
    price: str
    where: str


def _pages(conn: sqlite3.Connection) -> list[Page]:
    out = []
    for r in conn.execute("SELECT DISTINCT p.title FROM pages p JOIN enriched e ON e.page_id=p.page_id WHERE e.domain=?", (DOMAIN,)):
        m = re.match(r"(.*?)\s*(\d+)?/(?:Raffles|Auction)$", r[0])
        if m:
            out.append(Page(r[0], m.group(1), int(m.group(2) or 0), kind_of(r[0])))
    return out


def _prizes(conn: sqlite3.Connection, title: str) -> list[Prize]:
    out = []
    for r in conn.execute("SELECT f.entity, f.value_text, f.section FROM facts f JOIN pages p ON p.page_id=f.page_id "
                          "WHERE p.title=? AND f.domain=? ORDER BY f.value_num", (title, DOMAIN)):
        v = json.loads(r[1])
        out.append(Prize(title, r[0], v.get("who") or "", v.get("notes") or "", v.get("price") or "", r[2] or ""))
    return out


def _person(text: str) -> str:
    """Whose winnings are being asked about, if anyone's."""
    m = _PERSON.search(text)
    name = next((g for g in (m.groups() if m else ()) if g), "")
    return "" if name.casefold() in _NOT_A_NAME else name


def _kinds(text: str) -> list[str]:
    return [k for k, rx in ((RAFFLE, _RAFFLE), (AUCTION, _AUCTION)) if rx.search(text)]


def _event(text: str, events: set[str]) -> str:
    low = text.casefold()
    for event in sorted(events, key=len, reverse=True):
        if re.search(rf"\b{re.escape(event.casefold())}\b", low) or any(re.search(rf"\b{re.escape(c)}\b", low) for c in _CALLED.get(event, ())):
            return event
        if any(re.search(rf"\b{c}\b", text) for c in _CALLED_EXACT.get(event, ())):
            return event
    return ""


def _name(page: Page) -> str:
    return f"{page.event} {page.year}" if page.year else page.event


def _passage(conn: sqlite3.Connection, base: str, title: str) -> dict:
    """What the answer cites: the page, opened at its first table if it has one."""
    r = conn.execute("SELECT c.chunk_id, c.section_path, c.anchor, c.text FROM chunks c JOIN pages p ON p.page_id=c.page_id WHERE p.title=? "
                     "ORDER BY instr(c.text, '|---')=0, c.ord LIMIT 1", (title,)).fetchone()
    return {"title": title, "section": r[1] if r else "", "url": page_url(base, title, r[2] if r else None), "text": r[3] if r else title,
            "chunk_id": r[0] if r else None}


def _taker(p: Prize) -> str:
    who = p.who + (f" ({p.price})" if p.price and p.who else "") + (f" ({p.notes})" if p.notes else "")
    return who


def _grouped(prizes: list[Prize]) -> list[str]:
    """One line per item, the most won first and otherwise as they were drawn."""
    groups: dict[str, list[Prize]] = {}
    for p in prizes:
        groups.setdefault(p.item.casefold(), []).append(p)
    lines = []
    for rows in sorted(groups.values(), key=lambda g: -len(g)):
        named = [_taker(p) for p in rows if p.who]
        missing = len(rows) - len(named)
        said = ", ".join(named)
        if missing:
            said += (" and " if said else "") + (f"{missing} with no winner listed" if named else "no winner listed")
        lines.append(f"- **{rows[0].item}** ({len(rows)}): {said}" if len(rows) > 1 else f"- **{rows[0].item}:** {said}")
    return lines


def _noun(kind: str, n: int) -> str:
    return ("raffle" if kind == RAFFLE else "auction") + ("" if n == 1 else "s")


def _listing(page: Page, prizes: list[Prize], current: bool, cite: str) -> str:
    if not prizes:
        return f"The {_name(page)} {_noun(page.kind, 2 if page.kind == RAFFLE else 1)} page has no {'prizes' if page.kind == RAFFLE else 'items'} listed yet {cite}."
    so_far = " so far" if current else ""
    if page.kind == RAFFLE:
        drawn = len({p.where for p in prizes})
        head = f"**{_name(page)} raffles:** {len(prizes)} prize{'' if len(prizes) == 1 else 's'}{so_far}, in {drawn} {_noun(RAFFLE, drawn)} {cite}"
    else:  # how many auctions there were cannot be told: their sections are named too unevenly
        head = f"**{_name(page)} auction:** {len(prizes)} item{'' if len(prizes) == 1 else 's'} sold{so_far} {cite}"
    lines = _grouped(prizes)
    out = [head, "", *lines[:MAX_ITEMS]]
    if len(lines) > MAX_ITEMS:
        out += ["", f"That's the {MAX_ITEMS} most often won of {len(lines)} different items. The rest are on the page {cite}."]
    return "\n".join(out)


def _pick_page(pages: list[Page], kind: str, event: str, year: int | None) -> tuple[Page | None, str]:
    """The page meant, and what to say first if it isn't quite what was asked. Newest means the highest year on the page's title."""
    mine = [p for p in pages if p.kind == kind and (not event or p.event == event) and (year is None or p.year == year)]
    if not mine:
        where = " ".join(x for x in (event, str(year) if year else "") if x) or "that"
        return None, f"My notes have no {_noun(kind, 2 if kind == RAFFLE else 1)} page for {where}."
    page = max(mine, key=lambda p: p.year)
    note = ""
    if year is None and event:
        newest = max((p.year for p in pages if p.event == event), default=0)
        if newest > page.year:
            note = f"My notes have no {_noun(kind, 1)} page for {event} {newest} yet, so this is the latest one, from {page.year}."
    return page, note


def _for_person(conn: sqlite3.Connection, base: str, person: str, pages: list[Page], kind: str, event: str) -> list[dict]:
    wanted = sorted((p for p in pages if p.kind == kind and (not event or p.event == event)), key=lambda p: -p.year)
    verb = "won" if kind == RAFFLE else "bought"
    passages, lines = [], []
    for page in wanted:
        rows = [p for p in _prizes(conn, page.title) if p.who.casefold() == person.casefold()]
        if not rows:
            continue
        passages.append(_passage(conn, base, page.title))
        lines.append(f"**{_name(page)}** [{len(passages)}]")
        lines += [f"- {p.item}" + (f" ({p.price})" if p.price else "") + (f", {p.where}" if p.where else "") for p in rows]
        lines.append("")
    if not passages:
        return []
    passages[0]["answer"] = f"What {person} {verb}, as far as my notes go:\n\n" + "\n".join(lines).strip()
    return passages


def _for_item(conn: sqlite3.Connection, base: str, wanted: str, first: Page, pages: list[Page], kind: str, event: str, year: int | None) -> list[dict]:
    words = wanted.casefold().split()

    def has(p: Prize) -> bool:
        return all(w in p.item.casefold() for w in words)

    here = [p for p in _prizes(conn, first.title) if has(p)]
    if here:
        passage = _passage(conn, base, first.title)
        passage["answer"] = f"**{_name(first)}** [1]\n\n" + "\n".join(_grouped(here))
        return [passage]
    if year is not None:
        return []
    passages, lines = [], []
    for page in sorted((p for p in pages if p.kind == kind and (not event or p.event == event)), key=lambda p: -p.year):
        rows = [p for p in _prizes(conn, page.title) if has(p)]
        if rows:
            passages.append(_passage(conn, base, page.title))
            lines += [f"**{_name(page)}** [{len(passages)}]", *_grouped(rows), ""]
    if passages:
        passages[0]["answer"] = f"Not at {_name(first)}, but my notes have it from earlier years:\n\n" + "\n".join(lines[:60]).strip()
    return passages


def _answer_one(conn: sqlite3.Connection, base: str, question: str, kind: str, pages: list[Page], named: Page | None, follow_up: bool) -> list[dict]:
    events = {p.event for p in pages}
    event = named.event if named else _event(question, events)
    without_numbers = _NUMBERED.sub(" ", question)
    found = _YEAR.search(without_numbers)
    year = named.year if named else int(found.group(1)) if found else None

    person = _person(question)
    if person:
        got = _for_person(conn, base, person, pages, kind, event)
        if got or follow_up:
            return got
        first = _passage(conn, base, max((p for p in pages if p.kind == kind), key=lambda p: p.year).title)
        first["answer"] = f"My notes don't list {person} on any {_noun(kind, 1)} page."
        return [first]

    page, note = (named, "") if named else _pick_page(pages, kind, event, year)
    if follow_up:
        note = ""  # already said, if it needed saying
    if page is None:
        if follow_up:
            return []
        newest = max((p for p in pages if p.kind == kind), key=lambda p: p.year, default=None)
        if newest is None:
            return []
        passage = _passage(conn, base, newest.title)
        passage["answer"] = f"{note} The newest I have is {_name(newest)} [1]."
        return [passage]

    lead = note + "\n\n" if note else ""
    prizes = _prizes(conn, page.title)
    number = _NUMBERED.search(question) if kind == RAFFLE else None
    passage = _passage(conn, base, page.title)
    if number:
        wanted = int(number.group(1))
        rows = [p for p in prizes if (w := _NUMBERED.search(p.where)) and int(w.group(1)) == wanted]
        passage["answer"] = lead + (f"**{_name(page)}, Raffle {wanted}** [1]\n\n" + "\n".join(_grouped(rows)) if rows
                                    else f"{_name(page)} has no Raffle {wanted} listed [1].")
        return [passage]

    who = _WHO_WON.search(question)
    wanted_item = _TRAILING.sub("", who.group(1)).strip(" .?!") if who else ""
    if wanted_item and not _NOT_AN_ITEM.match(wanted_item):
        got = _for_item(conn, base, wanted_item, page, pages, kind, event, year)
        if got:
            got[0]["answer"] = lead + got[0]["answer"]
            return got
        if follow_up:
            return []
        passage["answer"] = lead + f'{_name(page)} doesn\'t list anything called "{wanted_item}" [1].'
        return [passage]

    current = page.year == max((p.year for p in pages if p.event == page.event), default=0) and page.year > 0
    passage["answer"] = lead + _listing(page, prizes, current, "[1]")
    return [passage]


def passage(conn: sqlite3.Connection, question: str, base_url: str, earlier: list[str] | None = None) -> list[dict] | None:
    """Passages with the answer on the first, or None for a question this does not answer, such as how raffles work."""
    pages = _pages(conn)
    if not pages or not question:
        return None
    text = question.strip()
    named = next((p for t in explicit_pages(conn, text) for p in pages if p.title == t), None)
    kinds = [named.kind] if named else _kinds(text)
    follow_up = False
    if not kinds:
        if not (_FOLLOW_UP.search(text) or _person(text)):
            return None
        for before in (earlier or [])[:EARLIER_QUESTIONS]:  # "who won these?" means what was just being talked about
            named = next((p for t in explicit_pages(conn, before) for p in pages if p.title == t), None)
            kinds = [named.kind] if named else _kinds(before)
            if kinds:
                if not named and not _event(text, {p.event for p in pages}):
                    text = f"{text} {' '.join(x for x in (_event(before, {p.event for p in pages}), ) if x)}".strip()
                follow_up = True
                break
        if not kinds:
            return None
    elif not named and (not (_WANTS.search(text) or _NUMBERED.search(text)) or (_HOW.search(text) and not _FOLLOW_UP.search(text))):
        return None

    out: list[dict] = []
    answers: list[str] = []
    for kind in kinds:  # both only if both were asked for; each from its own pages
        got = _answer_one(conn, base_url, text, kind, pages, named, follow_up)
        if not got:
            continue
        shift = len(out)
        said = got[0].pop("answer", "")
        answers.append(re.sub(r"\[(\d+)\]", lambda m: f"[{int(m.group(1)) + shift}]", said))
        out += got
    if not out:
        return None
    out[0]["answer"] = "\n\n".join(answers)
    return out
