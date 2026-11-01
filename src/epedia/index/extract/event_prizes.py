"""What was raffled and auctioned at each festival, from its `<festival>/Raffles` and `<festival>/Auction` pages.
`core/event_prizes.py` answers from these. A real conversation showed why the model cannot do it: asked for one page's prizes, it
invented a raffle out of the page's blank template and mixed up the rows."""

from __future__ import annotations

import json
import re
import sqlite3

from . import _RULE, Fact, _cells, register_pages

DOMAIN = "event_prize"
RAFFLE, AUCTION = "raffle", "auction"
_MAX_LABEL = 80  # longer than a heading: a sentence above the table is not its name
_PART = re.compile(r"\s*\(part \d+\)$")  # the chunker's mark on a long section, not part of its name
_NOT_A_PRICE = ("notes", "special description")
_PRICE_WORDS = re.compile(r"\b(?:winning bid|price in|price)\b|[()]", re.I)
_BLANK = frozenset(("", "?", "x", "-", "...", "none"))  # what editors type for "nothing here yet"


def kind_of(title: str) -> str:
    return RAFFLE if title.endswith("/Raffles") else AUCTION


def _pages(conn: sqlite3.Connection) -> list[str]:
    return [r[0] for r in conn.execute("SELECT title FROM pages WHERE (title LIKE '%/Raffles' OR title LIKE '%/Auction') "
                                       "AND title NOT LIKE 'Talk:%' AND is_redirect=0")]


def _money(header: str) -> str:
    """The name of a price column, or nothing for one that is not a price. 'Platinum; Kronars' is one heading on two lines, and
    pages also say 'Winning Bid (Platinum Kronars)' and 'Price in platinum kronar'."""
    if not header or "#" in header or header.casefold() in _NOT_A_PRICE:
        return ""
    return " ".join(_PRICE_WORDS.sub(" ", header).replace(";", " ").split()).title()


@register_pages(DOMAIN, _pages)
def extract(chunks: list[tuple[str, str]], title: str) -> list[Fact]:
    """One fact per row with an item. Which raffle it was is the line above its table when the chunker folded that section in, or
    else its section. The chunker splits long tables and the later pieces have no header, so the last header carries over."""
    kind = kind_of(title)
    facts: list[Fact] = []
    head: list[str] = []
    for section, text in chunks:
        label = _PART.sub("", section.split(" > ")[-1])
        above = ""
        for line in text.splitlines():
            if not line.startswith("|"):
                above = line.strip() or above
                continue
            if _RULE.fullmatch(line.strip()):
                continue
            cells = _cells(line)
            if cells[0].casefold() == "item" and len(cells) >= 2:
                head = cells
                if above and len(above) <= _MAX_LABEL:
                    label = above
                continue
            if not head or len(cells) != len(head) or not cells[0]:
                continue
            row = dict(zip(head, cells))
            who = row.get("Winner", row.get("Buyer", ""))
            price = next(((cell, _money(name)) for name, cell in zip(head[2:], cells[2:]) if cell and _money(name)), None)
            notes = row.get("Notes") or row.get("Special Description") or ""
            value = {"who": "" if who.strip().casefold() in _BLANK else who, "notes": "" if notes.strip().casefold() in _BLANK else notes}
            if price:
                value["price"] = f"{price[0]} {price[1]}"
            n = len(facts)
            facts.append((cells[0], kind, f"{n:05d}", float(n), json.dumps(value, ensure_ascii=False), label))
    return facts
