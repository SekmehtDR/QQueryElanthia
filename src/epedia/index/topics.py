"""Topic graph: which pages belong to the family of a hub page ("Crafting" -> Forging, Weaponsmithing, their guides, the
crafted weapons...). Derived from what the wiki already says, with no hand-made links:

- a page in a category named like the hub (`Category:Crafting materials`) is a member (weight 1.0);
- a subpage `Hub/Thing` is a member (1.0);
- a page the hub links to AND that links back is a close member (0.8); one direction only is a looser one (0.4).

Hubs are knowledge pages (crafting, guide, mechanics, skill, guild, race), the base weapons and armor described in the
main namespace ("Broadsword", whose variants are Weapon: pages) and the wiki's own category pages. A hub linked
from a huge part of the wiki (Zoluren, Paladin...) is too general to say anything and is left out; each hub keeps its
strongest members only. The question side (`family`) turns a name in the question into the weights retrieval favours.
"""

from __future__ import annotations

import json
import sqlite3

from ..db import get_state, set_state, transaction
from ..util import utcnow_iso

HUB_KINDS = ("crafting", "guide", "mechanics", "skill", "guild", "race")
MAX_INBOUND = 1500  # more than this and the page is linked from everywhere: not a topic
MAX_MEMBERS = 1500
MIN_MEMBERS = 3
# A link alone does not make these part of a topic: they are people, chatter or stubs that merely mention it.
LINK_SKIP = {"post", "news", "meta", "log", "script", "gm", "pc", "npc", "soft_redirect", "disambig", "category",
             "talk", "user", "file", "site"}


def _hubs(conn: sqlite3.Connection) -> dict[int, str]:
    kinds = ",".join("?" * len(HUB_KINDS))
    rows = conn.execute(
        f"SELECT p.page_id, p.title FROM pages p WHERE p.is_redirect=0 AND (p.page_kind IN ({kinds}) OR "
        "(p.page_kind='category' AND p.title LIKE 'Category:%') OR (p.page_kind='item' AND p.ns=0)) AND "
        "(SELECT count(*) FROM page_links l WHERE l.to_page_id = p.page_id) <= ?", (*HUB_KINDS, MAX_INBOUND)).fetchall()
    return {r[0]: r[1] for r in rows}


def build(conn: sqlite3.Connection, force: bool = False) -> dict[str, int]:
    """Rebuild the graph when pages were classified since the last build (or it has never been built)."""
    since = get_state(conn, "topics.built_at")
    if not force and since and not conn.execute(
            "SELECT 1 FROM pages WHERE classified_revid IS NOT NULL AND updated_at > ? LIMIT 1", (since,)).fetchone():
        return {"hubs": 0, "links": 0}
    hubs = _hubs(conn)
    members: dict[int, dict[int, tuple[float, str]]] = {h: {} for h in hubs}

    def add(hub: int, page: int, w: float, via: str) -> None:
        if page != hub and w > members[hub].get(page, (0, ""))[0]:
            members[hub][page] = (w, via)

    # category membership: the page's categories include the hub's category name
    by_cat = {t[9:].casefold(): h for h, t in hubs.items() if t.startswith("Category:")}
    for page_id, cats in conn.execute("SELECT page_id, categories FROM pages WHERE is_redirect=0 AND categories IS NOT NULL"):
        try:
            names = json.loads(cats)
        except ValueError:
            continue
        for name in names:
            hub = by_cat.get(str(name).replace("_", " ").replace("Category:", "").casefold())
            if hub is not None:
                add(hub, page_id, 1.0, "category")
    # subpages: one pass over the titles that have a slash, matched to the hub named before it
    by_title = {t.casefold(): h for h, t in hubs.items()}
    for pid, title in conn.execute("SELECT page_id, title FROM pages WHERE is_redirect=0 AND title LIKE '%/%'"):
        parent = title.rsplit("/", 1)[0]
        while parent:
            hub = by_title.get(parent.casefold())
            if hub is not None:
                add(hub, pid, 1.0, "subpage")
            parent = parent.rsplit("/", 1)[0] if "/" in parent else ""
    # links in either direction
    ids = ",".join("?" * len(hubs)) or "NULL"
    out_links = {(a, b) for a, b in conn.execute(
        f"SELECT from_page_id, to_page_id FROM page_links WHERE from_page_id IN ({ids})", list(hubs))}
    in_links = {(a, b) for a, b in conn.execute(
        f"SELECT to_page_id, from_page_id FROM page_links WHERE to_page_id IN ({ids})", list(hubs))}
    kinds = {r[0]: r[1] for r in conn.execute("SELECT page_id, page_kind FROM pages")}
    for hub, page in out_links | in_links:
        if kinds.get(page) in LINK_SKIP:
            continue
        both = (hub, page) in out_links and (hub, page) in in_links
        add(hub, page, 0.8 if both else 0.4, "link")

    rows = []
    kept = []
    for hub, found in members.items():
        if len(found) < MIN_MEMBERS:
            continue
        best = sorted(found.items(), key=lambda kv: -kv[1][0])[:MAX_MEMBERS]
        kept.append((hub, hubs[hub], len(best)))
        rows += [(hub, page, w, via) for page, (w, via) in best]
    with transaction(conn):
        conn.execute("DELETE FROM page_topics")
        conn.execute("DELETE FROM topic_hubs")
        conn.executemany("INSERT INTO topic_hubs(hub_page_id, title, members) VALUES (?,?,?)", kept)
        conn.executemany("INSERT OR REPLACE INTO page_topics(hub_page_id, page_id, weight, via) VALUES (?,?,?,?)", rows)
    set_state(conn, "topics.built_at", utcnow_iso())
    return {"hubs": len(kept), "links": len(rows)}


def family(conn: sqlite3.Connection, question: str, res=None) -> dict[int, float]:
    """page_id -> weight for the family around the hub the question names, or {} when it names none. `res` is the
    question's already-resolved name, so the caller and this agree on which entity it is."""
    from ..retrieval.resolve import resolve

    res = res or resolve(conn, question)
    if res is None:
        return {}
    hubs = {c.page_id for c in (res.chosen or res.candidates)}
    if not hubs:
        return {}
    marks = ",".join("?" * len(hubs))
    try:
        rows = conn.execute(
            f"SELECT page_id, max(weight) FROM page_topics WHERE hub_page_id IN ({marks}) GROUP BY page_id",
            list(hubs)).fetchall()
    except sqlite3.OperationalError:
        return {}
    out = {r[0]: r[1] for r in rows}
    for h in hubs & {r[0] for r in conn.execute(f"SELECT hub_page_id FROM topic_hubs WHERE hub_page_id IN ({marks})",
                                                list(hubs))}:
        out[h] = 1.0
    return out
