"""Clear the search index so a fresh one can be built from the pages already downloaded.

Only what search needs is removed (chunks, their vectors, the keyword index, the title index) and the pages are
marked as not indexed. The downloaded page HTML, the page list, classification, item stats and conversations are
kept, so rebuilding is local work (`index --reembed-all`, or the Re-index button): no wiki traffic.
"""

from __future__ import annotations

import sqlite3

from ..db import SCHEMA, transaction


def index_size(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        "chunks": conn.execute("SELECT count(*) FROM chunks").fetchone()[0],
        "vectors": conn.execute("SELECT count(*) FROM chunk_vec").fetchone()[0],
        "titles": conn.execute("SELECT count(*) FROM titles_fts").fetchone()[0],
        "pages": conn.execute("SELECT count(*) FROM pages WHERE is_redirect=0").fetchone()[0],
    }


def clear_index(conn: sqlite3.Connection) -> dict[str, int]:
    """Empty the search index; returns what was removed. Safe to repeat. The caller must make sure nothing else is
    writing to the index (a running sync or re-index)."""
    before = index_size(conn)
    # Deleting 200k+ rows one trigger at a time is slow; drop the keyword-index triggers, clear in bulk, put them back.
    with transaction(conn):
        for trigger in ("chunks_ai", "chunks_ad", "chunks_au"):
            conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")
        conn.execute("DELETE FROM chunk_vec")
        conn.execute("DELETE FROM chunks")
        conn.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('delete-all')")
        conn.execute("DELETE FROM titles_fts")
        conn.execute("UPDATE pages SET indexed_revid=NULL")
        conn.execute("DELETE FROM state WHERE key='reindex.progress'")
    conn.executescript(SCHEMA)  # recreates the triggers (every statement in it is IF NOT EXISTS)
    return before
