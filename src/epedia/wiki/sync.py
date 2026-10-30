"""Keep the index current: change detection, weekly full scan, and trickle refresh.

Change detection uses `allrevisions` (edits/creations) and `logevents` (deletes/moves/restores) rather than
recentchanges: it works on any MediaWiki, including a mirror whose recentchanges table never saw the edits.
Pages are identified by title, so sync works against either configured source.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any

from ..config import Settings
from ..db import delete_page, get_state, set_state, transaction
from ..llm.ollama import Ollama
from ..util import utcnow_iso
from . import history
from .client import WikiClient
from .crawl import crawl
from .discover import (
    check_primary_source,
    discover,
    newest_change_timestamp,
    upsert_page_info,
    upsert_redirect,
)

log = logging.getLogger("epedia.sync")

TEMPLATE_NS = (10, 828)
EMBEDDED_CAP = 5000


@dataclass
class Changes:
    titles: set[str] = field(default_factory=set)  # content pages to re-check (edited, created, moved, deleted)
    template_titles: set[str] = field(default_factory=set)
    newest: str | None = None
    revisions: int = 0
    moved: int = 0
    deleted_logs: int = 0


async def collect_changes(client: WikiClient, settings: Settings, since: str) -> Changes:
    ch = Changes()
    content_ns = set(settings.wiki.namespaces)
    nslist = "|".join(str(n) for n in sorted(content_ns | set(TEMPLATE_NS)))

    async for data in client.paginate({
        "action": "query", "list": "allrevisions", "arvstart": since, "arvdir": "newer", "arvlimit": 500,
        "arvprop": history.ARV_PROPS, "arvnamespace": nslist,
    }):
        for page in data.get("query", {}).get("allrevisions", []):
            for rev in page.get("revisions", []):
                ch.revisions += 1
                ch.newest = max(ch.newest or "", rev["timestamp"])
                if page["ns"] in TEMPLATE_NS:  # the pages that use it render differently now
                    ch.template_titles.add(page["title"])
                if page["ns"] in content_ns:  # a template or module is also indexed as a page in its own right
                    ch.titles.add(page["title"])

    for logtype in ("delete", "move"):
        async for data in client.paginate({
            "action": "query", "list": "logevents", "letype": logtype, "lestart": since, "ledir": "newer",
            "lelimit": 500, "leprop": "title|timestamp|details",
        }):
            for ev in data.get("query", {}).get("logevents", []):
                ch.newest = max(ch.newest or "", ev["timestamp"])
                ch.titles.add(ev["title"])
                if logtype == "move":
                    ch.moved += 1
                    target = (ev.get("params") or {}).get("target_title")
                    if target:
                        ch.titles.add(target)
                else:
                    ch.deleted_logs += 1
    return ch


async def expand_templates(client: WikiClient, settings: Settings, templates: set[str]) -> tuple[set[str], bool]:
    """Pages that transclude any edited template. Returns (titles, truncated)."""
    out: set[str] = set()
    ns = "|".join(str(n) for n in settings.wiki.namespaces)
    for t in sorted(templates):
        async for data in client.paginate(
            {"action": "query", "list": "embeddedin", "eititle": t, "einamespace": ns, "eilimit": 500}
        ):
            out.update(p["title"] for p in data.get("query", {}).get("embeddedin", []))
            if len(out) > EMBEDDED_CAP:
                return out, True
    return out, False


MAX_DETAIL_ROWS = 5000  # a full scan after a long gap can touch tens of thousands of pages; the list is a courtesy


def record_pages(conn: sqlite3.Connection, sync_id: int, details: list[tuple[str, str]]) -> None:
    """Remember which pages a run touched (new, edited, deleted, re-rendered by a template)."""
    conn.executemany(
        "INSERT INTO sync_pages(sync_id, title, action) VALUES (?, ?, ?)",
        [(sync_id, t, a) for t, a in details[:MAX_DETAIL_ROWS]],
    )


def mark_stale(conn: sqlite3.Connection, titles: set[str], touched: list[str] | None = None) -> int:
    """Force re-fetch + re-index even though the page's own revision id has not changed."""
    n = 0
    for t in titles:
        cur = conn.execute(
            "UPDATE pages SET fetched_revid=NULL, indexed_revid=NULL, smw_revid=NULL WHERE title=? AND is_redirect=0",
            (t,),
        )
        n += cur.rowcount
        if cur.rowcount and touched is not None:
            touched.append(t)
    return n


async def reconcile(
    conn: sqlite3.Connection, client: WikiClient, settings: Settings, ch: Changes, stats: dict[str, int],
    details: list[tuple[str, str]] | None = None,
) -> None:
    """Bring the pages table in line with the source for every title the change feed mentioned."""
    content_ns = set(settings.wiki.namespaces)
    infos: list[dict[str, Any]] = []
    missing_titles: list[str] = []
    titles = sorted(ch.titles)
    for i in range(0, len(titles), 50):
        data = await client.get({"action": "query", "prop": "info", "titles": "|".join(titles[i:i + 50])})
        for p in data.get("query", {}).get("pages", []):
            if p.get("missing") or p.get("invalid"):
                missing_titles.append(p["title"])
            else:
                infos.append(p)

    redirect_titles: list[str] = []
    with transaction(conn):
        for t in missing_titles:
            row = conn.execute("SELECT page_id FROM pages WHERE title=?", (t,)).fetchone()
            if row:
                delete_page(conn, row[0])
                stats["deleted"] += 1
                if details is not None:
                    details.append((t, "deleted"))
            conn.execute("DELETE FROM redirects WHERE from_title=?", (t,))
        for info in infos:
            if info.get("ns") not in content_ns:
                continue
            if info.get("redirect"):
                row = conn.execute("SELECT page_id FROM pages WHERE title=?", (info["title"],)).fetchone()
                if row:
                    delete_page(conn, row[0])
                    stats["deleted"] += 1
                    if details is not None:
                        details.append((info["title"], "deleted"))
                redirect_titles.append(info["title"])
                continue
            existed = conn.execute("SELECT 1 FROM pages WHERE title=?", (info["title"],)).fetchone()
            upsert_page_info(conn, info)
            stats["changed"] += 1
            if details is not None:
                details.append((info["title"], "edited" if existed else "new"))
    # Newly created redirects (e.g. the old name after a move) so title lookups keep resolving.
    for i in range(0, len(redirect_titles), 50):
        data = await client.get(
            {"action": "query", "titles": "|".join(redirect_titles[i:i + 50]), "redirects": 1, "prop": "info"}
        )
        with transaction(conn):
            for r in data.get("query", {}).get("redirects", []):
                upsert_redirect(conn, r["from"], r["to"], r.get("tofragment"))


async def run_pipeline_for_changes(
    settings: Settings, conn: sqlite3.Connection, client: WikiClient, ollama: Ollama, titles: set[str] | None = None
) -> None:
    """Fetch, classify, extract item stats for and index what is pending. `titles` limits the fetching to those pages
    (a delta update: the rest of the backlog is for `fill` to read from the mirror); None fetches everything pending."""
    from ..index.classify import classify_all
    from ..index.enrich import enrich_all
    from ..index.indexer import index_pages, rebuild_titles
    from .smw import smw_pages

    await crawl(conn, client, settings.wiki, show_progress=False, titles=titles)
    classify_all(conn, show_progress=False)
    await smw_pages(settings, conn, client, ollama)
    await index_pages(settings, conn, ollama, show_progress=False)
    enrich_all(conn)
    rebuild_titles(conn)


async def refresh_history(settings: Settings, conn: sqlite3.Connection, titles: set[str] | None = None,
                          limit: int = 500) -> None:
    """Keep edit history current as part of an update: the edits since the last read, and the key facts of character
    pages that changed. Does nothing until the one-off `history` read has been done (that is the bulk request, and the
    owner's call). Reads the real wiki, never the mirror, and never fails an update."""
    from . import timeline
    from .client import open_client

    if not get_state(conn, history.COMPLETE_KEY):
        return
    cfg = settings.wiki.model_copy(update={"source": settings.wiki.delta_source or settings.wiki.source,
                                           "fallback_source": None})
    try:
        async with open_client(cfg) as client:
            await history.backfill(client, conn, list(settings.wiki.namespaces))
            await timeline.fetch_all(client, conn, only=titles, limit=limit)
    except Exception as exc:
        log.warning("edit history not refreshed: %s", exc)


async def sync(
    settings: Settings, conn: sqlite3.Connection, client: WikiClient, ollama: Ollama, *, dry_run: bool = False
) -> dict[str, Any]:
    t0 = time.time()
    since = get_state(conn, "sync.cursor") or get_state(conn, "discover.started_at")
    if not since:
        raise RuntimeError("nothing to sync from: run `epedia discover` first")
    stats: dict[str, Any] = {"source": settings.wiki.source, "changed": 0, "deleted": 0, "moved": 0,
                             "templates": 0, "stale": 0, "errors": 0}
    ch = await collect_changes(client, settings, since)
    stats["templates"] = len(ch.template_titles)
    stats["moved"] = ch.moved
    log.info("changes on %s since %s: %d revisions -> %d pages, %d moves, %d deletions, %d templates",
             settings.wiki.source, since, ch.revisions, len(ch.titles), ch.moved, ch.deleted_logs,
             len(ch.template_titles))
    if dry_run:
        embedded: set[str] = set()
        if ch.template_titles:
            embedded, truncated = await expand_templates(client, settings, ch.template_titles)
            log.info("templates would re-render %d pages%s", len(embedded), " (truncated)" if truncated else "")
        return {**stats, "pages_to_check": len(ch.titles), "pages_rerendered_by_templates": len(embedded)}

    details: list[tuple[str, str]] = []
    if ch.template_titles:
        embedded, truncated = await expand_templates(client, settings, ch.template_titles)
        rerendered: list[str] = []
        with transaction(conn):
            stats["stale"] = mark_stale(conn, embedded, rerendered)
        details.extend((t, "re-rendered") for t in sorted(rerendered))
        if truncated:
            set_state(conn, "fullscan.needed", "1")
            log.warning("template edits touch >%d pages; a full scan is scheduled", EMBEDDED_CAP)
    await reconcile(conn, client, settings, ch, stats, details)
    changed = {t for t, action in details if action != "deleted"}
    await run_pipeline_for_changes(settings, conn, client, ollama, titles=changed)
    await refresh_history(settings, conn, changed)

    # allrevisions' start is inclusive, so the boundary revision is re-read next time; that is harmless.
    new_cursor = ch.newest or since
    set_state(conn, "sync.cursor", new_cursor)
    set_state(conn, "sync.last_run", utcnow_iso())
    with transaction(conn):
        cur = conn.execute(
            "INSERT INTO sync_log(run_at, kind, rc_from, rc_to, changed, deleted, moved, errors, seconds) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (utcnow_iso(), f"rev:{settings.wiki.source}", since, new_cursor, stats["changed"] + stats["stale"],
             stats["deleted"], stats["moved"], stats["errors"], round(time.time() - t0, 1)),
        )
        record_pages(conn, cur.lastrowid, details)
    return stats


def _touched_since_fetch(old: sqlite3.Row, info: dict[str, Any], authoritative: bool) -> bool:
    """Did the source's copy change after we fetched ours? Edit times compare across wikis; revision ids do not."""
    touched, fetched = info.get("touched"), old["fetched_at"]
    if fetched and touched:
        return touched > fetched
    if authoritative:  # same wiki as before: the exact comparison is still available and catches everything
        return old["touched"] != touched or old["latest_revid"] != info.get("lastrevid")
    return not fetched


async def rewind_cursor(conn: sqlite3.Connection, client: WikiClient, margin_hours: int = 1) -> str | None:
    """After reading pages from a source that lags the real wiki, make the next delta start from that source's newest
    edit (less a margin), so edits made after its snapshot are fetched from the real wiki instead of being skipped."""
    from datetime import datetime, timedelta, timezone

    newest = await newest_change_timestamp(client)
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    start = (datetime.strptime(newest, fmt).replace(tzinfo=timezone.utc) - timedelta(hours=margin_hours)).strftime(fmt)
    current = get_state(conn, "sync.cursor")
    if current is None or start < current:
        set_state(conn, "sync.cursor", start)
        log.info("delta cursor wound back to %s (newest edit on the source, less %dh)", start, margin_hours)
        return start
    return current


async def full_scan(
    settings: Settings, conn: sqlite3.Connection, client: WikiClient, ollama: Ollama, *, dry_run: bool = False
) -> dict[str, int]:
    """Re-walk allpages+info and compare with the database: finds new, removed and silently re-rendered pages.

    A page is stale when the source says it was touched after our copy was fetched; edit times are the same on every
    copy of a wiki, unlike revision ids. When the source may lag the real wiki (a mirror, with deltas coming from
    elsewhere) pages missing from it are not deleted, since they may simply not have arrived yet, and the delta
    cursor is wound back to the source's newest edit so the next delta covers everything the source has not seen.
    """
    check_primary_source(conn, settings.wiki)
    authoritative = settings.wiki.delta_source in (None, settings.wiki.source)
    t0 = time.time()
    stats = {"new": 0, "stale": 0, "removed": 0}
    details: list[tuple[str, str]] = []
    for ns in settings.wiki.namespaces:
        seen: set[str] = set()
        async for data in client.paginate({
            "action": "query", "generator": "allpages", "gapnamespace": ns, "gaplimit": 500,
            "gapfilterredir": "nonredirects", "prop": "info",
        }):
            infos = [p for p in data.get("query", {}).get("pages", []) if not p.get("missing")]
            with transaction(conn):
                for info in infos:
                    if ns == 14 and (info.get("length") or 0) < settings.wiki.category_min_bytes:
                        continue
                    seen.add(info["title"])
                    old = conn.execute(
                        "SELECT p.touched, p.latest_revid, h.fetched_at FROM pages p "
                        "LEFT JOIN page_html h ON h.page_id=p.page_id WHERE p.title=?", (info["title"],)
                    ).fetchone()
                    if old is None:
                        stats["new"] += 1
                        details.append((info["title"], "new"))
                        if not dry_run:
                            upsert_page_info(conn, info)
                    elif _touched_since_fetch(old, info, authoritative):
                        stats["stale"] += 1
                        details.append((info["title"], "edited"))
                        if not dry_run:
                            upsert_page_info(conn, info)
                            conn.execute(
                                "UPDATE pages SET fetched_revid=NULL, indexed_revid=NULL, smw_revid=NULL WHERE title=?",
                                (info["title"],),
                            )
        local = [(r[0], r[1]) for r in conn.execute("SELECT page_id, title FROM pages WHERE ns=? AND is_redirect=0", (ns,))]
        gone_rows = [(pid, title) for pid, title in local if title not in seen] if authoritative else []
        gone = [pid for pid, _ in gone_rows]
        stats["removed"] += len(gone)
        details.extend((title, "deleted") for _, title in gone_rows)
        if gone and not dry_run:
            with transaction(conn):
                for pid in gone:
                    delete_page(conn, pid)
    if dry_run:
        return stats
    await discover(conn, client, settings.wiki, force=False)  # fills redirects for any new namespaces only
    await run_pipeline_for_changes(settings, conn, client, ollama)
    await refresh_history(settings, conn)
    if not authoritative:
        await rewind_cursor(conn, client)
    set_state(conn, "fullscan.last_run", utcnow_iso())
    set_state(conn, "fullscan.needed", None)
    with transaction(conn):
        cur = conn.execute(
            "INSERT INTO sync_log(run_at, kind, changed, deleted, moved, errors, seconds) "
            "VALUES (?, 'full', ?, ?, 0, 0, ?)",
            (utcnow_iso(), stats["new"] + stats["stale"], stats["removed"], round(time.time() - t0, 1)),
        )
        record_pages(conn, cur.lastrowid, details)
    return stats


async def refresh_oldest(
    settings: Settings, conn: sqlite3.Connection, client: WikiClient, ollama: Ollama, *, older_than_days: int, limit: int
) -> int:
    """Re-parse the oldest fetched pages: catches output of DPL/SMW lists that changes with no edit at all."""
    rows = conn.execute(
        "SELECT p.title FROM pages p JOIN page_html h ON h.page_id=p.page_id "
        "WHERE h.fetched_at < strftime('%Y-%m-%dT%H:%M:%SZ', 'now', ?) ORDER BY h.fetched_at LIMIT ?",
        (f"-{older_than_days} days", limit),
    ).fetchall()
    with transaction(conn):
        n = mark_stale(conn, {r[0] for r in rows})
    if n:
        await run_pipeline_for_changes(settings, conn, client, ollama)
    return n
