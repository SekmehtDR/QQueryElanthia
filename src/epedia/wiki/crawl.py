"""Crawl pass: fetch rendered HTML (action=parse) for pages whose stored revision lags the wiki."""

from __future__ import annotations

import asyncio
import json
import logging
import signal
import sqlite3
import zlib
from typing import Any

from rich.progress import BarColumn, MofNCompleteColumn, Progress, TextColumn, TimeRemainingColumn

from ..config import WikiCfg
from ..db import delete_page, transaction
from ..util import utcnow_iso
from .client import WikiClient, WikiError

log = logging.getLogger("epedia.crawl")

PARSE_PROPS = "text|sections|categories|displaytitle|revid"


SCOPE = "AND title IN (SELECT title FROM crawl_scope)"  # a crawl limited to named pages (see crawl(titles=...))


def select_pending(
    conn: sqlite3.Connection, cfg: WikiCfg, *, ns: list[int] | None, limit: int, retry_errors: bool, scoped: bool = False
) -> list[sqlite3.Row]:
    order = ns or cfg.namespaces
    case = " ".join(f"WHEN {n} THEN {i}" for i, n in enumerate(order))
    where_ns = f"AND ns IN ({','.join(str(n) for n in order)})"
    err = "" if retry_errors else "AND error_count < 5"
    return conn.execute(
        f"""
        SELECT page_id, ns, title FROM pages
        WHERE is_redirect=0 {where_ns} {err} {SCOPE if scoped else ""}
          AND fetched_revid IS NOT latest_revid
        ORDER BY CASE ns {case} ELSE 99 END, page_id
        LIMIT ?
        """,
        (limit,),
    ).fetchall()


def count_pending(conn: sqlite3.Connection, cfg: WikiCfg, ns: list[int] | None, retry_errors: bool,
                  scoped: bool = False) -> int:
    order = ns or cfg.namespaces
    where_ns = f"AND ns IN ({','.join(str(n) for n in order)})"
    err = "" if retry_errors else "AND error_count < 5"
    return conn.execute(
        f"SELECT count(*) FROM pages WHERE is_redirect=0 {where_ns} {err} {SCOPE if scoped else ''} "
        "AND fetched_revid IS NOT latest_revid"
    ).fetchone()[0]


async def fetch_page(client: WikiClient, title: str) -> dict[str, Any] | None:
    """Return the parse payload, or None if the page no longer exists."""
    try:
        data = await client.get(
            {
                "action": "parse",
                "page": title,
                "prop": PARSE_PROPS,
                "disabletoc": 1,
                "disablelimitreport": 1,
                "disableeditsection": 1,
            }
        )
    except WikiError as exc:
        if exc.code in ("missingtitle", "nosuchpageid"):
            return None
        raise
    return data["parse"]


def store_parse(conn: sqlite3.Connection, page_id: int, parse: dict[str, Any]) -> None:
    categories = [c["category"].replace("_", " ") for c in parse.get("categories", [])]
    text = parse.get("text", "")
    is_disambig = int(any("disambig" in c.lower() for c in categories) or "may refer to" in text[:2000].lower())
    revid = parse["revid"]
    conn.execute(
        """
        INSERT INTO page_html(page_id, revid, html, sections, fetched_at) VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(page_id) DO UPDATE SET revid=excluded.revid, html=excluded.html,
          sections=excluded.sections, fetched_at=excluded.fetched_at
        """,
        (page_id, revid, zlib.compress(text.encode("utf-8"), 6), json.dumps(parse.get("sections", [])), utcnow_iso()),
    )
    conn.execute(
        """
        UPDATE pages SET fetched_revid=?, latest_revid=?, display_title=?,
          categories=?, is_disambig=?, error_count=0, last_error=NULL, updated_at=?
        WHERE page_id=?
        """,
        (revid, revid, parse.get("displaytitle"), json.dumps(categories), is_disambig, utcnow_iso(), page_id),
    )


async def crawl(
    conn: sqlite3.Connection,
    client: WikiClient,
    cfg: WikiCfg,
    *,
    ns: list[int] | None = None,
    limit: int | None = None,
    retry_errors: bool = False,
    batch_size: int = 200,
    show_progress: bool = True,
    titles: set[str] | None = None,
) -> dict[str, int]:
    """Fetch pending pages in batches. First Ctrl-C finishes the batch in flight and stops; a second interrupts.

    `titles` limits the crawl to those pages (a delta update fetches only what it found changed; the rest of the
    backlog is the job of `fill`, which reads from the mirror)."""
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()

    def on_sigint() -> None:
        stop.set()
        loop.remove_signal_handler(signal.SIGINT)  # restore the default so a second Ctrl-C interrupts at once

    installed = False
    try:
        loop.add_signal_handler(signal.SIGINT, on_sigint)
        installed = True
    except (NotImplementedError, RuntimeError):
        pass  # Windows event loops: KeyboardInterrupt propagates instead; batches commit atomically anyway.
    try:
        if titles is not None:
            conn.execute("CREATE TEMP TABLE IF NOT EXISTS crawl_scope(title TEXT PRIMARY KEY)")
            conn.execute("DELETE FROM crawl_scope")
            conn.executemany("INSERT OR IGNORE INTO crawl_scope(title) VALUES (?)", [(t,) for t in titles])
        return await _crawl_loop(conn, client, cfg, stop, ns, limit, retry_errors, batch_size, show_progress,
                                 scoped=titles is not None)
    finally:
        if titles is not None:
            conn.execute("DROP TABLE IF EXISTS crawl_scope")
        if installed:  # never leave our handler behind: later stages (smw, index) must stay interruptible
            try:
                loop.remove_signal_handler(signal.SIGINT)
            except (NotImplementedError, RuntimeError, ValueError):
                pass


async def _crawl_loop(
    conn: sqlite3.Connection,
    client: WikiClient,
    cfg: WikiCfg,
    stop: asyncio.Event,
    ns: list[int] | None,
    limit: int | None,
    retry_errors: bool,
    batch_size: int,
    show_progress: bool,
    scoped: bool = False,
) -> dict[str, int]:
    stats = {"fetched": 0, "deleted": 0, "errors": 0}
    total = count_pending(conn, cfg, ns, retry_errors, scoped)
    if limit:
        total = min(total, limit)
    progress = Progress(
        TextColumn("crawl"), BarColumn(), MofNCompleteColumn(), TimeRemainingColumn(), disable=not show_progress
    )
    task = progress.add_task("crawl", total=total)
    remaining = limit if limit else total
    with progress:
        while remaining > 0 and not stop.is_set():
            rows = select_pending(conn, cfg, ns=ns, limit=min(batch_size, remaining), retry_errors=retry_errors,
                                  scoped=scoped)
            if not rows:
                break
            results = await asyncio.gather(*(_one(client, r["title"]) for r in rows))
            with transaction(conn):
                for row, res in zip(rows, results):
                    pid = row["page_id"]
                    if isinstance(res, Exception):
                        stats["errors"] += 1
                        conn.execute(
                            "UPDATE pages SET error_count=error_count+1, last_error=?, updated_at=? WHERE page_id=?",
                            (str(res)[:300], utcnow_iso(), pid),
                        )
                    elif res is None:
                        stats["deleted"] += 1
                        delete_page(conn, pid)
                    else:
                        stats["fetched"] += 1
                        store_parse(conn, pid, res)
            progress.update(task, advance=len(rows))
            remaining -= len(rows)
    return stats


async def _one(client: WikiClient, title: str) -> dict[str, Any] | None | Exception:
    try:
        return await fetch_page(client, title)
    except Exception as exc:  # recorded per page so one bad page cannot stall the crawl
        log.warning("page %r failed: %s", title, exc)
        return exc
