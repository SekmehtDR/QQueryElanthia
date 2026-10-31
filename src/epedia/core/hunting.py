"""'What should I hunt?': match a player's combat ranks to the hunting ladder, in code.

The ladder (Estimated Ranks hunting ladder) lists a min and max rank per creature. Players choose creatures by
their offensive and defensive (combat) ranks whatever the guild, so the lookup works from those numbers. The
model only words the result; it never picks creatures or does the comparison.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass

LADDER_TITLE = "Estimated Ranks hunting ladder"

# "what should I hunt", "which creatures can I fight", not "what ranks do I need to hunt a rat"
HUNT_FIT = re.compile(
    r"\b(?:what|which|where)\b[^?.!]{0,40}\b(?:hunt|fight|train on|grind)\b"
    r"|\bbest\b[^?.!]{0,30}\b(?:creatures?|monsters?|hunting)\b"
    r"|\bcreatures?\b[^?.!]{0,30}\b(?:should|can|could)\s+i\s+(?:hunt|fight)\b",
    re.I,
)

_RANKS = re.compile(r"(\d[\d,]*)\s*(?:combat\s+)?ranks?\b", re.I)
_OFFENSE_WORD = r"(?:offen[sc]e|offensive|attack)"
_DEFENSE_WORD = r"(?:defen[sc]e|defensive|evasion)"
_WORD_THEN_NUMBER = [re.compile(rf"\b{w}\b\D{{0,14}}(\d[\d,]*)", re.I) for w in (_OFFENSE_WORD, _DEFENSE_WORD)]
_NUMBER_THEN_WORD = [re.compile(rf"(\d[\d,]*)\s+(?:ranks?\s+(?:of\s+|in\s+)?)?{w}\b", re.I) for w in (_OFFENSE_WORD, _DEFENSE_WORD)]

NEED_RANKS = (
    "Happy to help you find something worth fighting! What I match on is combat ranks, and your circle alone "
    "doesn't pin those down. Could you tell me your offensive and defensive ranks (your main weapon skill and "
    "your evasion or armor)? A rough number is fine, and tell me the area too if you have a favourite."
)


@dataclass
class Creature:
    name: str
    level: str
    low: int
    high: int
    place: str


def _num(s: str) -> int | None:
    try:
        return int(s.replace(",", "").strip())
    except ValueError:
        return None


def parse_ranks(*texts: str) -> list[int]:
    """Rank numbers the person gave, offense and defense first; empty when they gave none."""
    found: list[int] = []
    for text in texts:
        for pat in (*_labelled(text or ""), _RANKS):
            for m in pat.finditer(text or ""):
                n = _num(m.group(1))
                if n is not None and 0 < n <= 3000 and n not in found:
                    found.append(n)
    return found


def _labelled(text: str) -> list[re.Pattern]:
    """Whichever way this message writes them. In "200 offense and 180 defense" a word reaching for the next number would
    read offense as 180; in "offense 200 defense 180" a number reaching for the next word would read defense as 200."""
    def first(patterns: list[re.Pattern]) -> int:
        return min((m.start() for p in patterns if (m := p.search(text))), default=len(text))

    return _NUMBER_THEN_WORD if first(_NUMBER_THEN_WORD) < first(_WORD_THEN_NUMBER) else _WORD_THEN_NUMBER


def ladder(conn: sqlite3.Connection) -> list[Creature]:
    rows = conn.execute("SELECT text FROM chunks WHERE title = ? ORDER BY ord", (LADDER_TITLE,)).fetchall()
    out: list[Creature] = []
    seen: set[tuple[str, str]] = set()
    for r in rows:
        for line in r[0].splitlines():
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if not line.lstrip().startswith("|") or len(cells) < 6:
                continue
            low, high = _num(cells[3]), _num(cells[4])
            if low is None or high is None or low > high:  # "2,000-0" is the page's mark for no data
                continue
            key = (cells[0], cells[5])
            if key in seen:
                continue
            seen.add(key)
            out.append(Creature(cells[0], cells[1], low, high, cells[5]))
    return out


def fits(creatures: list[Creature], ranks: int, limit: int = 15) -> list[Creature]:
    inside = [c for c in creatures if c.low <= ranks <= c.high]
    inside.sort(key=lambda c: (abs(ranks - (c.low + c.high) / 2), c.name))  # comfortably inside the range first
    return inside[:limit]


def areas_in(text: str, creatures: list[Creature]) -> list[str]:
    """Place names from the ladder that the person mentioned (Zoluren, Ilithi, ...)."""
    places = {p.strip() for c in creatures for p in c.place.split(";") if len(p.strip()) >= 4}
    return sorted(p for p in places if re.search(r"\b" + re.escape(p) + r"\b", text, re.I))


def passage(conn: sqlite3.Connection, ranks: list[int], base_url: str, text: str = "") -> dict | None:
    """A table passage for the answer, or None when the ladder is not indexed or nothing fits."""
    creatures = ladder(conn)
    if not creatures or not ranks:
        return None
    r = min(ranks)  # the weaker of offense and defense limits what is safe
    areas = areas_in(text, creatures)
    pool = [c for c in creatures if any(a in c.place for a in areas)] if areas else creatures
    rows = fits(pool, r) or fits(creatures, r)
    if not rows:
        return None
    lines = [f"Creatures whose estimated rank range includes {r} ranks (from the ladder, player-reported):",
             "| Creature | Level | Min ranks | Max ranks | Where |", "|---|---|---|---|---|"]
    lines += [f"| {c.name} | {c.level or '-'} | {c.low} | {c.high} | {c.place} |" for c in rows]
    if len(ranks) > 1:
        lines.append(f"(Matched on the lower of the ranks given: {', '.join(str(x) for x in ranks)}.)")
    lines.append("Recommend only creatures from this table, and say where each is found.")
    lines.append("Rank ranges are anecdotal and may be outdated; stats, buffs, armor and mastery skills change them.")
    where = f" in {' and '.join(areas)}" if areas and rows and pool is not creatures and fits(pool, r) else ""
    bullets = [f"- **{c.name}** (level {c.level or '?'}, ranks {c.low}-{c.high}): {c.place} [1]" for c in rows]
    answer = (
        f"Going by the ladder, these creatures have a rank range that takes in {r} ranks{where}, "
        "with the ones where you sit nearest the middle of the range first:\n\n" + "\n".join(bullets)
        + "\n\n(OOC) The ranges are player-reported and can be out of date, and buffs, armor and mastery skills shift "
        "them. Start with a creature where your ranks sit well inside its range, and read its page before you go."
    )
    return {
        "answer": answer,
        "title": LADDER_TITLE,
        "section": "Bestiary by Estimated Ranks, matched to your ranks",
        "url": base_url.rstrip("/") + "/" + LADDER_TITLE.replace(" ", "_"),
        "text": "\n".join(lines),
        "chunk_id": None,
    }
