"""Where a thumbs-down answer first went wrong, and what to try, worked out by code from what was recorded on the way to it.

These are suggestions for whoever works through the vote ("process our feedback" in CLAUDE.md): nothing here changes Fennick.
It only spots failures it has a check for, and says "unclear" for the rest. No model is asked: the small one cannot be trusted to
judge its own answers. Everything read is data, because visitors wrote some of it and pages are player-written."""

from __future__ import annotations

import re
import sqlite3
from typing import Any

from ..retrieval.resolve import named_pages
from ..retrieval.search import STOPWORDS

STEPS = ("wiki", "indexing", "planner", "retrieval", "prompt", "model", "check")  # earliest first: the first failure is what to fix
_WORD = re.compile(r"[a-z0-9']+")
_DECLINED = re.compile(r"not listed|(?:could not|couldn't|can't|cannot) find|not in (?:my|the) notes|don't have|do not have|"
                       r"not (?:fully )?documented|no (?:details|information)", re.I)
_LIST_LINE = re.compile(r"^\s*(?:[-*]|\d+\.)\s", re.M)
_TABLE_RULE = "|---"
_DEFAULT = {"first_failure": "unclear", "findings": [],
            "suggestions": ["Nothing the checks know about: this one needs a look. If a new kind of failure turns up, add a check for it."]}


def _finding(check: str, step: str, says: str, fix: str) -> dict[str, str]:
    return {"check": check, "step": step, "says": says, "fix": fix}


def _page_chunks(conn: sqlite3.Connection, title: str) -> list[tuple[int, str]]:
    return [(r[0], r[1]) for r in conn.execute(
        "SELECT c.chunk_id, c.text FROM chunks c JOIN pages p ON p.page_id=c.page_id WHERE p.title=? ORDER BY c.ord", (title,))]


def _sent_from(passages: list[dict], title: str) -> list[dict]:
    return [p for p in passages if p.get("title") == title]


def _unsent(conn: sqlite3.Connection, passages: list[dict], title: str) -> list[str]:
    """The texts of a page's sections that were not among the passages."""
    sent = _sent_from(passages, title)
    ids = {p.get("chunk_id") for p in sent}
    texts = {(p.get("text") or "").strip() for p in sent}
    return [text for cid, text in _page_chunks(conn, title) if cid not in ids and text.strip() not in texts]


def passage_findings(conn: sqlite3.Connection, passages: list[dict], named: list[str]) -> list[dict[str, str]]:
    """What is wrong with one set of passages. Also used to try a question again on the current version."""
    out = []
    for title in named:
        total = len(_page_chunks(conn, title))
        missed = len(_unsent(conn, passages, title))
        if missed:
            out.append(_finding("named_page_unread", "retrieval",
                                f"The question names {title}, but only {total - missed} of its {total} sections were sent.",
                                "Read a page the question names directly, instead of searching around it."))
    if named and passages:
        others = sum(p.get("title") not in named for p in passages)
        if others * 2 >= len(passages):
            out.append(_finding("other_pages", "retrieval", f"{others} of the {len(passages)} passages were from other pages.",
                                "When a page is named, keep the other pages out of its slots."))
    seen: dict[str, set[str]] = {}
    counts: dict[str, int] = {}
    for p in passages:
        text = re.sub(r"\s+", " ", p.get("text") or "").strip()
        if len(text) >= 200:  # short lines repeat by chance; a long block repeating is a template
            seen.setdefault(text, set()).add(p.get("title") or "")
            counts[text] = counts.get(text, 0) + 1
    worst = max((n for text, n in counts.items() if len(seen[text]) > 1), default=0)
    if worst:
        out.append(_finding("repeated_block", "retrieval", f"{worst} of the {len(passages)} passages were the same block of text on different pages.",
                            "Stop boilerplate that repeats across pages from being searchable."))
    return out


def _turns_of(record: dict) -> list[dict[str, Any]]:
    """Each exchange in order, with what was recorded for it. The voted turn is last."""
    msgs = {m["msg_id"]: m for m in record.get("messages") or []}
    out = []
    for t in [*(record.get("earlier") or []), record["turn"]]:
        asked = msgs.get(t.get("user_msg_id")) or {}
        said = msgs.get(t.get("assistant_msg_id")) or {}
        out.append({"question": asked.get("content") or "", "planned": asked.get("standalone_q") or "", "answer": said.get("content") or "",
                    "passages": (t.get("chain") or {}).get("passages") or [], "verify": t.get("verify") or {}})
    return out


def _distinctive(text: str) -> set[str]:
    return {w.rstrip("s") for w in _WORD.findall((text or "").casefold()) if len(w) >= 5 and w not in STOPWORDS}


def diagnose(conn: sqlite3.Connection, record: dict) -> dict[str, Any]:
    """Findings earliest step first, the first failure, and what to try."""
    turns = _turns_of(record)
    now = turns[-1]
    named = named_pages(conn, now["question"]) or named_pages(conn, now["planned"])
    findings = passage_findings(conn, now["passages"], named)

    for i, t in enumerate(turns, 1):  # it said the wiki lacks something the page it leaned on has
        if not _DECLINED.search(t["answer"]) or not t["passages"]:
            continue
        title = next(iter(named), t["passages"][0].get("title") or "")
        tables = [x for x in _unsent(conn, t["passages"], title) if _TABLE_RULE in x]
        if tables:
            findings.append(_finding("declined_but_there", "retrieval",
                                     f"The answer to question {i} of {len(turns)} said it wasn't there, but {len(tables)} sections with tables on {title} were never sent.",
                                     "Find out why those sections rank below the rest of the page."))

    checks = now["verify"]
    if checks.get("unknown_entities"):
        names = ", ".join(str(x) for x in checks["unknown_entities"][:5])
        findings.append(_finding("unknown_names", "model", f"The answer named things no page has: {names}.",
                                 "Check whether the passages tempted it, or whether the check should have stopped it."))
    if checks.get("numbers_bad"):
        findings.append(_finding("bad_numbers", "model", "The answer has a figure its page does not have.",
                                 "Consider having code write these figures."))
    if checks.get("unsupported"):
        findings.append(_finding("unsupported", "model", f"{len(checks['unsupported'])} lines were not backed by their pages.",
                                 "Compare those lines with their passages: a wrong passage is retrieval, a right one is the model."))
    if len(_LIST_LINE.findall(now["answer"])) >= 3 and any(_TABLE_RULE in (p.get("text") or "") for p in now["passages"]):
        findings.append(_finding("list_from_table", "model", "The answer is a list made from table passages, and the small model copies tables badly.",
                                 "Consider having code write this answer, since the page is a table."))

    related = sum(bool(_distinctive(a["planned"] or a["question"]) & _distinctive(b["planned"] or b["question"])) for a, b in zip(turns, turns[1:]))
    if related >= 2:
        findings.append(_finding("asked_again", "check", f"The person asked about the same thing {related + 1} times in a row.",
                                 "Read the earlier answers too: the first one to miss is usually the real failure."))
    reasons = record["turn"].get("reasons") or []
    if "context" in reasons and len(turns) > 1 and not _distinctive(turns[-2]["planned"]) & _distinctive(now["planned"]):
        findings.append(_finding("lost_subject", "planner", f"They say it lost track, and the planner's rewrite kept nothing from the last question: {now['planned']!r}.",
                                 "Check what the planner was given and why the subject was dropped."))

    if not findings:
        return dict(_DEFAULT)
    findings.sort(key=lambda f: STEPS.index(f["step"]))
    return {"first_failure": findings[0]["step"], "findings": findings, "suggestions": list(dict.fromkeys(f["fix"] for f in findings if f["check"] != "asked_again"))}


async def retest(assistant: Any, conn: sqlite3.Connection, record: dict) -> dict[str, Any]:
    """The question again on the current version, retrieval only (no model, so nothing about the wording is proved). A vote may
    already be fixed: this is what to look at first."""
    from .assistant import Plan

    now = _turns_of(record)[-1]
    plan = (record["turn"].get("plan") or {})
    question = plan.get("standalone_question") or now["planned"] or now["question"]
    if not question:
        return {"error": "no question recorded"}
    fresh = Plan(standalone_question=question, kind=plan.get("kind") or "general", asked=now["question"])
    if hasattr(assistant, "_linked_pages"):  # what the page does with a message before planning it
        fresh.pinned = assistant._linked_pages(now["question"])[1]
    try:
        passages = await assistant.retrieve(fresh)
    except Exception as exc:  # usually Ollama not running: the queue is still worth having
        return {"error": str(exc)}
    if passages and passages[0].get("answer"):
        return {"passages": len(passages), "still": [], "looks_fixed": True, "written_by_code": True}
    named = named_pages(conn, now["question"]) or named_pages(conn, question)
    still = [f["says"] for f in passage_findings(conn, passages, named)]
    return {"passages": len(passages), "still": still, "looks_fixed": not still and bool(named), "written_by_code": False}
