"""Conversation memory: sessions and messages stored in the same SQLite file."""

from __future__ import annotations

import json
import re
import sqlite3
import uuid

from ..util import utcnow_iso

_CITATION = re.compile(r"\s*\[\d+\](?:\[\d+\])*")
MAX_HISTORY_CHARS = 1500


def new_session(conn: sqlite3.Connection, frontend: str = "cli", session_id: str | None = None) -> str:
    sid = session_id or uuid.uuid4().hex[:12]
    conn.execute(
        "INSERT OR IGNORE INTO sessions(session_id, frontend, created_at) VALUES (?, ?, ?)",
        (sid, frontend, utcnow_iso()),
    )
    return sid


def history(conn: sqlite3.Connection, session_id: str, turns: int) -> list[dict[str, str]]:
    """Last `turns` user/assistant exchanges, with old [n] citation markers removed (numbering is per answer)."""
    rows = conn.execute(
        "SELECT role, content FROM messages WHERE session_id=? ORDER BY msg_id DESC LIMIT ?",
        (session_id, turns * 2),
    ).fetchall()
    out = []
    for r in reversed(rows):
        content = _CITATION.sub("", r["content"]) if r["role"] == "assistant" else r["content"]
        if len(content) > MAX_HISTORY_CHARS:  # bound prompt size: a long answer's opening is enough context
            content = content[:MAX_HISTORY_CHARS].rstrip() + " ..."
        out.append({"role": r["role"], "content": content})
    return out


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
) -> None:
    conn.execute(
        "INSERT INTO messages(session_id, role, content, standalone_q, mode, kind, sources, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (session_id, role, content, standalone_q, mode, kind, json.dumps(sources) if sources else None,
         utcnow_iso()),
    )


def messages(conn: sqlite3.Connection, session_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT role, content, standalone_q, mode, sources FROM messages WHERE session_id=? ORDER BY msg_id",
        (session_id,),
    ).fetchall()
    return [
        {**dict(r), "sources": json.loads(r["sources"]) if r["sources"] else []}
        for r in rows
    ]
