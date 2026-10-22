"""Index pass: stored HTML -> chunks -> FTS rows + embedding vectors (reusing vectors for unchanged text)."""

from __future__ import annotations

import json
import logging
import sqlite3
import struct
import zlib

from rich.progress import BarColumn, MofNCompleteColumn, Progress, TextColumn, TimeRemainingColumn

from ..config import Settings
from ..db import transaction
from ..llm.ollama import Ollama
from ..text.chunker import Chunk, chunk_page
from ..text.html2md import html_to_page
from ..util import utcnow_iso

log = logging.getLogger("epedia.index")

EMBED_BATCH = 32


def vec_blob(v: list[float]) -> bytes:
    return struct.pack(f"{len(v)}f", *v)


def select_pending(
    conn: sqlite3.Connection, ns: list[int] | None, limit: int, reembed_all: bool, after_id: int = 0
) -> list[sqlite3.Row]:
    where = "is_redirect=0 AND fetched_revid IS NOT NULL AND error_count < 5 AND page_id > ?"
    if not reembed_all:
        where += " AND indexed_revid IS NOT fetched_revid"
    if ns:
        where += f" AND ns IN ({','.join(str(n) for n in ns)})"
    return conn.execute(
        f"SELECT page_id, title, fetched_revid, categories FROM pages WHERE {where} ORDER BY page_id LIMIT ?",
        (after_id, limit),
    ).fetchall()


def build_chunks(settings: Settings, conn: sqlite3.Connection, row: sqlite3.Row) -> list[Chunk]:
    html_row = conn.execute("SELECT html FROM page_html WHERE page_id=?", (row["page_id"],)).fetchone()
    if not html_row:
        return []
    html = zlib.decompress(html_row["html"]).decode("utf-8")
    cats = json.loads(row["categories"] or "[]")
    parsed = html_to_page(html)
    if parsed.soft_redirect is not None:
        return []  # a "Redirect to: X" stub carries no knowledge of its own; classify records the redirect
    return chunk_page(row["title"], parsed.blocks, settings.chunking, cats, parsed.notices)


async def index_pages(
    settings: Settings,
    conn: sqlite3.Connection,
    ollama: Ollama,
    *,
    ns: list[int] | None = None,
    limit: int | None = None,
    reembed_all: bool = False,
    show_progress: bool = True,
) -> dict[str, int]:
    stats = {"pages": 0, "chunks": 0, "embedded": 0, "reused": 0}
    total = conn.execute(
        "SELECT count(*) FROM pages WHERE is_redirect=0 AND fetched_revid IS NOT NULL "
        + ("" if reembed_all else "AND indexed_revid IS NOT fetched_revid ")
        + (f"AND ns IN ({','.join(str(n) for n in ns)})" if ns else "")
    ).fetchone()[0]
    if limit:
        total = min(total, limit)
    remaining = limit if limit else total
    progress = Progress(TextColumn("index"), BarColumn(), MofNCompleteColumn(), TimeRemainingColumn(),
                        disable=not show_progress)
    task = progress.add_task("index", total=total)
    last_id = 0  # keyset pagination: also guarantees --reembed-all visits each page once
    with progress:
        while remaining > 0:
            rows = select_pending(conn, ns, min(EMBED_BATCH, remaining), reembed_all, last_id)
            if not rows:
                break
            await _index_batch(settings, conn, ollama, rows, stats)
            last_id = rows[-1]["page_id"]
            progress.update(task, advance=len(rows))
            remaining -= len(rows)
    return stats


async def _index_batch(
    settings: Settings, conn: sqlite3.Connection, ollama: Ollama, rows: list[sqlite3.Row], stats: dict[str, int]
) -> None:
    per_page: list[tuple[sqlite3.Row, list[Chunk]]] = []
    for r in rows:
        try:
            per_page.append((r, build_chunks(settings, conn, r)))
        except Exception as exc:  # one malformed page must not stop a 100k-page run; record and move on
            log.warning("could not chunk %s: %s", r["title"], exc)
            conn.execute(
                "UPDATE pages SET error_count=error_count+1, last_error=? WHERE page_id=?",
                (f"index: {exc}"[:300], r["page_id"]),
            )
            stats["errors"] = stats.get("errors", 0) + 1
    rows = [r for r, _ in per_page]

    # Existing vectors by content hash so unchanged text is never re-embedded.
    page_ids = [r["page_id"] for r in rows]
    qmarks = ",".join("?" * len(page_ids))
    existing: dict[str, bytes] = {}
    for old in conn.execute(
        f"SELECT c.content_hash, v.embedding FROM chunks c JOIN chunk_vec v ON v.chunk_id=c.chunk_id "
        f"WHERE c.page_id IN ({qmarks})",
        page_ids,
    ):
        existing[old["content_hash"]] = old["embedding"]

    to_embed: dict[str, str] = {}
    for _, chunks in per_page:
        for c in chunks:
            if c.content_hash not in existing and c.content_hash not in to_embed:
                to_embed[c.content_hash] = c.embed_text

    hashes = list(to_embed)
    vectors: dict[str, bytes] = {}
    for i in range(0, len(hashes), EMBED_BATCH):
        batch = hashes[i:i + EMBED_BATCH]
        embs = await ollama.embed([to_embed[h] for h in batch])
        for h, v in zip(batch, embs):
            vectors[h] = vec_blob(v)
    stats["embedded"] += len(hashes)

    with transaction(conn):
        for row, chunks in per_page:
            pid = row["page_id"]
            conn.execute(
                "DELETE FROM chunk_vec WHERE chunk_id IN (SELECT chunk_id FROM chunks WHERE page_id=? AND kind='section')",
                (pid,),
            )
            conn.execute("DELETE FROM chunks WHERE page_id=? AND kind='section'", (pid,))
            for c in chunks:
                cur = conn.execute(
                    "INSERT INTO chunks(page_id, revid, ord, kind, title, section_path, anchor, text, token_est, content_hash) "
                    "VALUES (?, ?, ?, 'section', ?, ?, ?, ?, ?, ?)",
                    (pid, row["fetched_revid"], c.ord, row["title"], c.section_path, c.anchor, c.text, c.token_est,
                     c.content_hash),
                )
                blob = vectors.get(c.content_hash) or existing[c.content_hash]
                if c.content_hash in existing:
                    stats["reused"] += 1
                conn.execute("INSERT INTO chunk_vec(chunk_id, embedding) VALUES (?, ?)", (cur.lastrowid, blob))
            stats["chunks"] += len(chunks)
            conn.execute(
                "UPDATE pages SET indexed_revid=?, updated_at=? WHERE page_id=?",
                (row["fetched_revid"], utcnow_iso(), pid),
            )
    stats["pages"] += len(rows)


def rebuild_titles(conn: sqlite3.Connection) -> int:
    """Rebuild titles_fts from pages + redirects so title matching resolves through redirects."""
    with transaction(conn):
        conn.execute("DELETE FROM titles_fts")
        conn.execute(
            "INSERT INTO titles_fts(title_norm, page_id, is_redirect) "
            "SELECT title_norm, page_id, 0 FROM pages "
            "WHERE is_redirect=0 AND coalesce(page_kind, '') <> 'soft_redirect'"  # no content; the redirect row resolves it
        )
        # Join against pages: a redirect's stored target id outlives its page when that page is deleted or recreated.
        conn.execute(
            "INSERT INTO titles_fts(title_norm, page_id, is_redirect) "
            "SELECT r.from_norm, r.to_page_id, 1 FROM redirects r JOIN pages p ON p.page_id = r.to_page_id"
        )
    return conn.execute("SELECT count(*) FROM titles_fts").fetchone()[0]
