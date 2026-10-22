"""Structured item questions: validated ItemsQuery -> SMW ask string (live) or local SQL (fallback) -> table passage."""

from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
from typing import Any
from urllib.parse import quote

from ..config import Settings
from ..wiki.client import WikiClient
from ..wiki.smw import ask, printout_text

log = logging.getLogger("epedia.items")

PAGE_TYPES = {"weapon", "armor", "shield", "item", "title"}
OPS = {"=", "!=", ">", ">=", "<", "<=", "~"}
_SAFE_VALUE = re.compile(r"[\[\]|{}<>]")


LIVE_ASK_TIMEOUT_S = 15.0


class ItemsQueryError(ValueError):
    pass


def known_properties(conn: sqlite3.Connection) -> dict[str, str]:
    return {r["prop"]: r["value_type"] for r in conn.execute("SELECT prop, value_type FROM smw_properties")}


def validate(iq: dict[str, Any], props: dict[str, str]) -> dict[str, Any]:
    pt = str(iq.get("page_type", "")).lower()
    if pt not in PAGE_TYPES:
        raise ItemsQueryError(f"bad page_type {pt!r}")
    filters = []
    for f in iq.get("filters") or []:
        prop, op = f.get("property"), f.get("op")
        if prop not in props or op not in OPS:
            raise ItemsQueryError(f"bad filter {f!r}")
        if op in (">", ">=", "<", "<=") and props[prop] != "_num":
            raise ItemsQueryError(f"numeric operator on non-numeric property {prop}")
        value = f.get("value")
        if props[prop] == "_num" and op != "~":
            # The planner may send "10" as a string; a numeric property must compare as a number everywhere
            # (SMW and the local fallback), or '9' would sort after '10'.
            try:
                num = float(str(value).replace(",", "").strip())
            except ValueError:
                raise ItemsQueryError(f"non-numeric value {value!r} for numeric property {prop}") from None
            value = int(num) if num.is_integer() else num
        filters.append({"property": prop, "op": op, "value": value})
    sort = iq.get("sort")
    if sort:
        if sort.get("property") not in props:
            raise ItemsQueryError(f"bad sort {sort!r}")
        sort = {"property": sort["property"], "order": "asc" if sort.get("order") == "asc" else "desc"}
    fields = [x for x in (iq.get("fields") or []) if x in props]
    for extra in ([sort["property"]] if sort else []) + [f["property"] for f in filters]:
        if extra not in fields:
            fields.append(extra)
    limit = max(1, min(int(iq.get("limit") or 10), 25))
    if sort:
        limit = max(limit, 5)  # a ranking question ("the heaviest ...") still needs neighbours for context
    return {"page_type": pt, "filters": filters, "sort": sort, "fields": fields[:6], "limit": limit}


def build_ask(q: dict[str, Any]) -> str:
    """SMW semantics: '>' means >=, '<' means <=; '>>' and '<<' are the strict forms."""
    parts = [f"[[Page_type_is::{q['page_type']}]]"]
    for f in q["filters"]:
        val = _SAFE_VALUE.sub("", str(f["value"]))
        op = f["op"]
        if op == "=":
            parts.append(f"[[{f['property']}::{val}]]")
        elif op == "!=":
            parts.append(f"[[{f['property']}::!{val}]]")
        elif op == "~":
            parts.append(f"[[{f['property']}::~*{val}*]]")
        else:
            smw_op = {">=": ">", ">": ">>", "<=": "<", "<": "<<"}[op]
            parts.append(f"[[{f['property']}::{smw_op}{val}]]")
    parts += [f"|?{p}" for p in q["fields"]]
    if q["sort"]:
        parts.append(f"|sort={q['sort']['property']}|order={q['sort']['order']}")
    parts.append(f"|limit={q['limit']}")
    return "".join(parts)


def local_rows(conn: sqlite3.Connection, q: dict[str, Any]) -> list[dict]:
    """Fallback evaluation over item_properties when the live wiki query is unavailable."""
    where = ["EXISTS (SELECT 1 FROM item_properties t WHERE t.page_id=p.page_id AND t.prop='Page_type_is' "
             "AND t.value_text=?)"]
    args: list[Any] = [q["page_type"]]
    for f in q["filters"]:
        col = "value_num" if isinstance(f["value"], (int, float)) else "value_text"
        op = {"=": "=", "!=": "<>", ">": ">", ">=": ">=", "<": "<", "<=": "<=", "~": "LIKE"}[f["op"]]
        val = f"%{f['value']}%" if f["op"] == "~" else f["value"]
        where.append(
            f"EXISTS (SELECT 1 FROM item_properties t WHERE t.page_id=p.page_id AND t.prop=? AND t.{col} {op} ?)"
        )
        args += [f["property"], val]
    order = ""
    if q["sort"]:
        order = (
            f"ORDER BY (SELECT max(value_num) FROM item_properties s WHERE s.page_id=p.page_id AND s.prop=?) "
            f"{'ASC' if q['sort']['order'] == 'asc' else 'DESC'}"
        )
        args.append(q["sort"]["property"])
    sql = f"SELECT p.page_id, p.title FROM pages p WHERE {' AND '.join(where)} {order} LIMIT ?"
    args.append(q["limit"])
    out = []
    for r in conn.execute(sql, args):
        row = {"title": r["title"], "printouts": {}}
        for prop in q["fields"]:
            vals = [x["value_text"] for x in conn.execute(
                "SELECT value_text FROM item_properties WHERE page_id=? AND prop=?", (r["page_id"], prop))]
            row["printouts"][prop] = vals
        out.append(row)
    return out


def render_table(rows: list[dict], fields: list[str]) -> str:
    head = "| Item | " + " | ".join(re.sub(r"_(is|of)$", "", f).replace("_", " ") for f in fields) + " |"
    sep = "|---|" + "---|" * len(fields)
    lines = [head, sep]
    for r in rows:
        # SMW labels printouts with spaces ("Weight of"); our local fallback keys them with underscores.
        cells = [printout_text(r["printouts"].get(f) or r["printouts"].get(f.replace("_", " "), [])) for f in fields]
        lines.append(f"| {r['title']} | " + " | ".join(cells) + " |")
    return "\n".join(lines)


async def items_passage(settings: Settings, conn: sqlite3.Connection, iq: dict[str, Any]) -> dict | None:
    q = validate(iq, known_properties(conn))
    ask_string = build_ask(q)
    rows: list[dict] = []
    source = "live Semantic MediaWiki results"
    try:
        # Interactive path: one retry and a hard cap, so an unreachable wiki costs seconds, not the background
        # client's two-minute backoff, before the local fallback answers.
        async with WikiClient(settings.wiki, concurrency=1, retries=1) as client:
            rows = await asyncio.wait_for(ask(client, ask_string), timeout=LIVE_ASK_TIMEOUT_S)
    except Exception as exc:
        log.warning("live ask failed (%s); using local index", exc or exc.__class__.__name__)
        rows = local_rows(conn, q)
        source = "results from the locally indexed item stats (may be slightly out of date)"
    if not rows:
        return None
    table = render_table(rows, q["fields"])
    note = '(In the query above, ">" means at least and "<" means at most.)'
    special = settings.wiki.base_url.rstrip("/") + "/Special:Ask?q=" + quote(ask_string.split("|")[0], safe="")
    return {
        "title": "Elanthipedia item query",
        "section": source,
        "url": special,
        "text": f"Query: {ask_string}\n{note}\n{table}",
        "chunk_id": None,
    }
