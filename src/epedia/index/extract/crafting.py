"""The crafting skills and the disciplines each one controls, from the Crafting page's own list, and the guide the page
names for each discipline. `core/disciplines.py` answers "what are the crafting disciplines?" from these."""

from __future__ import annotations

import re

from . import Fact, register

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
