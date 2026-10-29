"""'What are the crafting disciplines?' answered in code from the Crafting page's list (facts, domain 'crafting'), so the
model cannot call a discipline a skill or invent a sixth one."""

from __future__ import annotations

import re
import sqlite3

_ASK = re.compile(r"\b(disciplines?|crafting skills?|crafting skillsets?|(?:kinds|types|sorts) of crafting|"
                  r"what (?:can|could) i (?:craft|make)|what crafts)\b", re.I)
TITLE = "Crafting"


def wants(question: str) -> bool:
    return bool(_ASK.search(question))


def passage(conn: sqlite3.Connection, question: str, base_url: str) -> dict | None:
    if not wants(question):
        return None
    rows = conn.execute("SELECT entity, grp, attribute, value_text FROM facts WHERE domain='crafting' ORDER BY grp, entity").fetchall()
    skills: dict[str, list[str]] = {}
    guides: dict[str, str] = {}
    for r in rows:
        if r["attribute"] == "discipline_of":
            skills.setdefault(r["grp"], []).append(r["entity"])
        elif r["attribute"] == "guide":
            guides[r["entity"]] = r["value_text"]
    if not skills:
        return None
    lines = []
    for skill, disciplines in skills.items():
        parts = [f"{d} (guide: {guides[d]})" if d in guides else d for d in disciplines]
        own = f" (guide: {guides[skill]})" if skill in guides else ""  # the page names one guide for the whole skill
        lines.append(f"- **{skill}** skill{own}: " + ", ".join(parts) + " [1]")
    n = len(skills)
    answer = (f"In my notes crafting is {n} Lore skills, and each one controls its own disciplines. These are the ones "
              "released so far, skill first:\n\n" + "\n".join(lines)
              + "\n\n(OOC) The Crafting page says not every discipline has been released yet; a skill is trained, a "
              "discipline is what you actually craft with it.")
    url = base_url.rstrip("/") + "/" + TITLE + "#General_Notes"
    return {"answer": answer, "title": TITLE, "section": "General Notes", "url": url, "chunk_id": None,
            "text": answer.replace(" [1]", "")}
