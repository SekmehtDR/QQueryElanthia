"""`epedia doctor`: a repeatable integrity and coverage report for the index. Read-only; every check is one query, and a
check that finds a problem says what it means, so a failing line is a lead rather than a puzzle."""

from __future__ import annotations

import math
import sqlite3
import struct
from dataclasses import dataclass

from ..db import get_state


@dataclass
class Check:
    name: str
    ok: bool
    detail: str


def _one(conn: sqlite3.Connection, sql: str, *args) -> int:
    return conn.execute(sql, args).fetchone()[0]


def _vector_norms(conn: sqlite3.Connection, sample: int = 200) -> list[float]:
    """Lengths of a sample of stored embeddings. retrieval/search.py converts vec0's L2 distance to cosine distance,
    which is only right for unit vectors (Ollama's nomic-embed-text returns them normalised)."""
    out = []
    for (cid,) in conn.execute("SELECT chunk_id FROM chunks ORDER BY random() LIMIT ?", (sample,)).fetchall():
        row = conn.execute("SELECT embedding FROM chunk_vec WHERE chunk_id=?", (cid,)).fetchone()
        if row and row[0]:
            vals = struct.unpack(f"{len(row[0]) // 4}f", row[0])
            out.append(math.sqrt(sum(x * x for x in vals)))
    return out


def run(conn: sqlite3.Connection) -> list[Check]:
    out: list[Check] = []

    def add(name: str, ok: bool, detail: str = "") -> None:
        out.append(Check(name, ok, detail))

    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    add("SQLite integrity_check", integrity == "ok", integrity)
    add("foreign keys", not conn.execute("PRAGMA foreign_key_check").fetchall())

    live = "is_redirect=0"
    for label, col in (("fetched", "fetched_revid"), ("indexed", "indexed_revid"), ("classified", "classified_revid")):
        lag = _one(conn, f"SELECT count(*) FROM pages WHERE {live} AND fetched_revid IS NOT NULL AND "
                         f"({col} IS NULL OR {col} != fetched_revid)") if col != "fetched_revid" else _one(
            conn, f"SELECT count(*) FROM pages WHERE {live} AND (fetched_revid IS NULL OR "
                  "latest_revid IS NOT NULL AND fetched_revid != latest_revid)")
        add(f"pages {label} at their latest revision", lag == 0, f"{lag:,} behind" if lag else "")
    errors = _one(conn, "SELECT count(*) FROM pages WHERE error_count > 0")
    add("pages without fetch errors", errors == 0, f"{errors:,} with errors" if errors else "")

    chunks = _one(conn, "SELECT count(*) FROM chunks")
    fts = _one(conn, "SELECT count(*) FROM chunks_fts")
    vec = _one(conn, "SELECT count(*) FROM chunk_vec")
    add("full-text rows = chunks", fts == chunks, f"{fts:,} vs {chunks:,}")
    add("vector rows = chunks", vec == chunks, f"{vec:,} vs {chunks:,}")
    norms = _vector_norms(conn)
    add("vectors are unit length", all(abs(n - 1) < 0.01 for n in norms),
        f"{len(norms)} sampled, {min(norms):.3f} to {max(norms):.3f}: search reads L2 distance as cosine, which needs "
        "unit vectors" if norms else "none sampled")
    orphans = _one(conn, "SELECT count(*) FROM chunks WHERE page_id NOT IN (SELECT page_id FROM pages)")
    add("no orphan chunks", orphans == 0, f"{orphans:,} orphans" if orphans else "")
    empty = _one(conn, "SELECT count(*) FROM chunks WHERE text IS NULL OR length(trim(text)) = 0")
    add("no empty chunks", empty == 0, f"{empty:,} empty" if empty else "")
    # A File page is often just the image, and talk and user pages are often blank: only articles are expected to have text.
    nochunk = _one(
        conn, "SELECT count(*) FROM pages p WHERE is_redirect=0 AND indexed_revid IS NOT NULL AND page_kind NOT IN "
              "('soft_redirect', 'file', 'talk', 'user', 'site') AND NOT EXISTS (SELECT 1 FROM chunks k WHERE k.page_id=p.page_id)")
    add("indexed pages have chunks", nochunk < 100, f"{nochunk:,} indexed pages with no chunks (stubs are normal)")

    # Link graph coverage for the pages that carry knowledge (generic items are mostly bare).
    row = conn.execute(
        "SELECT count(*), sum(EXISTS(SELECT 1 FROM page_links l WHERE l.from_page_id=p.page_id)) FROM pages p "
        "WHERE is_redirect=0 AND page_kind IN ('crafting','guide','mechanics','skill','guild','spell','creature',"
        "'material','recipe','general')").fetchone()
    pct = 100 * (row[1] or 0) / row[0] if row[0] else 0
    add("link graph covers knowledge pages", pct >= 70, f"{pct:.0f}% of {row[0]:,} pages have outgoing links")

    # Typed facts: every table row on the materials page should be a fact, and every enriched page current.
    facts = _one(conn, "SELECT count(*) FROM facts")
    materials = _one(conn, "SELECT count(DISTINCT entity) FROM facts WHERE domain='material'")
    add("material facts extracted", materials >= 250, f"{facts:,} facts for {materials:,} materials")
    stale = _one(conn, "SELECT count(*) FROM pages p WHERE EXISTS (SELECT 1 FROM facts f WHERE f.page_id=p.page_id) "
                       "AND (enriched_revid IS NULL OR enriched_revid != fetched_revid)")
    add("facts are current", stale == 0, f"{stale:,} source pages changed since their facts were read" if stale else "")

    disciplines = _one(conn, "SELECT count(*) FROM facts WHERE domain='crafting' AND attribute='discipline_of'")
    add("crafting disciplines read from the Crafting page", disciplines >= 5, f"{disciplines} disciplines")

    suspect = get_state(conn, "enrich.suspect")
    add("extractors still read what they used to", not suspect,
        f"read far fewer facts than before from: {suspect}. The wiki's layout may have changed; the old facts were kept" if suspect else "")

    checked, wrong, examples = crosscheck_materials(conn)
    add("material tables agree with the material pages", wrong == 0 and checked > 0,
        f"{checked:,} material pages compared; " + (f"{wrong:,} differ, e.g. {'; '.join(examples)}" if wrong else "all match"))

    hubs = _one(conn, "SELECT count(*) FROM topic_hubs")
    members = _one(conn, "SELECT count(*) FROM page_topics")
    add("topic graph built", hubs >= 100 and members > 0, f"{hubs:,} hubs, {members:,} memberships")

    # Rules the answers depend on must still be on the indexed pages that state them.
    for title, needle in (("Forging guide", 'cap" at 7.5 density'), ("Category:Crafting materials", "Hardness affects")):
        found = _one(conn, "SELECT count(*) FROM chunks WHERE title=? AND text LIKE ?", title, f"%{needle}%")
        add(f"rule still on {title}", found > 0, f'"{needle}"' if found else f'"{needle}" not found: update core/materials.py')
    return out


def crosscheck_materials(conn: sqlite3.Connection) -> tuple[int, int, list[str]]:
    """Each material page repeats its own stats; compare them with the facts read from the Category:Crafting materials
    tables. Returns (pages compared, pages that differ, a few examples). A mismatch means one of the two is stale."""
    from .extract import number, tables
    from .extract.materials import ATTRIBUTES

    facts: dict[str, dict[str, float]] = {}
    for r in conn.execute("SELECT entity, attribute, value_num FROM facts WHERE domain='material' AND value_num IS NOT NULL"):
        facts.setdefault(r["entity"].casefold(), {})[r["attribute"]] = r["value_num"]
    checked = wrong = 0
    examples: list[str] = []
    for page in conn.execute("SELECT page_id, title FROM pages WHERE page_kind='material' AND is_redirect=0"):
        known = facts.get(page["title"].casefold())
        if known is None:
            continue
        own: dict[str, float] = {}
        for text in (r[0] for r in conn.execute("SELECT text FROM chunks WHERE page_id=? ORDER BY ord", (page["page_id"],))):
            for _above, header, rows in tables(text):
                if len(rows) != 1:
                    continue  # a stats table is one row under named columns
                for h, cell in zip(header, rows[0], strict=False):
                    attr = ATTRIBUTES.get(h.strip().lower())
                    if attr and number(cell) is not None:
                        own.setdefault(attr, number(cell))
        diffs = [f"{a} {own[a]:g} vs {known[a]:g}" for a in own if a in known and abs(own[a] - known[a]) > 0.01]
        if not own:
            continue
        checked += 1
        if diffs:
            wrong += 1
            if len(examples) < 3:
                examples.append(f"{page['title']}: {', '.join(diffs[:2])}")
    return checked, wrong, examples
