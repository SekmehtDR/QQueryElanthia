"""Conversation memory: sessions and messages stored in the same SQLite file.

A session is one conversation. Starting a new chat simply starts a new session; the old one stays on disk (the web
page lists it under Past chats) but is never given to the model unless the person resumes it. `context_from` lets a
person set the topic aside without losing the visible transcript: messages up to that point are shown, not remembered.
"""

from __future__ import annotations

import json
import re
import sqlite3
import uuid

from ..util import utcnow_iso
from .attribution import OFFER_LINE
from .userinput import split_message

# "[2]" cited by the model, "[2*]" matched automatically, "[2?]" a citation its passage does not bear out
_CITATION = re.compile(r"\s*\[\d+[*?]?\](?:\[\d+[*?]?\])*")
MAX_HISTORY_CHARS = 1500
TITLE_CHARS = 60


def new_session(conn: sqlite3.Connection, frontend: str = "cli", session_id: str | None = None,
                owner: str | None = None) -> str:
    sid = session_id or uuid.uuid4().hex[:12]
    conn.execute(
        "INSERT OR IGNORE INTO sessions(session_id, frontend, created_at, updated_at, owner) VALUES (?, ?, ?, ?, ?)",
        (sid, frontend, utcnow_iso(), utcnow_iso(), owner),
    )
    return sid


def owned_by(conn: sqlite3.Connection, session_id: str, owner: str) -> bool:
    """Does this conversation belong to `owner`? Conversations from before owners existed belong to 'local'."""
    row = conn.execute("SELECT owner FROM sessions WHERE session_id=?", (session_id,)).fetchone()
    if row is None:
        return False
    return (row["owner"] or "local") == owner


def session_exists(conn: sqlite3.Connection, session_id: str) -> bool:
    return conn.execute("SELECT 1 FROM sessions WHERE session_id=?", (session_id,)).fetchone() is not None


def _context_from(conn: sqlite3.Connection, session_id: str) -> int:
    row = conn.execute("SELECT context_from FROM sessions WHERE session_id=?", (session_id,)).fetchone()
    return (row["context_from"] or 0) if row else 0


def _clip(content: str) -> str:
    if len(content) > MAX_HISTORY_CHARS:  # bound prompt size: a long answer's opening is enough context
        return content[:MAX_HISTORY_CHARS].rstrip() + " ..."
    return content


def history(conn: sqlite3.Connection, session_id: str, turns: int) -> list[dict[str, str]]:
    """Last `turns` user/assistant exchanges, with old [n] citation markers removed (numbering is per answer).

    Only messages after the session's context boundary count: a person who set the topic aside gets a clean slate
    while the transcript stays visible. The partial text of an answer that failed is shown, never remembered."""
    rows = conn.execute(
        "SELECT role, content FROM messages WHERE session_id=? AND msg_id>? AND coalesce(mode, '') != 'error' "
        "ORDER BY msg_id DESC LIMIT ?",
        (session_id, _context_from(conn, session_id), turns * 2),
    ).fetchall()
    out = []
    for r in reversed(rows):
        content = _CITATION.sub("", _without_offer(r["content"])) if r["role"] == "assistant" else r["content"]
        out.append({"role": r["role"], "content": _clip(content)})
    return out


def _without_offer(answer: str) -> str:
    """Drop the closing "Want to hear about X?" line: left in the history, the model copies it into new answers."""
    parts = re.split(r"\n\s*\n", answer.rstrip())
    if len(parts) > 1 and OFFER_LINE.match(parts[-1].strip()):
        return "\n\n".join(parts[:-1])
    return answer


def recent_questions(conn: sqlite3.Connection, session_id: str, n: int = 8) -> list[str]:
    """The standalone form of the last `n` questions, oldest first: a cheap summary that outlives the turn window."""
    rows = conn.execute(
        "SELECT standalone_q FROM messages WHERE session_id=? AND msg_id>? AND role='user' "
        "AND standalone_q IS NOT NULL ORDER BY msg_id DESC LIMIT ?",
        (session_id, _context_from(conn, session_id), n),
    ).fetchall()
    return [r["standalone_q"] for r in reversed(rows)]


def last_question(conn: sqlite3.Connection, session_id: str) -> sqlite3.Row | None:
    """The last question that was answered by searching (its standalone form and kind), with the answer's mode, after
    the context boundary: what "are you sure?" is about."""
    return conn.execute(
        "SELECT u.standalone_q, u.kind, (SELECT a.mode FROM messages a WHERE a.session_id=u.session_id "
        "AND a.role='assistant' AND a.msg_id>u.msg_id ORDER BY a.msg_id LIMIT 1) AS answer_mode "
        "FROM messages u WHERE u.session_id=? AND u.msg_id>? AND u.role='user' AND u.standalone_q IS NOT NULL "
        "ORDER BY u.msg_id DESC LIMIT 1",
        (session_id, _context_from(conn, session_id)),
    ).fetchone()


RECENT_QUESTION_CHARS = 120  # longer ones are pasted posts or essays, not something to click again


def recent_user_questions(conn: sqlite3.Connection, owner: str | None = None, limit: int = 20,
                          frontend: str = "web") -> list[str]:
    """The last `limit` distinct questions asked on the web page (by anyone, or only by `owner`), newest first, for the
    owner's start screen. Each is the planner's standalone form ("What tools do I need for forging?", not "What tools do
    I need for it?") so it makes sense on its own. Small talk, the clock, declined questions, stories and re-checks are
    left out, as are chats deleted since."""
    rows = conn.execute(
        "SELECT m.standalone_q FROM messages m JOIN sessions s ON s.session_id = m.session_id "
        "WHERE m.role='user' AND m.standalone_q IS NOT NULL AND s.frontend=? AND (? IS NULL OR coalesce(s.owner, 'local')=?) "
        "AND coalesce(m.kind, '') != 'meta' AND coalesce(m.mode, '') NOT IN ('clock', 'declined', 'fiction', 'recheck') "
        "ORDER BY m.msg_id DESC LIMIT ?",
        (frontend, owner, owner, limit * 10),  # duplicates are common (the same question asked again): read ahead
    ).fetchall()
    out: list[str] = []
    seen: set[str] = set()
    for r in rows:
        q = re.sub(r"\s+", " ", r["standalone_q"]).strip()
        key = re.sub(r"[^a-z0-9]+", " ", q.casefold()).strip()
        if not key or key in seen or len(q) > RECENT_QUESTION_CHARS:
            continue
        seen.add(key)
        out.append(q)
        if len(out) >= limit:
            break
    return out


def last_pasted(conn: sqlite3.Connection, session_id: str) -> str:
    """The most recent text the person pasted into this conversation, for "the message I sent you"."""
    for r in conn.execute(
        "SELECT content FROM messages WHERE session_id=? AND msg_id>? AND role='user' ORDER BY msg_id DESC LIMIT 6",
        (session_id, _context_from(conn, session_id)),
    ):
        material = split_message(r["content"])[1]
        if material:
            return material
    return ""


def seen_titles(conn: sqlite3.Connection, session_id: str) -> set[str]:
    """Pages already cited or offered in this conversation, so they are not offered again."""
    seen: set[str] = set()
    for r in conn.execute(
        "SELECT sources, related FROM messages WHERE session_id=? AND role='assistant'", (session_id,)
    ):
        for s in json.loads(r["sources"]) if r["sources"] else []:
            if s.get("title"):
                seen.add(s["title"])
        seen.update(json.loads(r["related"]) if r["related"] else [])
    return seen


def last_was_clarification(conn: sqlite3.Connection, session_id: str) -> bool:
    row = conn.execute(
        "SELECT mode FROM messages WHERE session_id=? AND role='assistant' ORDER BY msg_id DESC LIMIT 1",
        (session_id,),
    ).fetchone()
    return bool(row) and row["mode"] == "clarify"


def add_message(
    conn: sqlite3.Connection,
    session_id: str,
    role: str,
    content: str,
    *,
    standalone_q: str | None = None,
    mode: str | None = None,
    kind: str | None = None,
    sources: list[dict] | None = None,
    related: list[str] | None = None,
    cited: list[int] | None = None,
    turn_id: int | None = None,
) -> int:
    """Store one message and return its id."""
    cur = conn.execute(
        "INSERT INTO messages(session_id, role, content, standalone_q, mode, kind, sources, related, cited, turn_id, "
        "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (session_id, role, content, standalone_q, mode, kind, json.dumps(sources) if sources else None,
         json.dumps(related) if related else None, json.dumps(cited) if cited is not None else None, turn_id,
         utcnow_iso()),
    )
    conn.execute("UPDATE sessions SET updated_at=? WHERE session_id=?", (utcnow_iso(), session_id))
    if role == "user":  # the first question names the conversation in the Past chats list
        conn.execute(
            "UPDATE sessions SET title=? WHERE session_id=? AND title IS NULL",
            (re.sub(r"\s+", " ", content).strip()[:TITLE_CHARS], session_id),
        )
    return cur.lastrowid


def note_plan(conn: sqlite3.Connection, msg_id: int, standalone_q: str, mode: str, kind: str) -> None:
    """Fill in what the planner made of a question that was saved before planning (so a failed turn keeps it)."""
    conn.execute("UPDATE messages SET standalone_q=?, mode=?, kind=? WHERE msg_id=?", (standalone_q, mode, kind, msg_id))


def set_topic(conn: sqlite3.Connection, session_id: str, topic: str | None) -> None:
    conn.execute("UPDATE sessions SET topic=? WHERE session_id=?", (topic or None, session_id))


def get_topic(conn: sqlite3.Connection, session_id: str) -> str | None:
    row = conn.execute("SELECT topic FROM sessions WHERE session_id=?", (session_id,)).fetchone()
    return row["topic"] if row else None


def clear_context(conn: sqlite3.Connection, session_id: str) -> None:
    """Set the topic aside: keep the transcript, but the model remembers nothing before this point."""
    newest = conn.execute("SELECT max(msg_id) FROM messages WHERE session_id=?", (session_id,)).fetchone()[0] or 0
    conn.execute("UPDATE sessions SET context_from=?, topic=NULL WHERE session_id=?", (newest, session_id))


def delete_session(conn: sqlite3.Connection, session_id: str) -> bool:
    cur = conn.execute("DELETE FROM sessions WHERE session_id=?", (session_id,))  # messages cascade
    return cur.rowcount > 0


def delete_sessions(conn: sqlite3.Connection, owner: str | None = None, frontend: str = "web") -> int:
    """Every conversation on this front end (or only `owner`'s), for "Delete all" in Past chats; their messages and turn
    records go with them, and so their questions leave the start screen. Returns how many conversations were deleted."""
    cur = conn.execute("DELETE FROM sessions WHERE frontend=? AND (? IS NULL OR coalesce(owner, 'local')=?)",
                       (frontend, owner, owner))
    return cur.rowcount


def list_sessions(conn: sqlite3.Connection, frontend: str = "web", limit: int = 50, owner: str | None = None) -> list[dict]:
    """Conversations that have at least one question, newest first, for the Past chats list. With `owner`, only that
    person's (visitors never see each other's, or the owner's)."""
    rows = conn.execute(
        "SELECT s.session_id, substr(coalesce(s.title, (SELECT m.content FROM messages m WHERE m.session_id=s.session_id "
        "AND m.role='user' ORDER BY m.msg_id LIMIT 1)), 1, 60) AS title, s.topic, coalesce(s.updated_at, s.created_at) AS updated_at, "
        "(SELECT count(*) FROM messages m WHERE m.session_id=s.session_id AND m.role='user') AS questions "
        "FROM sessions s WHERE s.frontend=? AND questions > 0 AND (? IS NULL OR coalesce(s.owner, 'local') = ?) "
        "ORDER BY updated_at DESC, s.rowid DESC LIMIT ?",
        (frontend, owner, owner, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def messages(conn: sqlite3.Connection, session_id: str) -> list[dict]:
    """The transcript; an answer carries its turn id and any thumbs up or down it was given (core/feedback.py)."""
    rows = conn.execute(
        "SELECT m.role, m.content, m.standalone_q, m.mode, m.kind, m.sources, m.related, t.turn_id, t.feedback "
        "FROM messages m LEFT JOIN turns t ON t.assistant_msg_id = m.msg_id "
        "WHERE m.session_id=? ORDER BY m.msg_id",
        (session_id,),
    ).fetchall()
    return [
        {**dict(r), "sources": json.loads(r["sources"]) if r["sources"] else [],
         "related": json.loads(r["related"]) if r["related"] else []}
        for r in rows
    ]
