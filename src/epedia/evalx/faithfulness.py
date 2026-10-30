"""Scoring an answer for truthfulness, in code: are its citations borne out, does it name things no passage has, does it
pretend to look things up, and did it refuse when it should (or should not) have. Used by `epedia eval --full`."""

from __future__ import annotations

import re
import sqlite3
from statistics import median

from ..core import verify
from ..core.attribution import OFFER_LINE

# How Fennick (or the plain style) says the wiki has nothing on it. The partial note ("I couldn't find every part of
# that.") follows a real answer, so it is not a refusal.
REFUSAL = re.compile(
    r"couldn't find (?!every part)|could not find|can't find|cannot find|no page (?:called|named|on|about)|"
    r"nothing on (?:that|this|it)|doesn't seem to cover|does not seem to cover|don't have (?:anything|a page|notes|any notes)|"
    r"not in my (?:notes|satchel)|nothing in my (?:notes|satchel)|no page that mentions|outside (?:of )?Elanthia|"
    r"isn't something I do|isn't a tale I|"
    r"my notes (?:don't|do not) (?:cover|mention|say|have)|no (?:mention|record) of",
    re.I,
)
# Paragraphs code writes around the model's answer (edit notes, honesty notes, the story decline; the related-page offer
# is OFFER_LINE): not the model's words.
_CODE_TAIL = re.compile(r"^(?:That said, my notes|Mind you,|One caution: |Spinning a tale of my own|"
                        r"I only answer from the wiki, so I don't write|Note: (?:a figure marked|most of this answer could "
                        r"not|.{1,200} not appear in the pages this answer))")


def is_refusal(answer: str) -> bool:
    """Did the answer decline (the wiki has nothing on it, or it is not something Fennick does)? Only the opening
    counts: a refusal leads, it is not buried under paragraphs of content."""
    first = re.split(r"\n\s*\n", answer.strip(), maxsplit=1)[0]
    return bool(REFUSAL.search(first[:400]))


def model_text(answer: str) -> str:
    """The answer without the paragraphs code appended to it."""
    parts = [p for p in re.split(r"\n\s*\n", answer.strip()) if not (OFFER_LINE.match(p.strip()) or _CODE_TAIL.match(p.strip()))]
    return "\n\n".join(parts)


def passages_for(conn: sqlite3.Connection, sources: list) -> list[dict]:
    """The numbered passages an answer was written from, read back from the index by chunk id (a history passage has
    none; its title stands in)."""
    out = []
    for s in sources:
        cid = getattr(s, "chunk_id", None) if not isinstance(s, dict) else s.get("chunk_id")
        title = s.title if not isinstance(s, dict) else s["title"]
        row = conn.execute("SELECT title, section_path, text FROM chunks WHERE chunk_id=?", (cid,)).fetchone() if cid else None
        out.append({"title": row["title"], "section": row["section_path"], "text": row["text"]} if row
                   else {"title": title, "section": "", "text": getattr(s, "snippet", "") or ""})
    return out


def check(conn: sqlite3.Connection, answer: str, sources: list, question: str, message: str = "",
          context: str = "") -> dict:
    """One answer's truthfulness record."""
    v = verify.run(model_text(answer), passages_for(conn, sources), question, message, context)
    return {**v.as_dict(), "refused": is_refusal(answer)}


def check_done(conn: sqlite3.Connection, done, question: str, message: str = "", context: str = "") -> dict:
    """One answer's record from a finished turn: what the assistant measured against the very passages it used (the
    edit-history passage and code-written tables cannot be read back from the index), or nothing to check when code
    wrote the answer."""
    if getattr(done, "by_code", False):
        return {**verify.Verification("").as_dict(), "refused": is_refusal(done.answer), "by_code": True}
    if getattr(done, "verification", None):
        return {**done.verification, "refused": is_refusal(done.answer)}
    return check(conn, done.answer, done.sources, question, message, context)


def aggregate(rows: list[dict]) -> dict[str, float]:
    """Run-level numbers from per-answer records (each from `check`, plus `seconds`)."""
    if not rows:
        return {}
    cited = sum(r["cited_units"] for r in rows)
    factual = sum(r["factual_units"] for r in rows)
    out = {
        "answers": float(len(rows)),
        "citation_validity": 1 - sum(len(r["unsupported"]) for r in rows) / cited if cited else 1.0,
        "cited_share": cited / factual if factual else 0.0,
        "answers_with_unknown_names": sum(1 for r in rows if r["unknown_entities"]) / len(rows),
        "answers_with_fake_actions": sum(1 for r in rows if r["fake_actions"]) / len(rows),
        "numbers_bad": float(sum(r["numbers_bad"] for r in rows)),
        "answers_revised": sum(1 for r in rows if r.get("revised")) / len(rows),  # first draft withdrawn and rewritten
    }
    secs = sorted(r["seconds"] for r in rows if r.get("seconds") is not None)
    if secs:
        out["seconds_p50"] = median(secs)
        out["seconds_p90"] = secs[min(len(secs) - 1, int(0.9 * len(secs)))]
    return out
