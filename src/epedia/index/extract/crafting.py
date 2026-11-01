"""The crafting skills and the disciplines each one controls, from the Crafting page's own list, and the guide the page
names for each discipline. `core/disciplines.py` answers "what are the crafting disciplines?" from these."""

from __future__ import annotations

import re
import sqlite3

from . import _RULE, Fact, number, register, register_pages, tables

TITLE = "Crafting"
_ITEM = re.compile(r"^(\s*)- (.+?)\s*$")


@register("crafting", (TITLE,))
def extract(chunks: list[tuple[str, str]]) -> list[Fact]:
    facts: list[Fact] = []
    for section, text in chunks:
        skill = ""
        for line in text.splitlines():
            m = _ITEM.match(line)
            if not m:
                continue
            indent, item = len(m.group(1)), m.group(2)
            if section.startswith("General Notes"):
                if indent == 0:
                    skill = item
                elif skill:
                    facts.append((item, skill, "discipline_of", None, skill, section))
            elif section == "Guides" and " - " in item:
                discipline, guide = item.split(" - ", 1)
                facts.append((discipline.strip(), "", "guide", None, guide.strip(), section))
    return facts


# What it takes to mastercraft something (`core/craft_ranks.py`): the rank figures per tier, and each product's tier.
GUIDE = "Tailoring player guide"
_DIFFICULTY = re.compile(r"^\s*0?(\d{1,2})\s*-")
_TIER_ROW = re.compile(r"^(?:Tier\s*)?(\d{1,2})$")


def tier_of(cell: str) -> int | None:
    """'11- v.difficult' or '09 - Intricate' -> the tier. 'Varies', '?', a blank or '~50 engineering ranks' -> None."""
    m = _DIFFICULTY.match(cell)
    return int(m.group(1)) if m and 1 <= int(m.group(1)) <= 12 else None


def _tier_facts(tier: int, source: str, name: str, base: str, techs: str, section: str) -> list[Fact]:
    entity = f"Tier {tier}"
    return [(entity, source, "name", None, name, section), (entity, source, "base", number(base), base, section),
            (entity, source, "techs", number(techs), techs, section)]


@register("craft_tier", (TITLE,))
def chart(chunks: list[tuple[str, str]]) -> list[Fact]:
    """The Crafting page's chart. Its header sits in a data row, and a cell at a workability reads '980 / ~700'."""
    facts: list[Fact] = []
    for section, text in chunks:
        if section != "Difficulty":
            continue
        for _, _, rows in tables(text):
            head = next((r for r in rows if "Instruction Difficulty" in r), None)
            if head is None:
                continue
            for row in rows:
                m = _TIER_ROW.match(row[0])
                if not m or len(row) != len(head):
                    continue
                tier = int(m.group(1))
                facts += _tier_facts(tier, "chart", row[1], row[2], row[3], section)
                for label, cell in zip(head[4:], row[4:]):
                    base, _, techs = cell.partition("/")
                    facts += [(f"Tier {tier}", "chart", f"{key}@{number(label):g}", number(value), value.strip(), section)
                              for key, value in (("base", base), ("techs", techs)) if number(value) is not None]
    return facts


@register("craft_tier", (GUIDE,))
def guide_table(chunks: list[tuple[str, str]]) -> list[Fact]:
    """The player guide's table, which fills the chart's blanks. Its with-techs column can be a range ('850-950')."""
    facts: list[Fact] = []
    for section, text in chunks:
        for _, header, rows in tables(text):
            if [c.casefold() for c in header[:2]] != ["tier", "difficulty"]:
                continue
            for row in rows:
                if len(row) >= 4 and _TIER_ROW.match(row[0]):
                    facts += _tier_facts(int(row[0]), "guide", row[1], row[2], row[3], section)
    return facts


def _product_pages(conn: sqlite3.Connection) -> list[str]:
    return [f"{r[0]} products" for r in conn.execute("SELECT entity FROM facts WHERE domain='crafting' AND attribute='discipline_of'")]


def _heading(cell: str) -> str:
    return cell.casefold().rstrip("*").strip()


def _blocks(text: str) -> list[list[list[str]]]:
    """Each run of table lines as rows of cells, rule lines dropped. Not `tables`: a split table's later pieces have no rule."""
    blocks: list[list[list[str]]] = []
    current: list[list[str]] = []
    for line in [*text.splitlines(), ""]:
        if line.startswith("|"):
            if not _RULE.fullmatch(line.strip()):
                current.append([c.strip() for c in line.strip().strip("|").split("|")])
        elif current:
            blocks.append(current)
            current = []
    return blocks


@register_pages("craft_product", _product_pages)
def products(chunks: list[tuple[str, str]], title: str) -> list[Fact]:
    """Every row of a discipline's products page. The chunker splits long tables and the later pieces have no header, so
    the last real header carries over to a run of lines with the same columns and a tier where the header says one goes."""
    facts: list[Fact] = []
    last: list[str] = []
    for section, text in chunks:
        for rows in _blocks(text):
            names = [_heading(c) for c in rows[0]]
            if "difficulty" in names and "item" in names:
                last, rows = names, rows[1:]
            elif not (last and len(rows[0]) == len(last) and tier_of(rows[0][last.index("difficulty")]) is not None):
                continue
            for row in rows:
                if len(row) != len(last):
                    continue
                cell = dict(zip(last, row))
                placeholder = re.search(r"<([a-z ]+)>", cell["item"])
                name = " ".join(re.sub(r"<[^>]*>", " ", cell["item"]).split())
                if not name:
                    continue
                facts.append((name, section, "tier", tier_of(cell["difficulty"]), cell["difficulty"], section))
                for key, attribute in (("technique", "technique"), ("source", "source")):
                    if cell.get(key):
                        facts.append((name, section, attribute, None, cell[key], section))
                if placeholder:
                    facts.append((name, section, "placeholder", None, placeholder.group(1), section))
    return facts
