"""Thumbs up or down on an answer, kept on that answer's turn record (and so with its conversation), for reviewing the
answers people did not like: what was asked, what was said, what retrieval and the checks recorded, and why they
disliked it.

A vote lives in `turns.feedback` ("up" or "down"), `turns.feedback_reason` (JSON: the reasons picked and a note) and
`turns.feedback_at`. Deleting a conversation deletes its turns, votes included."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

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
