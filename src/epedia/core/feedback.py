"""Thumbs up or down on an answer, kept on that answer's turn record (and so with its conversation), for reviewing the
answers people did not like: what was asked, what was said, what retrieval and the checks recorded, and why they
disliked it.

A vote lives in `turns.feedback` ("up" or "down"), `turns.feedback_reason` (JSON: the reasons picked and a note) and
`turns.feedback_at`. Deleting a conversation deletes its turns, votes included."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

import yaml

from ..util import utcnow_iso

# The reasons a thumbs-down offers (the page shows the labels; the keys are what is stored).
REASONS = {
    "wrong": "Wrong or made up",
    "missed": "Didn't answer my question",
    "context": "Lost track of the conversation",
    "unclear": "Too long or hard to follow",
}
VOTES = ("up", "down")
MAX_NOTE = 500


def turn_session(conn: sqlite3.Connection, turn_id: int) -> str | None:
    """The conversation a turn belongs to, or None if there is no such turn (or it has no answer to vote on)."""
    row = conn.execute("SELECT session_id FROM turns WHERE turn_id=? AND assistant_msg_id IS NOT NULL",
                       (turn_id,)).fetchone()
    return row["session_id"] if row else None


def record(conn: sqlite3.Connection, turn_id: int, vote: str | None, reasons: list[str] | None = None,
           note: str = "") -> dict:
    """Set (or, with vote None, clear) the vote on a turn. Unknown reasons are dropped; a reason or note without a
    thumbs-down is meaningless and not kept."""
    if vote is not None and vote not in VOTES:
        raise ValueError(f"vote must be one of {VOTES} or null")
    picked = [r for r in dict.fromkeys(reasons or []) if r in REASONS] if vote == "down" else []
    note = (note or "").strip()[:MAX_NOTE] if vote == "down" else ""
    reason = json.dumps({"reasons": picked, "note": note}, ensure_ascii=False) if (picked or note) else None
    conn.execute("UPDATE turns SET feedback=?, feedback_reason=?, feedback_at=? WHERE turn_id=?",
                 (vote, reason, utcnow_iso() if vote else None, turn_id))
    return {"vote": vote, "reasons": picked, "note": note}


def parse_reason(text: str | None) -> dict:
    try:
        data = json.loads(text) if text else {}
    except (ValueError, TypeError):
        data = {"note": text}
    return {"reasons": list(data.get("reasons") or []), "note": data.get("note") or ""}


def votes(conn: sqlite3.Connection, vote: str | None = "down", since: str | None = None, limit: int = 50) -> list[dict]:
    """Voted turns, newest vote first: the question as typed and as planned, the answer, the reasons, who asked (the
    owner here, or a visitor) and what the checks recorded. `vote` None lists both kinds."""
    rows = conn.execute(
        "SELECT t.turn_id, t.session_id, t.feedback, t.feedback_reason, t.feedback_at, t.owner, t.total_ms, t.regenerated,"
        " t.verify_json, t.plan_json, u.content AS question, u.standalone_q, a.content AS answer, a.mode, a.kind "
        "FROM turns t JOIN messages u ON u.msg_id=t.user_msg_id JOIN messages a ON a.msg_id=t.assistant_msg_id "
        "WHERE t.feedback IS NOT NULL AND (? IS NULL OR t.feedback=?) AND (? IS NULL OR t.feedback_at >= ?) "
        "ORDER BY t.feedback_at DESC LIMIT ?",
        (vote, vote, since, since, limit),
    ).fetchall()
    out = []
    for r in rows:
        checks = json.loads(r["verify_json"]) if r["verify_json"] else {}
        out.append({**{k: r[k] for k in r.keys() if k not in ("verify_json", "plan_json", "feedback_reason")},
                    **parse_reason(r["feedback_reason"]), "checks": checks})
    return out


def conversation(conn: sqlite3.Connection, turn_id: int) -> dict[str, Any] | None:
    """A voted turn with the conversation up to and including it, and its full record (plan, retrieval signals,
    checks), for working out what went wrong."""
    t = conn.execute("SELECT * FROM turns WHERE turn_id=?", (turn_id,)).fetchone()
    if t is None:
        return None
    upto = t["assistant_msg_id"] or t["user_msg_id"]
    msgs = conn.execute(
        "SELECT m.msg_id, m.role, m.content, m.standalone_q, m.mode, m.kind, tt.feedback FROM messages m "
        "LEFT JOIN turns tt ON tt.assistant_msg_id = m.msg_id WHERE m.session_id=? AND m.msg_id<=? ORDER BY m.msg_id",
        (t["session_id"], upto),
    ).fetchall()
    record_ = {k: t[k] for k in t.keys()}
    for k in ("plan_json", "grounding_json", "verify_json"):
        record_[k.removesuffix("_json")] = json.loads(record_.pop(k)) if t[k] else None
    record_.update(parse_reason(record_.pop("feedback_reason")))
    return {"turn": record_, "messages": [dict(m) for m in msgs]}


# Marked lines and rewritten drafts are noted on an entry but do not put it on the list: most answers have one, and a list
# of most answers says nothing.
WORTH_A_LOOK = frozenset({"thumbs down", "failed", "named things no page has", "a figure its page does not have"})


def why_flagged(row: dict) -> list[str]:
    """What makes a turn worth the owner's look, most telling first."""
    checks = row.get("checks") or {}
    out = []
    if row.get("feedback") == "down":
        out.append("thumbs down")
    if row.get("error"):
        out.append("failed")
    if checks.get("unknown_entities"):
        out.append("named things no page has")
    if checks.get("numbers_bad"):
        out.append("a figure its page does not have")
    if checks.get("unsupported"):
        out.append("lines its pages do not back")
    if row.get("regenerated"):
        out.append("rewritten")
    return out


def review(conn: sqlite3.Connection, flagged: bool = True, since: str | None = None, owner: str | None = None,
           frontend: str | None = "web", limit: int = 50) -> list[dict]:
    """Turns for the owner to look through, thumbs-down first and then newest first. `owner` is an owner id, "local", or
    "visitors" for everyone else; `frontend` None means every front end. Eval runs delete their sessions, and their turns."""
    rows = conn.execute(
        "SELECT t.turn_id, t.session_id, t.started_at, t.owner, t.frontend, t.feedback, t.feedback_reason, t.error, t.regenerated, "
        "t.verify_json, u.content AS question, u.standalone_q, a.mode, a.kind FROM turns t JOIN messages u ON u.msg_id=t.user_msg_id "
        "LEFT JOIN messages a ON a.msg_id=t.assistant_msg_id WHERE (? IS NULL OR t.frontend=?) AND (? IS NULL OR t.started_at >= ?) "
        "AND (? IS NULL OR (CASE WHEN ?='visitors' THEN coalesce(t.owner, 'local') != 'local' ELSE coalesce(t.owner, 'local')=? END)) "
        "ORDER BY t.feedback='down' DESC, t.turn_id DESC LIMIT ?",
        (frontend, frontend, since, since, owner, owner, owner, limit * 10 if flagged else limit),
    ).fetchall()
    out = []
    for r in rows:
        row = {k: r[k] for k in r.keys() if k not in ("verify_json", "feedback_reason")}
        row.update(parse_reason(r["feedback_reason"]))
        row["checks"] = json.loads(r["verify_json"]) if r["verify_json"] else {}
        row["flags"] = why_flagged(row)
        if not flagged or WORTH_A_LOOK & set(row["flags"]):
            out.append(row)
        if len(out) >= limit:
            break
    return out


def eval_item(conn: sqlite3.Connection, turn_id: int) -> str | None:
    """A turn as a skeleton for eval/questions.yaml, to check and paste by hand: nothing joins the evaluation set by itself,
    and the pages it expects are only what the answer cited, which is the first thing to doubt in a bad answer."""
    t = conn.execute(
        "SELECT t.session_id, t.user_msg_id, u.content, u.standalone_q, u.kind, u.mode, a.sources, a.cited FROM turns t "
        "JOIN messages u ON u.msg_id=t.user_msg_id LEFT JOIN messages a ON a.msg_id=t.assistant_msg_id WHERE t.turn_id=?",
        (turn_id,)).fetchone()
    if t is None:
        return None
    cited = set(json.loads(t["cited"])) if t["cited"] else set()
    sources = json.loads(t["sources"]) if t["sources"] else []
    pages = list(dict.fromkeys(s["title"] for s in sources if s.get("n") in cited and s.get("title")))
    earlier = [r[0] for r in conn.execute("SELECT content FROM messages WHERE session_id=? AND role='user' AND msg_id<? ORDER BY msg_id",
                                          (t["session_id"], t["user_msg_id"]))]
    item: dict[str, Any] = {"q": t["content"], "kind": t["kind"] or "general", "expect_pages": pages}
    if earlier:
        item["history"] = earlier
    if t["mode"] and t["mode"] != "search":
        item["mode"] = t["mode"]
    return ("# Check before adding to eval/questions.yaml: expect_pages is only what this answer cited.\n"
            + yaml.safe_dump([item], allow_unicode=True, sort_keys=False, width=110))
