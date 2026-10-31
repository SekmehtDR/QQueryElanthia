"""The closing offer ("Want to hear about X next?"): a page about the same subject as the answer, chosen after it is
written.

The log showed what goes wrong otherwise. Picked from the top-ranked passage before the answer, from every page that
passage's page links to with the most-linked first, then ranked by closeness to the question, the offers were hubs that
everything links to ("Who is Hodierna?" -> Shard, Tirost's lore -> Zoluren four times) and pages that only list the
thing asked about ("What color is Telothian?" -> Drathrok's Duskruin 430/Auction).

So the offer starts from the page the answer cites most, and a candidate must be about the same subject, not just mention
it: the two pages link to each other, share a topic family, or the anchor's opening passage names it. Hubs and listings
are left out, and candidates are ranked by how close they are to what the anchor page is about.
"""

from __future__ import annotations

import re
import sqlite3
import struct
from collections import Counter
from typing import Iterable

from ..config import RetrievalCfg
from ..index.classify import ITEM_NS
from ..kinds import (
    NAMES_A_THING_SQL,
    OFFER_EXCLUDED_PAGE_KINDS,
    OFFER_LISTING_TITLE,
    OFFER_MAX_INLINKS,
    OFFER_MAX_INLINKS_NAMED,
    OFFER_NO_ANCHOR_KINDS,
    OFFER_NOT_AFTER,
    OFFER_ONLY_FOR,
)

OFFER_CHUNKS_PER_PAGE = 6  # only a page's opening chunks are compared: enough to tell what it is about, and fast
OFFER_MAX_LEAD_DISTANCE = 0.40  # cosine distance to the anchor page's opening: how much the two pages are about one thing
OFFER_MAX_DISTANCE = 0.60  # to the question: only what is far from it altogether is ruled out
MIN_OFFER_INLINKS = 8  # a page must be linked from at least this many pages to be a thread worth pulling on
TOPIC_MIN_WEIGHT = 0.8  # family members by category, subpage or a link both ways (a one-way link is 0.4)
_NS_PREFIX = re.compile(r"^[A-Za-z]+:(?=\S)")
ITEM_NS_SQL = ",".join(str(n) for n in sorted(ITEM_NS))


def anchor_title(sources: Iterable[dict], cited: Iterable[int]) -> str | None:
    """The page the answer is about: the one it cites most (the earliest of a tie), else its first source."""
    titles = [s["title"] for s in sources]
    real = [t for t in titles if not t.endswith(": recent edits")]
    counts = Counter(titles[n - 1] for n in cited if 1 <= n <= len(titles) and titles[n - 1] in real)
    if counts:
        best = max(counts.values())
        return next(titles[n - 1] for n in cited if counts.get(titles[n - 1]) == best)
    return real[0] if real else None


def _candidates(conn: sqlite3.Connection, anchor: int) -> dict[int, set[str]]:
    """page_id -> why it is about the same subject as the anchor: "mutual" (the pages link to each other), "family"
    (a topic family they share), "named" (the anchor's opening passage names it)."""
    out: dict[int, set[str]] = {}
    for (pid,) in conn.execute(
            "SELECT a.to_page_id FROM page_links a JOIN page_links b ON b.from_page_id=a.to_page_id "
            "AND b.to_page_id=a.from_page_id WHERE a.from_page_id=?", (anchor,)):
        out.setdefault(pid, set()).add("mutual")
    # the anchor's family (it is the hub), and the hubs whose family it belongs to
    for (pid,) in conn.execute(
            "SELECT page_id FROM page_topics WHERE hub_page_id=? AND weight>=? UNION "
            "SELECT hub_page_id FROM page_topics WHERE page_id=? AND weight>=?",
            (anchor, TOPIC_MIN_WEIGHT, anchor, TOPIC_MIN_WEIGHT)):
        out.setdefault(pid, set()).add("family")
    # pages the opening passage names: what the page itself says it is about
    lead = conn.execute("SELECT text FROM chunks WHERE page_id=? ORDER BY ord LIMIT 1", (anchor,)).fetchone()
    if lead:
        text = lead[0].casefold()
        for pid, title in conn.execute(
                "SELECT p.page_id, p.title FROM page_links l JOIN pages p ON p.page_id=l.to_page_id "
                "WHERE l.from_page_id=?", (anchor,)):
            name = _NS_PREFIX.sub("", title)  # "Concept:Rakash" is named as "Rakash"
            if len(name) > 3 and re.search(r"\b" + re.escape(name.casefold()) + r"\b", text):
                out.setdefault(pid, set()).add("named")
    out.pop(anchor, None)
    return out


def related(conn: sqlite3.Connection, cfg: RetrievalCfg, kind: str, anchor: str | None, used: set[str] | frozenset[str],
            qvec: list[float] | None) -> list[str]:
    """Up to `related_k` titles worth offering after an answer about `anchor`, best first; [] when none fits."""
    if not cfg.offer_related or kind == "meta" or not anchor:
        return []
    row = conn.execute("SELECT page_id, page_kind FROM pages WHERE title=? AND is_redirect=0", (anchor,)).fetchone()
    if not row or row[1] in OFFER_NO_ANCHOR_KINDS:
        return []
    anchor_id, not_after = row[0], OFFER_NOT_AFTER.get(row[1], frozenset())
    found = _candidates(conn, anchor_id)
    if not found:
        return []
    marks = ",".join("?" * len(found))
    rows = conn.execute(
        # how many articles link here: item pages link to their race and shop through templates, which says nothing
        f"SELECT p.page_id, p.title, p.page_kind, (SELECT count(*) FROM page_links x JOIN pages f ON f.page_id=x.from_page_id "
        f"WHERE x.to_page_id=p.page_id AND f.ns NOT IN ({ITEM_NS_SQL}) AND " + NAMES_A_THING_SQL.format(col="f.ns") + ") "
        f"FROM pages p WHERE p.page_id IN ({marks}) AND p.is_redirect=0 AND p.is_log=0 AND p.is_obsolete=0 AND "
        + NAMES_A_THING_SQL.format(col="p.ns"), list(found)).fetchall()
    keep: dict[str, int] = {}
    for pid, title, page_kind, inlinks in rows:
        if title in used or page_kind in OFFER_EXCLUDED_PAGE_KINDS or title.startswith("RanikMap"):
            continue
        if page_kind in not_after or kind not in OFFER_ONLY_FOR.get(page_kind, {kind}):
            continue
        # obscure, or linked from everywhere; a page the anchor's opening names may be better known (the Rakash)
        if not MIN_OFFER_INLINKS <= inlinks <= (OFFER_MAX_INLINKS_NAMED if "named" in found[pid] else OFFER_MAX_INLINKS):
            continue
        if OFFER_LISTING_TITLE.search(title):  # auctions, loot lists, yearly updates: they list things, about nothing
            continue
        # No kind filter: a candidate is already about the same subject, and the thread worth pulling is often of
        # another kind (a weapon's people, a god's city's temple).
        if page_kind in (None, "general") and "mutual" not in found[pid]:  # an unsorted page earns its place by linking back
            continue
        keep[title] = pid
    if not keep:
        return []
    return _closest(conn, keep, anchor_id, qvec)[: cfg.related_k]


def _closest(conn: sqlite3.Connection, pages: dict[str, int], anchor_id: int, qvec: list[float] | None) -> list[str]:
    """Order pages by how close their opening is to the anchor page's own opening; drop those far from either.

    A good next thread is by nature not the question itself ("What is a svidaw sis?" is 0.58 from the Rakash, who are
    0.34 from the svidaw sis page), so the question only rules out what is far from everything."""
    lead = conn.execute("SELECT v.embedding FROM chunks c JOIN chunk_vec v ON v.chunk_id=c.chunk_id "
                        "WHERE c.page_id=? ORDER BY c.ord LIMIT 1", (anchor_id,)).fetchone()

    def distances(blob: bytes, ids: list[int]) -> dict[int, float]:
        marks = ",".join("?" * len(ids))
        return {r[0]: r[1] for r in conn.execute(
            "SELECT c.page_id, min(vec_distance_cosine(v.embedding, ?)) FROM chunks c "
            f"JOIN chunk_vec v ON v.chunk_id=c.chunk_id WHERE c.page_id IN ({marks}) AND c.ord < {OFFER_CHUNKS_PER_PAGE} "
            "GROUP BY c.page_id", (blob, *ids)) if r[1] is not None}

    ids = list(pages.values())
    near = distances(struct.pack(f"{len(qvec)}f", *qvec), ids) if qvec is not None else {}
    like = distances(lead[0], ids) if lead is not None else {}
    ids = [i for i in ids if (qvec is None or near.get(i, 1.0) <= OFFER_MAX_DISTANCE)
           and (lead is None or like.get(i, 1.0) <= OFFER_MAX_LEAD_DISTANCE)]
    by_id = {pid: title for title, pid in pages.items()}
    return [by_id[i] for i in sorted(ids, key=lambda i: (like.get(i, 1.0), near.get(i, 1.0)))]
