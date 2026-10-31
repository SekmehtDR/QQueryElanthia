"""Offer replay: what the closing offer would be now for every answer in the conversation log, next to what was offered
then, for the owner to judge; plus labelled cases from the log (eval/offers.yaml) that must not come back.

Each logged answer keeps its question, kind, sources and citations, so the replay needs only one embedding per
question and no model call.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import yaml

from ..config import Settings
from ..core import offers
from ..llm.ollama import Ollama

SKIP_MODES = {"clarify", "declined", "clock", "fiction", "error"}


def logged_answers(conn: sqlite3.Connection) -> list[dict]:
    """One row per distinct question in the log that was answered from pages: question, kind, sources, cited, offer."""
    rows, seen = [], set()
    for r in conn.execute(
            "SELECT a.standalone_q, a.kind, a.mode, a.sources, a.cited, a.related FROM messages a "
            "WHERE a.role='assistant' AND a.sources IS NOT NULL ORDER BY a.msg_id DESC"):
        q = (r[0] or "").strip()
        if not q or r[1] == "meta" or r[2] in SKIP_MODES or q.casefold() in seen:
            continue
        sources = json.loads(r[3] or "[]")
        if not sources:
            continue
        seen.add(q.casefold())
        rows.append({"q": q, "kind": r[1] or "general", "sources": sources, "cited": json.loads(r[4] or "[]"),
                     "old": (json.loads(r[5]) if r[5] else [None])[0]})
    return rows


async def run_offers(settings: Settings, conn: sqlite3.Connection, ollama: Ollama, cases: Path) -> tuple[list[dict], dict]:
    """(rows of question/anchor/old/new, metrics). Rows marked `case` come from the labelled file and carry `ok`."""
    cfg = settings.retrieval
    rows = logged_answers(conn)
    items = yaml.safe_load(cases.read_text(encoding="utf-8")) if cases.exists() else []
    for item in items:
        rows.append({"q": item["q"], "kind": item.get("kind", "general"), "sources": [{"title": item["anchor"]}],
                     "cited": [1], "old": item.get("was"), "must_not": item.get("must_not", []), "case": True})
    vecs = await ollama.embed(["search_query: " + r["q"] for r in rows]) if rows else []
    for r, qvec in zip(rows, vecs, strict=True):
        r["anchor"] = offers.anchor_title(r["sources"], r["cited"])
        used = {s["title"] for s in r["sources"]}
        r["new"] = (offers.related(conn, cfg, r["kind"], r["anchor"], used, qvec) or [None])[0]
        if r.get("case"):
            r["ok"] = r["new"] not in r["must_not"]
        del r["sources"]
    logged = [r for r in rows if not r.get("case")]
    labelled = [r for r in rows if r.get("case")]
    metrics = {
        "answers": float(len(logged)),
        "offered_before": sum(r["old"] is not None for r in logged) / max(len(logged), 1),
        "offered_now": sum(r["new"] is not None for r in logged) / max(len(logged), 1),
        "changed": sum(r["old"] != r["new"] for r in logged) / max(len(logged), 1),
        "cases_ok": sum(r["ok"] for r in labelled) / max(len(labelled), 1),
    }
    return rows, metrics
