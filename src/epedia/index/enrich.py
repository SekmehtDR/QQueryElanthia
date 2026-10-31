"""The enrich stage: derive typed facts from indexed pages. Pure code (no network, no model), idempotent, keyed by
revision like every other stage, per extractor (table `enriched`): one page can be read by several, and one having read it
says nothing about the others. `pages.enriched_revid` records the last read of any kind."""

from __future__ import annotations

import logging
import sqlite3
from typing import Any

from ..db import set_state, transaction
from .extract import REGISTRY
from .extract import crafting as _crafting  # noqa: F401  (registers itself)
from .extract import materials as _materials  # noqa: F401  (registers itself)
from .extract import names as _names  # noqa: F401  (registers itself)
from .topics import build as build_topics

log = logging.getLogger(__name__)

SUSPECT_MIN_FACTS = 4  # one line edited off a page is an edit; most of a table vanishing is a layout change


def enrich_all(conn: sqlite3.Connection, force: bool = False) -> dict[str, Any]:
    stats = {"pages": 0, "facts": 0}
    suspect: list[str] = []
    for ex in REGISTRY:
        for title in (ex.pages(conn) if ex.pages else ex.titles):
            page = conn.execute(
                "SELECT page_id, fetched_revid, indexed_revid, (SELECT revid FROM enriched e WHERE e.page_id=p.page_id "
                "AND e.domain=?) AS read_revid FROM pages p WHERE title=? AND is_redirect=0",
                (ex.domain, title)).fetchone()
            # Wait for the index: the extractors read the page's chunks.
            if page is None or page["indexed_revid"] != page["fetched_revid"]:
                continue
            if not force and page["read_revid"] == page["fetched_revid"]:
                continue
            chunks = [(r["section_path"], r["text"]) for r in conn.execute(
                "SELECT section_path, text FROM chunks WHERE page_id=? ORDER BY ord", (page["page_id"],))]
            facts = ex.run(chunks, title) if ex.pages else ex.run(chunks)
            had = conn.execute("SELECT count(*) FROM facts WHERE page_id=? AND domain=?",
                               (page["page_id"], ex.domain)).fetchone()[0]
            if had >= SUSPECT_MIN_FACTS and len(facts) < had / 2:
                # A layout change on the wiki, not an edit: keep what we know and retry next run rather than wipe it.
                log.warning("extractor %s read %d facts from %s, which had %d: kept the old ones", ex.domain, len(facts),
                            title, had)
                suspect.append(title)
                continue
            with transaction(conn):
                conn.execute("DELETE FROM facts WHERE page_id=? AND domain=?", (page["page_id"], ex.domain))
                conn.executemany(
                    "INSERT OR REPLACE INTO facts(page_id, domain, entity, grp, attribute, value_num, value_text, section)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    [(page["page_id"], ex.domain, *f) for f in facts])
                conn.execute("INSERT OR REPLACE INTO enriched(page_id, domain, revid) VALUES (?,?,?)",
                             (page["page_id"], ex.domain, page["fetched_revid"]))
                conn.execute("UPDATE pages SET enriched_revid=? WHERE page_id=?",
                             (page["fetched_revid"], page["page_id"]))
            stats["pages"] += 1
            stats["facts"] += len(facts)
    set_state(conn, "enrich.suspect", "; ".join(suspect) or None)  # `epedia doctor` reports it
    stats["suspect"] = len(suspect)
    stats.update({f"topic_{k}": v for k, v in build_topics(conn, force=force).items()})
    return stats
