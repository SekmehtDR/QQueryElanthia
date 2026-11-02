"""'Shorter' under an answer: the model cuts its own answer down, and the same checks read the result.

There is no "explain it simpler" on purpose. Tried on real answers, the small model paraphrased game facts into new claims ("Kertigen is a
place"; "the wiki says both types are not fully known"), some of which passed every check. Cutting text down, it stays faithful, and it
does best given only the answer: with the passages as well it copied them back, longer and with more mistakes.

Only what the model wrote is cut. An answer code wrote is exact on purpose, and the notes code added after the model's text (a player's log
is not lore, a list is partial) come back word for word, because a shorter answer must never lose them. A result that fails the checks is
not shown: the person keeps the answer they have. Nothing is saved, so a vote is always on the original."""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from typing import Any

from ..util import now_text
from . import turns, verify

_REWRITABLE_MODES = ("search", "items")
MIN_SENTENCES = 3  # shorter than this is short already
MAX_LENGTH = 0.8  # of the original: anything longer is not worth the reader's click
_SHARE = 0.45  # how much to ask for: it writes a little more than asked
_SENTENCE = re.compile(r"(?<=[.!?])\s+")
_DOUBT = re.compile(r"\[\d+\?\]")
_ECHOED_HEADING = re.compile(r"\A\*\*[^*\n]+\*\*(?:\s*\[\d+[*?]?\])*\s*\n+")  # it likes to title its answer with the question


class CannotRewrite(Exception):
    """Says why, in words for the person reading."""


@dataclass
class Rewritten:
    answer: str
    cited: list[int]


def possible(mode: str | None, kind: str | None, by_code: bool = False) -> bool:
    """Whether an answer of this sort has model-written text worth cutting: not code's, and not small talk about Fennick."""
    return not by_code and mode in _REWRITABLE_MODES and kind != "meta"


def _sentences(text: str) -> int:
    return sum(len(s.split()) > 2 for s in _SENTENCE.split(" ".join(text.split())))


def _system(body: str) -> str:
    return (f"Rewrite this answer about the game DragonRealms in at most {max(1, round(_sentences(body) * _SHARE))} sentences. Keep only what answers the "
            "question. Keep the [n] after each fact you keep, copied exactly. Add no fact that is not in the answer. Keep any **bold headings** "
            "you still have text for. Write only the new answer.")


def _notes_still_true(tail: str, final: str) -> str:
    """Code's notes, less any about a passage the shorter answer no longer cites ("[8] is a player's log" with no [8] left)."""
    from .assistant import ANY_CITE

    cited = set(ANY_CITE.findall(final))
    kept = [p for p in re.split(r"\n\s*\n", tail) if p.strip() and (not ANY_CITE.search(p) or set(ANY_CITE.findall(p)) & cited)]
    return "".join(f"\n\n{p.strip()}" for p in kept)


def _plan_of(row: dict[str, Any]) -> dict:
    return json.loads(row["plan_json"]) if row.get("plan_json") else {}


async def shorter(assistant: Any, conn: sqlite3.Connection, turn_id: int) -> Rewritten:
    """The cut-down text, with code's notes back on the end. Raises CannotRewrite rather than returning something the checks refused."""
    from .assistant import ANY_CITE

    row = turns.get(conn, turn_id)
    chain = turns.unpack_chain(row["chain"]) if row else {}
    plan = _plan_of(row) if row else {}
    body, passages = chain.get("model_body") or "", chain.get("passages") or []
    if not row or not body or not passages or not possible(plan.get("mode"), plan.get("kind")):
        raise CannotRewrite("This answer can't be shortened.")
    if _sentences(body) < MIN_SENTENCES:
        raise CannotRewrite("This one's about as short as it gets.")
    question = plan.get("standalone_question") or ""
    asked = plan.get("asked") or question
    messages = [{"role": "system", "content": _system(body)}, {"role": "user", "content": f"Question: {question}\n\nAnswer to rewrite:\n{body}"}]
    chunks: list[str] = []
    watch = verify.Repeats()
    stream = assistant.ollama.chat_stream(messages, temperature=assistant.s.chat.temperature)
    try:
        async for tok in stream:
            chunks.append(tok)
            if watch.feed(tok):
                raise CannotRewrite("That didn't come out right, so here's the original.")
    finally:
        if hasattr(stream, "aclose"):
            await stream.aclose()
    draft = _ECHOED_HEADING.sub("", assistant._clean_draft("".join(chunks), passages).strip())
    # What the first answer said has been checked already, so its names are not inventions.
    checked = verify.run(draft, passages, question, asked, f"{body} {now_text()}")
    final = verify.strip_fake_actions(checked.text).strip()
    before = verify.run(body, passages, question, asked, f"{body} {now_text()}")
    if (not final or checked.fabricated or checked.numbers_bad or (ANY_CITE.search(body) and not ANY_CITE.search(final))
            or len(checked.unsupported) > len(before.unsupported) or len(_DOUBT.findall(final)) > len(_DOUBT.findall(body))
            or len(final) > MAX_LENGTH * len(body)):
        raise CannotRewrite("That didn't hold up against the notes, so here's the original.")
    answer = final + _notes_still_true(chain.get("code_tail") or "", final)
    n = len(passages)
    return Rewritten(answer, sorted({int(c) for c in ANY_CITE.findall(answer) if 1 <= int(c) <= n}))
