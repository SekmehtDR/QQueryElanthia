"""Semantic MediaWiki access: per-page properties (browsebysubject), the property catalog, and live ask queries."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import sqlite3
from typing import Any

from ..config import Settings
from ..db import transaction
from ..llm.ollama import Ollama
from ..text.stats import clean_value, render
from ..util import token_estimate, utcnow_iso
from .client import WikiClient, WikiError

log = logging.getLogger("epedia.smw")

TYPE_CODES = {1: "_num", 2: "_txt", 4: "_boo", 6: "_dat", 9: "_wpg"}
STATS_ORD = 1000


def parse_properties(data: dict[str, Any]) -> list[dict]:
    """browsebysubject response -> [{prop, value_text, value_num, value_type}] (internal '_' properties dropped)."""
    out: list[dict] = []
    for entry in data.get("query", {}).get("data", []):
        prop = entry["property"]
        if prop.startswith("_"):
            continue
        for di in entry.get("dataitem", []):
            vtype = TYPE_CODES.get(di.get("type"), "_txt")
            raw = str(di.get("item", ""))
            if vtype == "_wpg":
                raw = raw.split("#", 1)[0].replace("_", " ")
            else:
                raw = clean_value(raw)
            num = None
            if vtype == "_num":
                try:
                    num = float(raw)
                except ValueError:
                    pass
            if raw:
                out.append({"prop": prop, "value_text": raw, "value_num": num, "value_type": vtype})
    return out


async def fetch_properties(client: WikiClient, title: str) -> list[dict] | None:
    try:
        data = await client.get({"action": "browsebysubject", "subject": title})
    except WikiError as exc:
        log.warning("browsebysubject %s: %s", title, exc)
        return None
    return parse_properties(data)


async def smw_pages(
    settings: Settings,
    conn: sqlite3.Connection,
    client: WikiClient,
    ollama: Ollama,
    *,
    ns: list[int] | None = None,
    limit: int | None = None,
    batch: int = 30,
) -> dict[str, int]:
    ns_list = ns or settings.wiki.item_namespaces
    stats = {"pages": 0, "props": 0, "no_props": 0}
    last_id = 0
    remaining = limit or 10**9
    while remaining > 0:
        rows = conn.execute(
            f"SELECT page_id, title, fetched_revid FROM pages WHERE is_redirect=0 AND fetched_revid IS NOT NULL "
            f"AND smw_revid IS NOT fetched_revid AND page_id > ? AND ns IN ({','.join(str(n) for n in ns_list)}) "
            f"ORDER BY page_id LIMIT ?",
            (last_id, min(batch, remaining)),
        ).fetchall()
        if not rows:
            break
        results = await asyncio.gather(*(fetch_properties(client, r["title"]) for r in rows))
        sheets: list[tuple[sqlite3.Row, list[dict], str]] = []
        for row, props in zip(rows, results):
            if props is None:
                continue
            sheets.append((row, props, render(row["title"], props) if props else ""))
        texts = {
            r["page_id"]: f"search_document: {r['title']} | Stats\n{sheet}" for r, _, sheet in sheets if sheet
        }
        vectors: dict[int, bytes] = {}
        if texts:
            ids = list(texts)
            embs = await ollama.embed([texts[i] for i in ids])
            from ..index.indexer import vec_blob

            vectors = {i: vec_blob(v) for i, v in zip(ids, embs)}
        with transaction(conn):
            for row, props, sheet in sheets:
                pid = row["page_id"]
                conn.execute("DELETE FROM item_properties WHERE page_id=?", (pid,))
                conn.executemany(
                    "INSERT OR IGNORE INTO item_properties(page_id, prop, value_text, value_num, value_type) "
                    "VALUES (?, ?, ?, ?, ?)",
                    [(pid, p["prop"], p["value_text"], p["value_num"], p["value_type"]) for p in props],
                )
                conn.execute(
                    "DELETE FROM chunk_vec WHERE chunk_id IN (SELECT chunk_id FROM chunks WHERE page_id=? AND kind='stats')",
                    (pid,),
                )
                conn.execute("DELETE FROM chunks WHERE page_id=? AND kind='stats'", (pid,))
                if sheet:
                    et = texts[pid]
                    cur = conn.execute(
                        "INSERT INTO chunks(page_id, revid, ord, kind, title, section_path, anchor, text, token_est, "
                        "content_hash) VALUES (?, ?, ?, 'stats', ?, 'Stats', NULL, ?, ?, ?)",
                        (pid, row["fetched_revid"], STATS_ORD, row["title"], sheet, token_estimate(sheet),
                         hashlib.sha1(et.encode()).hexdigest()),
                    )
                    conn.execute("INSERT INTO chunk_vec(chunk_id, embedding) VALUES (?, ?)", (cur.lastrowid, vectors[pid]))
                else:
                    stats["no_props"] += 1
                conn.execute(
                    "UPDATE pages SET smw_revid=?, updated_at=? WHERE page_id=?",
                    (row["fetched_revid"], utcnow_iso(), pid),
                )
                stats["props"] += len(props)
        stats["pages"] += len(sheets)
        last_id = rows[-1]["page_id"]
        remaining -= len(rows)
    return stats


def build_catalog(conn: sqlite3.Connection) -> int:
    """Aggregate item_properties into smw_properties (type, #pages, sample values), preserving curated labels."""
    rows = conn.execute(
        "SELECT prop, value_type, count(DISTINCT page_id) AS n FROM item_properties GROUP BY prop, value_type"
    ).fetchall()
    best: dict[str, sqlite3.Row] = {}
    for r in rows:
        if r["prop"] not in best or r["n"] > best[r["prop"]]["n"]:
            best[r["prop"]] = r
    with transaction(conn):
        for prop, r in best.items():
            samples = [
                x[0] for x in conn.execute(
                    "SELECT value_text FROM item_properties WHERE prop=? GROUP BY value_text "
                    "ORDER BY count(*) DESC LIMIT 6", (prop,))
            ]
            sample_txt = ", ".join(s[:30] for s in samples)
            conn.execute(
                "INSERT INTO smw_properties(prop, value_type, page_count, sample_values) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(prop) DO UPDATE SET value_type=excluded.value_type, page_count=excluded.page_count, "
                "sample_values=excluded.sample_values",
                (prop, r["value_type"], r["n"], sample_txt),
            )
    return len(best)


async def ask(client: WikiClient, query: str) -> list[dict[str, Any]]:
    """Run an SMW ask query; returns [{title, url, printouts}]."""
    data = await client.get({"action": "ask", "query": query, "api_version": 2})
    results = data.get("query", {}).get("results", [])
    if isinstance(results, dict):
        results = list(results.values())
    return [
        {"title": r.get("fulltext") or r.get("displaytitle"), "url": r.get("fullurl"), "printouts": r.get("printouts", {})}
        for r in results
    ]


def printout_text(vals: list[Any]) -> str:
    out = []
    for v in vals:
        if isinstance(v, dict):
            out.append(str(v.get("fulltext") or v.get("raw") or v.get("value") or ""))
        else:
            out.append(clean_value(str(v)))
    return ", ".join(x for x in out if x)


def dump(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False)[:2000]
