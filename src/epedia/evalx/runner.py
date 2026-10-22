"""Retrieval evaluation: hit@k, MRR, planner mode and kind accuracy over a YAML question set."""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from ..config import Settings
from ..core.assistant import Assistant
from ..llm.ollama import Ollama

# Reviewer's checklist for judging an answer's voice by hand (score each 0 or 1; aim for 5 of 6).
VOICE_CHECKLIST = (
    "voice check: (1) direct answer in the first two sentences  (2) every factual sentence has a valid [n]  "
    "(3) two spot-checked facts really appear in the cited passages  (4) the wiki's hedges (outdated, obsolete, "
    "incomplete, era, source tier) are relayed  (5) mechanics plain, (OOC) at most once, commands in CAPS  "
    "(6) at most one closing offer, taken from the related list, none when it is empty"
)


@dataclass
class Result:
    q: str
    standalone: str
    mode: str
    expected_mode: str
    expect: list[str]
    titles: list[str]
    rank: int | None
    kind: str = "general"
    expected_kind: str | None = None
    answer: str | None = None
    related: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


async def run_eval(
    settings: Settings, conn: sqlite3.Connection, ollama: Ollama, path: Path, ks: list[int], answers: bool
) -> tuple[list[Result], dict[str, float]]:
    questions = yaml.safe_load(path.read_text(encoding="utf-8"))
    assistant = Assistant(settings, conn, ollama)
    results: list[Result] = []
    k_max = max(ks)
    for item in questions:
        hist: list[dict[str, str]] = []
        for earlier in item.get("history", []):
            hist += [{"role": "user", "content": earlier}, {"role": "assistant", "content": "(answered)"}]
        plan = await assistant.plan(hist, item["q"])
        old_k = settings.retrieval.k_final
        settings.retrieval.k_final = k_max
        try:
            passages = await assistant.retrieve(plan)
        finally:
            settings.retrieval.k_final = old_k
        titles: list[str] = []
        for p in passages:
            if p["title"] not in titles:
                titles.append(p["title"])
        expect = item.get("expect_pages", [])
        want = {t.lower() for t in expect}
        rank = next((i + 1 for i, t in enumerate(titles) if t.lower() in want), None)
        res = Result(item["q"], plan.standalone_question, plan.mode, item.get("mode", "search"), expect, titles, rank,
                     kind=plan.kind, expected_kind=item.get("kind"))
        if answers:
            sid = f"eval-{uuid.uuid4().hex[:10]}"  # throwaway: must not see, or leave behind, real chat history
            try:
                done = await assistant.ask(sid, item["q"])
                res.answer, res.related = done.answer, done.related
            finally:
                conn.execute("DELETE FROM messages WHERE session_id=?", (sid,))
                conn.execute("DELETE FROM sessions WHERE session_id=?", (sid,))
        results.append(res)

    scored = [r for r in results if r.expect]
    metrics: dict[str, float] = {"questions": float(len(results)), "scored": float(len(scored))}
    for k in ks:
        metrics[f"hit@{k}"] = sum(1 for r in scored if r.rank and r.rank <= k) / max(len(scored), 1)
    metrics["mrr"] = sum(1 / r.rank for r in scored if r.rank) / max(len(scored), 1)
    metrics["mode_accuracy"] = sum(1 for r in results if r.mode == r.expected_mode) / max(len(results), 1)
    labelled = [r for r in results if r.expected_kind]
    if labelled:
        metrics["kind_accuracy"] = sum(1 for r in labelled if r.kind == r.expected_kind) / len(labelled)
    return results, metrics
