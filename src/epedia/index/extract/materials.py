"""Crafting materials from the tables of Category:Crafting materials: every metal, stone, bone, wood, cloth and
leather with its stats (hardness, density, workability, bow affinities...). The tables are the wiki's own lookup;
the individual material pages repeat them."""

from __future__ import annotations

import re

from . import Fact, number, register, tables

TITLE = "Category:Crafting materials"

# header text (lower case) -> attribute
ATTRIBUTES = {
    "hardness": "hardness", "durability": "durability", "workability": "workability", "electrical": "electrical",
    "thermal": "thermal", "physical": "physical", "physical resistance": "physical", "ductility": "ductility",
    "capacity": "capacity", "density": "density", "density*": "density", "value": "value", "value*": "value",
    "boulder value*": "value", "stiffness": "stiffness", "bend strength": "bend_strength", "potency": "potency",
    "efficacy": "efficacy", "solubility": "solubility", "toxicity": "toxicity",
    "shortbow / arbalest affinity": "affinity_short", "longbow / hx affinity": "affinity_long",
    "composite bow / lx affinity": "affinity_composite",
}
_LABEL = re.compile(r"(?:^|> )(?:(?P<craft>Forging|Engineering|Outfitting|Alchemy) materials > )?"
                    r"(?P<rarity>Common|Rare|Special) (?P<matter>[a-z]+)(?: \(part \d+\))?$", re.I)
_MATTER = {"metals": "metal", "plants": "plant"}
_CRAFT = {"metal": "Forging", "stone": "Engineering", "bone": "Engineering", "wood": "Engineering",
          "cloth": "Outfitting", "leather": "Outfitting"}


def _group(*labels: str) -> str | None:
    """'metal|Common|Forging' from the first label that names rarity and matter ('Common stone',
    'Forging materials > Rare metals (part 2)')."""
    for label in labels:
        m = _LABEL.search(label.strip())
        if m:
            matter = _MATTER.get(m["matter"].lower(), m["matter"].lower())
            craft = (m["craft"] or _CRAFT.get(matter, "")).capitalize()
            return f"{matter}|{m['rarity'].capitalize()}|{craft}"
    return None


@register("material", (TITLE,))
def extract(chunks: list[tuple[str, str]]) -> list[Fact]:
    facts: list[Fact] = []
    for section, text in chunks:
        for above, header, rows in tables(text):
            grp = _group(above, section)
            if grp is None:
                continue
            cols = [(i, ATTRIBUTES.get(h.strip().lower())) for i, h in enumerate(header)]
            for row in rows:
                name = row[0].strip() if row else ""
                if not name:
                    continue
                for i, attr in cols:
                    if i == 0 or attr is None or i >= len(row) or not row[i].strip():
                        continue
                    facts.append((name, grp, attr, number(row[i]), row[i].strip(), section))
    return facts
