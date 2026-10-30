"""Extractors turn the tables and infoboxes of known pages into typed `facts` rows.

Each extractor is a small function registered here with the page titles it reads; `index.enrich` runs it whenever one
of those pages has a newer revision than its facts. A new kind of structured data is one extractor, not new plumbing.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass

# (entity, grp, attribute, value_num, value_text, section)
Fact = tuple[str, str, str, float | None, str, str]


@dataclass(frozen=True)
class Extractor:
    domain: str
    titles: tuple[str, ...]
    run: Callable[..., list[Fact]]  # [(section_path, text)] of the page -> facts (a per-page one also gets the title)
    pages: Callable[[sqlite3.Connection], list[str]] | None = None  # titles listed from the database instead


REGISTRY: list[Extractor] = []


def register(domain: str, titles: tuple[str, ...]):
    def deco(fn: Callable[[list[tuple[str, str]]], list[Fact]]):
        REGISTRY.append(Extractor(domain, titles, fn))
        return fn
    return deco


def register_pages(domain: str, pages: Callable[[sqlite3.Connection], list[str]]):
    """An extractor for many pages of one kind (each material's own page): `pages` lists their titles from facts
    already read, so it must be registered after the extractor that reads those, and it is called as fn(chunks, title)."""
    def deco(fn: Callable[[list[tuple[str, str]], str], list[Fact]]):
        REGISTRY.append(Extractor(domain, (), fn, pages))
        return fn
    return deco


_NUM = re.compile(r"-?\d[\d,]*\.?\d*")
_RULE = re.compile(r"\|(?:\s*:?-+:?\s*\|)+")


def number(cell: str) -> float | None:
    """The first number in a table cell ('3,750 Kronars' -> 3750.0, '4.5; 4.75' -> 4.5), or None."""
    m = _NUM.search(cell.replace(" ", " "))
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", ""))
    except ValueError:
        return None


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def tables(text: str):
    """Every markdown table in `text` as (the line just above it, header cells, data rows). A section the chunker
    folded into its neighbour shows up only as such a line ("Common stone"), so the label is kept with the table."""
    lines = text.splitlines()
    i = 0
    while i < len(lines) - 1:
        if lines[i].startswith("|") and _RULE.fullmatch(lines[i + 1].strip()):
            above = next((ln.strip() for ln in reversed(lines[:i]) if ln.strip()), "")
            j = i + 2
            rows = []
            while j < len(lines) and lines[j].startswith("|"):
                if not _RULE.fullmatch(lines[j].strip()):
                    rows.append(_cells(lines[j]))
                j += 1
            yield above, _cells(lines[i]), rows
            i = j
        else:
            i += 1


def table_rows(text: str) -> tuple[list[str], list[list[str]]]:
    """(header cells, data rows) of the first markdown table in `text`; empty if there is none."""
    for _, header, rows in tables(text):
        return header, rows
    return [], []
