"""Build/crawl progress numbers shared by the console monitor and the web status panel."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

NS_NAMES = {0: "Main", 14: "Category", 110: "Armor", 112: "Shield", 114: "Weapon", 116: "Title",
            118: "Item", 120: "Post", 208: "Concept"}
RATE_WINDOW_MIN = 5


def snapshot(conn: sqlite3.Connection) -> dict[str, Any]:
    """Counts per namespace plus a crawl rate measured from the fetch timestamps stored in the database."""
    rows = conn.execute(
        "SELECT ns, count(*) AS pages, "
        "SUM(fetched_revid IS NOT NULL AND fetched_revid = latest_revid) AS fetched, "
        "SUM(indexed_revid IS NOT NULL AND indexed_revid = fetched_revid) AS indexed, "
        "SUM(error_count > 0) AS errors FROM pages WHERE is_redirect=0 GROUP BY ns ORDER BY ns"
    ).fetchall()
    namespaces = [
        {"ns": r["ns"], "name": NS_NAMES.get(r["ns"], str(r["ns"])), "pages": r["pages"],
         "fetched": r["fetched"] or 0, "indexed": r["indexed"] or 0, "errors": r["errors"] or 0}
        for r in rows
    ]
    total = sum(n["pages"] for n in namespaces)
    fetched = sum(n["fetched"] for n in namespaces)
    indexed = sum(n["indexed"] for n in namespaces)
    errors = sum(n["errors"] for n in namespaces)

    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=RATE_WINDOW_MIN)).strftime("%Y-%m-%dT%H:%M:%SZ")
    recent = conn.execute("SELECT count(*) FROM page_html WHERE fetched_at >= ?", (cutoff,)).fetchone()[0]
    per_min = recent / RATE_WINDOW_MIN
    remaining = total - fetched
    last = conn.execute("SELECT max(fetched_at) FROM page_html").fetchone()[0]
    state = {r["key"]: r["value"] for r in conn.execute(
        "SELECT key, value FROM state WHERE key IN ('sync.last_run', 'fullscan.last_run')")}
    return {
        "total": total, "fetched": fetched, "indexed": indexed, "errors": errors,
        "chunks": conn.execute("SELECT count(*) FROM chunks").fetchone()[0],
        "crawl_per_min": round(per_min, 1),
        "eta_minutes": round(remaining / per_min) if per_min > 0 and remaining > 0 else None,
        "crawling": per_min > 0,
        "last_fetch": last,
        "complete": total > 0 and remaining == 0,
        "last_sync": state.get("sync.last_run"),
        "namespaces": namespaces,
    }
