"""Hybrid retrieval: FTS5 BM25 + sqlite-vec KNN fused with Reciprocal Rank Fusion, plus title boosts."""

from __future__ import annotations

import re
import sqlite3
import struct
from dataclasses import dataclass, field

from ..config import RetrievalCfg
from ..kinds import namespace_weight
from ..util import page_url
from .titles import GAME_WORDS, normalize, query_as_title, tokens

STOPWORDS = {
    "a", "an", "the", "is", "are", "was", "were", "of", "to", "in", "on", "for", "and", "or", "do", "does",
    "did", "how", "what", "who", "where", "when", "which", "why", "can", "i", "you", "me", "my", "it", "its",
    "with", "at", "by", "from", "that", "this", "be", "as", "about", "tell", "get", "much", "many",
}


GENERIC_TITLE_WORDS = {"skill", "command", "spell", "guide", "ability", "stat"}


@dataclass
class Hit:
    chunk_id: int
    page_id: int
    ns: int
    title: str
    section_path: str
    anchor: str | None
    text: str
    kind: str
    score: float = 0.0
    fts_rank: int | None = None
    vec_rank: int | None = None
    title_match: str = ""
    url: str = ""
    page_kind: str | None = None
    is_log: bool = False
    is_obsolete: bool = False
    weight: float = 1.0  # combined multiplier applied to the rank-fusion score (for --explain)
    extras: dict = field(default_factory=dict)
    vec_dist: float | None = None  # cosine distance to the question (vectors are unit length), when vector search found it


def score_chunk(
    rrf: float,
    cfg: RetrievalCfg,
    *,
    question_kind: str | None,
    ns: int,
    page_kind: str | None,
    is_disambig: bool,
    is_log: bool,
    is_obsolete: bool,
    title_label: str,
    partial_weight: float,
    in_prior: bool,
) -> tuple[float, float]:
    """Final score and the combined multiplier.

    Multipliers (namespace, disambiguation, question kind x page kind, logs, obsolete) scale the rank-fusion score;
    the additive boosts (exact/partial title match, previous-answer continuity) come after, so a page whose title
    is exactly what the user named still wins even when its kind is not the favoured one.
    """
    weight = cfg.ns_weights.get(str(ns), namespace_weight(ns))  # discussion and wiki machinery rank below articles
    if is_disambig:
        weight *= cfg.disambig_weight
    if question_kind and page_kind:
        weight *= cfg.kind_weights.get(question_kind, {}).get(page_kind, 1.0)
    if is_log:
        weight *= cfg.log_weight
    if is_obsolete:
        weight *= cfg.obsolete_weight
    score = rrf * weight
    if title_label == "exact":
        score += cfg.title_exact_boost
    elif title_label == "partial":
        score += cfg.title_partial_boost * partial_weight
    if in_prior:
        score += cfg.prior_boost
    return score, weight


def _fts_query(query: str, mode: str) -> str | None:
    toks = [t for t in tokens(query) if t not in STOPWORDS and len(t) > 1]
    if not toks:
        toks = tokens(query)
    if not toks:
        return None
    quoted = [f'"{t.replace(chr(34), "")}"' for t in toks]
    return (" AND " if mode == "and" else " OR ").join(quoted)


def fts_search(conn: sqlite3.Connection, query: str, k: int) -> list[int]:
    for mode in ("and", "or"):
        q = _fts_query(query, mode)
        if not q:
            return []
        try:
            rows = conn.execute(
                "SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH ? ORDER BY bm25(chunks_fts, 5.0, 2.0, 1.0) LIMIT ?",
                (q, k),
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        if len(rows) >= 5 or mode == "or":
            return [r[0] for r in rows]
    return []


def vec_search_dist(conn: sqlite3.Connection, qvec: list[float], k: int) -> list[tuple[int, float]]:
    """(chunk_id, cosine distance) nearest first. vec0 measures L2; the embeddings are unit length (`epedia doctor`
    checks), so cosine distance is L2 squared over two."""
    blob = struct.pack(f"{len(qvec)}f", *qvec)
    rows = conn.execute(
        "SELECT chunk_id, distance FROM chunk_vec WHERE embedding MATCH ? AND k = ? ORDER BY distance", (blob, k)
    ).fetchall()
    return [(r[0], r[1] * r[1] / 2) for r in rows]


def vec_search(conn: sqlite3.Connection, qvec: list[float], k: int) -> list[int]:
    return [cid for cid, _ in vec_search_dist(conn, qvec, k)]


def title_matches(conn: sqlite3.Connection, query: str) -> tuple[dict[int, str], dict[int, float]]:
    """Return (exact page_ids -> label, partial page_ids -> scaled boost weight)."""
    exact: dict[int, str] = {}
    partial: dict[int, float] = {}
    candidates = {normalize(query), query_as_title(query)}
    for cand in [c for c in candidates if c]:
        # "stealth" should find the page titled "Stealth skill", "choke" -> "Choke command".
        for variant in (cand, f"{cand} skill", f"{cand} command"):
            # `title_norm = ?` on an FTS5 column cannot use the index (a full scan of ~125k rows, six times per
            # question). A phrase MATCH uses it; equality is then confirmed on the few candidates it returns.
            phrase = '"' + variant.replace('"', '""') + '"'
            try:
                rows = conn.execute(
                    "SELECT page_id, title_norm FROM titles_fts WHERE titles_fts MATCH ? LIMIT 100", (phrase,)
                ).fetchall()
            except sqlite3.OperationalError:  # a query with no indexable tokens (punctuation only)
                continue
            for r in rows:
                if r["title_norm"] == variant:
                    exact[r[0]] = "exact"
    qtoks = [t for t in tokens(query) if t not in STOPWORDS]
    if qtoks:
        match = " OR ".join(f'"{t}"' for t in qtoks[:12])
        rows = conn.execute(
            "SELECT page_id, title_norm FROM titles_fts WHERE titles_fts MATCH ? LIMIT 200", (match,)
        ).fetchall()
        scored: list[tuple[float, int]] = []
        qset = set(tokens(query))
        for r in rows:
            ttoks = tokens(r["title_norm"])
            if len(r["title_norm"]) < 4 or not ttoks or r[0] in exact:
                continue
            if set(ttoks) <= GAME_WORDS and len(qtoks) > len(ttoks):
                continue
            # Words like "skill" or "command" are page-type labels, not part of what the user asks about.
            core = [t for t in ttoks if t not in GENERIC_TITLE_WORDS] or ttoks
            if all(t in qset for t in core):
                scored.append((0.6 + 0.4 * min(1.0, len(core) / max(len(qtoks), 1)), r[0]))
        for w, pid in sorted(scored, reverse=True)[:3]:
            partial[pid] = min(1.0, w)
    return exact, partial


# "Which player knows the most about X?", "who worked out the epoch?": what the wiki has on the people behind a topic is in
# its discussion pages (signed posts) and players' own pages, which rank below articles and rarely make the cut.
WHO_KNOWS = re.compile(
    r"\b(?:which|what) (?:players?|person|people|characters?|gms?|gamemasters?|users?|editors?)\b|"
    r"\bwho (?:\w+ )?(?:knows?|knew|figured|worked|wrote|discovered|calculated|found|came up|understands?|maintains?|"
    r"tracks?|researched|tested|documented|mapped|is (?:the |an )?(?:expert|authority))\b|"
    r"\b(?:experts?|authority|authorities) (?:on|in|about)\b",
    re.I,
)
# The words of such a question that are about the people, not the topic
_PEOPLE_WORDS = {"player", "players", "person", "people", "character", "characters", "gm", "gms", "gamemaster",
                 "gamemasters", "user", "users", "editor", "editors", "knows", "know", "knew", "most", "best", "expert",
                 "experts", "authority", "figured", "worked", "wrote", "discovered", "calculated", "found", "understands",
                 "maintains", "researched", "tested", "documented", "game", "games", "out", "up", "came"}
DISCUSSION_SQL = "(p.ns % 2 = 1 OR p.ns = 2)"  # every talk namespace, and User


def discussion_search(conn: sqlite3.Connection, query: str, cfg: RetrievalCfg, base_url: str,
                      qvec: list[float] | None, k: int = 3, avoid_pages: set[int] | None = None) -> list[Hit]:
    """The best passages from discussion and player pages only, one per page, for questions about who knows what:
    keyword search on the topic's words (the question without its words about people) and the nearest vectors among
    those pages, fused like `search`."""
    topic = " ".join(t for t in tokens(query) if t not in STOPWORDS and t not in _PEOPLE_WORDS)
    scores: dict[int, float] = {}
    for mode in ("and", "or"):
        q = _fts_query(topic, mode) if topic else None
        if not q:
            break
        try:
            rows = conn.execute(
                "SELECT f.rowid FROM chunks_fts f JOIN chunks c ON c.chunk_id=f.rowid JOIN pages p ON p.page_id=c.page_id "
                f"WHERE chunks_fts MATCH ? AND {DISCUSSION_SQL} ORDER BY bm25(chunks_fts, 5.0, 2.0, 1.0) LIMIT ?",
                (q, cfg.k_fts)).fetchall()
        except sqlite3.OperationalError:
            rows = []
        for i, r in enumerate(rows):
            scores[r[0]] = scores.get(r[0], 0.0) + 1.0 / (cfg.rrf_k + i + 1)
        if len(rows) >= 5:
            break
    if qvec:  # the vector index cannot filter by namespace: read further down and keep the discussion pages
        near = vec_search_dist(conn, qvec, 50 * cfg.k_vec)
        if near:
            marks = ",".join("?" * len(near))
            keep = {r[0] for r in conn.execute(
                f"SELECT c.chunk_id FROM chunks c JOIN pages p ON p.page_id=c.page_id WHERE c.chunk_id IN ({marks}) "
                f"AND {DISCUSSION_SQL}", [cid for cid, _ in near])}
            for i, cid in enumerate([cid for cid, _ in near if cid in keep][: cfg.k_vec]):
                scores[cid] = scores.get(cid, 0.0) + 1.0 / (cfg.rrf_k + i + 1)
    if not scores:
        return []
    marks = ",".join("?" * len(scores))
    rows = conn.execute(
        "SELECT c.chunk_id, c.page_id, c.kind, c.title, c.section_path, c.anchor, c.text, p.ns, p.page_kind "
        f"FROM chunks c JOIN pages p ON p.page_id=c.page_id WHERE c.chunk_id IN ({marks})", list(scores)).fetchall()
    rows = sorted(rows, key=lambda r: scores[r["chunk_id"]], reverse=True)
    out: list[Hit] = []
    seen: set[int] = set()
    for r in rows:
        if r["page_id"] in seen or (avoid_pages and r["page_id"] in avoid_pages):
            continue
        seen.add(r["page_id"])
        out.append(Hit(chunk_id=r["chunk_id"], page_id=r["page_id"], ns=r["ns"], title=r["title"],
                       section_path=r["section_path"], anchor=r["anchor"], text=r["text"], kind=r["kind"],
                       score=scores[r["chunk_id"]], url=page_url(base_url, r["title"], r["anchor"]),
                       page_kind=r["page_kind"]))
        if len(out) >= k:
            break
    return out


def search(
    conn: sqlite3.Connection,
    query: str,
    cfg: RetrievalCfg,
    base_url: str,
    qvec: list[float] | None,
    *,
    k_final: int | None = None,
    mode: str = "hybrid",
    prior_chunk_ids: list[int] | None = None,
    kind: str | None = None,
    avoid_pages: set[int] | None = None,
    topic_pages: dict[int, float] | None = None,
) -> list[Hit]:
    """kind: what the question is about (crafting, place, ...); favours matching page kinds via cfg.kind_weights.
    prior_chunk_ids: chunks the previous answer cited. They stay candidates and get a modest boost, so a
    follow-up ("what about its weight?") keeps working from the same pages instead of starting from scratch.
    avoid_pages: pages the question did not mean (a same-named page of another kind); never returned.
    topic_pages: page_id -> weight for the family of pages around a topic the question names; they are favoured."""
    k_final = cfg.k_final if k_final is None else k_final  # 0 is a real answer: only code-written passages are wanted
    if k_final <= 0:
        return []
    fts_ids = fts_search(conn, query, cfg.k_fts) if mode in ("hybrid", "fts") else []
    vec_hits = vec_search_dist(conn, qvec, cfg.k_vec) if (mode in ("hybrid", "vec") and qvec) else []
    vec_ids = [cid for cid, _ in vec_hits]
    vec_dist = dict(vec_hits)

    scores: dict[int, float] = {}
    fts_rank = {cid: i + 1 for i, cid in enumerate(fts_ids)}
    vec_rank = {cid: i + 1 for i, cid in enumerate(vec_ids)}
    for ranks in (fts_rank, vec_rank):
        for cid, r in ranks.items():
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (cfg.rrf_k + r)

    exact, partial = title_matches(conn, query)
    # Make sure the title-matched pages' lead chunks are candidates even if search missed them.
    for pid in list(exact) + list(partial):
        for r in conn.execute(
            "SELECT chunk_id FROM chunks WHERE page_id=? ORDER BY CASE kind WHEN 'stats' THEN 0 ELSE 1 END, ord LIMIT 2",
            (pid,),
        ):
            scores.setdefault(r[0], 0.0)

    prior = set(prior_chunk_ids or [])
    for cid in prior:
        scores.setdefault(cid, 0.0)

    if not scores:
        return []
    ids = list(scores)
    qm = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT c.chunk_id, c.page_id, c.kind, c.ord, c.title, c.section_path, c.anchor, c.text, p.ns, "
        f"p.is_disambig, p.page_kind, p.is_log, p.is_obsolete "
        f"FROM chunks c JOIN pages p ON p.page_id=c.page_id "
        f"WHERE c.chunk_id IN ({qm}) AND coalesce(p.page_kind, '') <> 'soft_redirect'",
        ids,
    ).fetchall()

    hits: list[Hit] = []
    for r in rows:
        if avoid_pages and r["page_id"] in avoid_pages:
            continue
        label = "exact" if r["page_id"] in exact else ("partial" if r["page_id"] in partial else "")
        score, weight = score_chunk(
            scores[r["chunk_id"]], cfg, question_kind=kind, ns=r["ns"], page_kind=r["page_kind"],
            is_disambig=bool(r["is_disambig"]), is_log=bool(r["is_log"]), is_obsolete=bool(r["is_obsolete"]),
            title_label=label, partial_weight=partial.get(r["page_id"], 0.0),
            in_prior=r["chunk_id"] in prior,
        )
        if topic_pages and r["page_id"] in topic_pages:
            score *= 1.0 + cfg.topic_boost * topic_pages[r["page_id"]]
        hits.append(
            Hit(
                chunk_id=r["chunk_id"], page_id=r["page_id"], ns=r["ns"], title=r["title"],
                section_path=r["section_path"], anchor=r["anchor"], text=r["text"], kind=r["kind"],
                score=score, fts_rank=fts_rank.get(r["chunk_id"]), vec_rank=vec_rank.get(r["chunk_id"]),
                title_match=label, url=page_url(base_url, r["title"], r["anchor"]),
                page_kind=r["page_kind"], is_log=bool(r["is_log"]), is_obsolete=bool(r["is_obsolete"]),
                weight=weight, vec_dist=vec_dist.get(r["chunk_id"]),
            )
        )
    hits.sort(key=lambda h: h.score, reverse=True)

    out: list[Hit] = []
    per_page: dict[int, int] = {}
    for h in hits:
        if per_page.get(h.page_id, 0) >= cfg.per_page_cap:
            continue
        per_page[h.page_id] = per_page.get(h.page_id, 0) + 1
        out.append(h)
        if len(out) >= k_final:
            break
    return out
