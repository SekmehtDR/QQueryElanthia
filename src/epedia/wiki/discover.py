"""Discover pass: enumerate pages and redirects per namespace via allpages + prop=info."""

from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any

from ..config import WikiCfg
from ..db import get_state, set_state, transaction
from ..retrieval.titles import normalize
from ..util import utcnow_iso
from .client import WikiClient

log = logging.getLogger("epedia.discover")


def upsert_page_info(conn: sqlite3.Connection, info: dict[str, Any]) -> int:
    """Insert or refresh a page row from a prop=info record; returns our internal page_id.

    A page is identified by its title, not the wiki's page id: ids and revision numbers differ between the
    original site and a mirror, and we must be able to read from either. page_id is purely internal.
    """
    title = info["title"]
    conn.execute(
        """
        INSERT INTO pages(ns, title, title_norm, latest_revid, length, touched, is_redirect, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(title) DO UPDATE SET
          ns=excluded.ns, title_norm=excluded.title_norm,
          latest_revid=excluded.latest_revid, length=excluded.length, touched=excluded.touched,
          is_redirect=excluded.is_redirect, updated_at=excluded.updated_at
        """,
        (
            info["ns"],
            title,
            normalize(title),
            info.get("lastrevid"),
            info.get("length"),
            info.get("touched"),
            1 if info.get("redirect") else 0,
            utcnow_iso(),
        ),
    )
    return conn.execute("SELECT page_id FROM pages WHERE title=?", (title,)).fetchone()[0]


def upsert_redirect(conn: sqlite3.Connection, from_title: str, to_title: str, fragment: str | None) -> None:
    row = conn.execute("SELECT page_id FROM pages WHERE title=?", (to_title,)).fetchone()
    conn.execute(
        """
        INSERT INTO redirects(from_title, from_norm, to_title, to_page_id, to_fragment) VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(from_title) DO UPDATE SET from_norm=excluded.from_norm, to_title=excluded.to_title,
          to_page_id=excluded.to_page_id, to_fragment=excluded.to_fragment
        """,
        (from_title, normalize(from_title), to_title, row[0] if row else None, fragment),
    )


async def newest_change_timestamp(client: WikiClient) -> str:
    """Timestamp of the newest revision on this source (recent-changes is not reliable on a mirror)."""
    data = await client.get(
        {"action": "query", "list": "allrevisions", "arvdir": "older", "arvlimit": 1, "arvprop": "timestamp"}
    )
    return data["query"]["allrevisions"][0]["revisions"][0]["timestamp"]


def check_primary_source(conn: sqlite3.Connection, cfg: WikiCfg, *, rebase: bool = False) -> None:
    """Revision ids are only comparable within one wiki, so full walks must use the same source as before.

    Incremental `sync` may use either source (it only touches pages that changed); anything that compares or
    rewrites every page's revision must run against the primary source, or switch it deliberately with --force.
    """
    current = get_state(conn, "primary_source")
    if current is None or rebase:
        set_state(conn, "primary_source", cfg.source)
        if rebase and current not in (None, cfg.source):
            log.warning("primary source changed %s -> %s; pages will be re-fetched from the new source",
                        current, cfg.source)
    elif current != cfg.source:
        raise SourceMismatch(
            f"this database was built from source {current!r} but the active source is {cfg.source!r}. "
            f"Run with `--source {current}`, or add --force to make {cfg.source!r} the primary source "
            f"(every page is then re-fetched)."
        )


class SourceMismatch(RuntimeError):
    pass


async def adopt_fetched(conn: sqlite3.Connection, client: WikiClient) -> dict[str, int]:
    """Keep pages fetched from another source whose content is provably still current.

    After the primary source changes, every page's revision id is in a new numbering, so all fetched pages look
    stale. But a revision's timestamp is the same on the original and a mirror: if the new source's latest
    revision of a page is no newer than the moment we fetched it, our stored copy already contains it. Those
    pages are re-labelled with the new revision id instead of being downloaded and re-embedded again.
    """
    rows = conn.execute(
        "SELECT p.title, h.fetched_at FROM pages p JOIN page_html h ON h.page_id=p.page_id "
        "WHERE p.is_redirect=0 AND p.fetched_revid IS NOT NULL AND p.fetched_revid IS NOT p.latest_revid"
    ).fetchall()
    stats = {"checked": len(rows), "adopted": 0, "stale": 0, "missing": 0}
    for i in range(0, len(rows), 50):
        batch = {r["title"]: r["fetched_at"] for r in rows[i:i + 50]}
        data = await client.get({"action": "query", "prop": "revisions", "rvprop": "timestamp",
                                 "titles": "|".join(batch)})
        keep: list[str] = []
        for page in data.get("query", {}).get("pages", []):
            if page.get("missing") or not page.get("revisions"):
                stats["missing"] += 1
            elif page["revisions"][0]["timestamp"] <= batch.get(page["title"], ""):
                keep.append(page["title"])
            else:
                stats["stale"] += 1
        with transaction(conn):
            for title in keep:
                conn.execute(
                    "UPDATE pages SET "
                    "indexed_revid = CASE WHEN indexed_revid = fetched_revid THEN latest_revid ELSE indexed_revid END, "
                    "smw_revid = CASE WHEN smw_revid = fetched_revid THEN latest_revid ELSE smw_revid END, "
                    "fetched_revid = latest_revid WHERE title=?",
                    (title,),
                )
        stats["adopted"] += len(keep)
    return stats


async def discover(
    conn: sqlite3.Connection,
    client: WikiClient,
    cfg: WikiCfg,
    namespaces: list[int] | None = None,
    *,
    force: bool = False,
) -> dict[str, int]:
    ns_list = namespaces or cfg.namespaces
    check_primary_source(conn, cfg, rebase=force)
    if get_state(conn, "discover.started_at") is None:
        ts = await newest_change_timestamp(client)
        set_state(conn, "discover.started_at", ts)
        log.info("discover.started_at = %s", ts)

    totals = {"pages": 0, "redirects": 0}
    for ns in ns_list:
        done_key = f"discover.{ns}.done_at"
        if get_state(conn, done_key) and not force:
            log.info("ns %s already discovered (%s); skipping", ns, get_state(conn, done_key))
            continue
        if force:
            for suffix in ("pages.continue", "redir.continue", "done_at"):
                set_state(conn, f"discover.{ns}.{suffix}", None)
        totals["pages"] += await _walk(conn, client, cfg, ns, redirects=False)
        totals["redirects"] += await _walk(conn, client, cfg, ns, redirects=True)
        set_state(conn, done_key, utcnow_iso())
        log.info("ns %s discovered", ns)
    if force:
        log.info("adopt: %s", await adopt_fetched(conn, client))
    return totals


async def _walk(conn: sqlite3.Connection, client: WikiClient, cfg: WikiCfg, ns: int, *, redirects: bool) -> int:
    key = f"discover.{ns}.{'redir' if redirects else 'pages'}.continue"
    params: dict[str, Any] = {
        "action": "query",
        "generator": "allpages",
        "gapnamespace": ns,
        "gaplimit": 500,
        "gapfilterredir": "redirects" if redirects else "nonredirects",
        "prop": "info",
    }
    saved = get_state(conn, key)
    if saved == "done":
        return 0
    if saved:
        params.update(json.loads(saved))

    count = 0
    while True:
        data = await client.get(params)
        query = data.get("query", {})
        # allpages cannot combine with redirects=1, so resolve the listed redirect titles separately.
        resolved: list[dict[str, Any]] = []
        if redirects:
            titles = [p["title"] for p in query.get("pages", [])]
            for i in range(0, len(titles), 50):
                r = await client.get(
                    {"action": "query", "titles": "|".join(titles[i:i + 50]), "redirects": 1, "prop": "info"}
                )
                resolved.append(r.get("query", {}))
        with transaction(conn):
            if redirects:
                for q in resolved:
                    for r in q.get("redirects", []):
                        upsert_redirect(conn, r["from"], r["to"], r.get("tofragment"))
                        count += 1
                    # Resolved targets ride along in pages[]; keep the ones in our namespaces.
                    for info in q.get("pages", []):
                        if info.get("missing") or info.get("ns") not in cfg.namespaces:
                            continue
                        upsert_page_info(conn, info)
                # Fill in to_page_id for redirects whose targets we just inserted.
                conn.execute(
                    "UPDATE redirects SET to_page_id=(SELECT page_id FROM pages WHERE pages.title=redirects.to_title) "
                    "WHERE to_page_id IS NULL"
                )
            else:
                for info in query.get("pages", []):
                    if info.get("missing"):
                        continue
                    if ns == 14 and (info.get("length") or 0) < cfg.category_min_bytes:
                        continue
                    upsert_page_info(conn, info)
                    count += 1
            cont = data.get("continue")
            set_state(conn, key, json.dumps(cont) if cont else "done")
        if not cont:
            break
        params.update(cont)
        log.debug("ns %s %s: %d so far (%s)", ns, "redirects" if redirects else "pages", count, cont.get("gapcontinue"))
    return count
