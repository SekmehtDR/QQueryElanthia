"""The record of each turn: how long each step took, what the planner decided, and what went wrong if anything did.

A turn row is opened as soon as the question is saved and closed when the answer is (or when it fails), so a question
that crashed the model or was abandoned by the reader still leaves a trace the owner can look at.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import asdict, is_dataclass
from typing import Any

from ..util import utcnow_iso

STEPS = ("planner", "retrieve", "ttft")


class Turn:
    def __init__(self, conn: sqlite3.Connection, turn_id: int, session_id: str, user_msg_id: int):
        self.conn = conn
        self.turn_id = turn_id
        self.session_id = session_id
        self.user_msg_id = user_msg_id
        self.closed = False
        self.plan: Any = None  # set once the question is planned, so a turn that fails later still records it
        self._t0 = time.monotonic()
        self._marks: dict[str, int] = {}

    @classmethod
    def start(cls, conn: sqlite3.Connection, session_id: str, user_msg_id: int) -> "Turn":
        row = conn.execute("SELECT frontend, owner FROM sessions WHERE session_id=?", (session_id,)).fetchone()
        cur = conn.execute(
            "INSERT INTO turns(session_id, user_msg_id, started_at, frontend, owner) VALUES (?,?,?,?,?)",
            (session_id, user_msg_id, utcnow_iso(), row["frontend"] if row else None,
             (row["owner"] or "local") if row else None),
        )
        conn.execute("UPDATE messages SET turn_id=? WHERE msg_id=?", (cur.lastrowid, user_msg_id))
        return cls(conn, cur.lastrowid, session_id, user_msg_id)

    def elapsed_ms(self) -> int:
        return int((time.monotonic() - self._t0) * 1000)

    def mark(self, step: str) -> None:
        """Time from the start of the turn to the end of `step` (the first mark of a step counts)."""
        if step in STEPS and step not in self._marks:
            self._marks[step] = self.elapsed_ms()

    def finish(self, assistant_msg_id: int | None, plan: Any = None, *, grounding: dict | None = None,
               verify: dict | None = None, regenerated: bool = False) -> None:
        self._close(assistant_msg_id, plan, None, grounding, verify, regenerated)

    def fail(self, error: BaseException | str, plan: Any = None, assistant_msg_id: int | None = None) -> None:
        text = error if isinstance(error, str) else (f"{type(error).__name__}: {error}".rstrip(": ") or type(error).__name__)
        self._close(assistant_msg_id, plan, text[:1000], None, None, False)

    def _close(self, assistant_msg_id, plan, error, grounding, verify, regenerated) -> None:
        if self.closed:
            return
        self.closed = True
        plan = plan if plan is not None else self.plan
        self.conn.execute(
            "UPDATE turns SET assistant_msg_id=?, planner_ms=?, retrieve_ms=?, ttft_ms=?, total_ms=?, plan_json=?, "
            "grounding_json=?, verify_json=?, regenerated=?, error=? WHERE turn_id=?",
            (assistant_msg_id, self._marks.get("planner"), self._marks.get("retrieve"), self._marks.get("ttft"),
             self.elapsed_ms(), _json(plan), _json(grounding), _json(verify), int(regenerated), error, self.turn_id),
        )


def _json(value: Any) -> str | None:
    if value is None:
        return None
    if is_dataclass(value):
        # the question's vector is 768 floats of noise, and prebuilt passages are already in the answer's sources
        value = {k: v for k, v in asdict(value).items() if k not in ("qvec", "prebuilt")}
    return json.dumps(value, ensure_ascii=False, default=str)


def get(conn: sqlite3.Connection, turn_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM turns WHERE turn_id=?", (turn_id,)).fetchone()
    return dict(row) if row else None
