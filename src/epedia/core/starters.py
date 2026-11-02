"""What the start screen offers to ask, by subject. Real questions that went well come first, because they are what people want to know;
the checked ones in `kinds.START_TOPICS` fill the rest, so a quiet page still has something true to offer."""

from __future__ import annotations

import sqlite3
from typing import Any

from ..kinds import START_TOPICS
from . import memory

SHOWN = 3


def topics(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    out = []
    for key, label, kinds, checked in START_TOPICS:
        picked: dict[str, str] = {}
        for q in [*memory.answered_questions(conn, kinds, SHOWN), *checked]:
            picked.setdefault(memory.question_key(q), q)
        out.append({"key": key, "label": label, "questions": list(picked.values())[:SHOWN]})
    return out
