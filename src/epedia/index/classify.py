"""Page classification (what kind of page is this?) and the internal link graph.

Kinds come from namespace, category and title patterns: Semantic MediaWiki's Page_type only covers items.
Everything here works from data already in the database (stored HTML, categories), with no network or model.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import zlib
from dataclasses import dataclass
from urllib.parse import parse_qs, unquote

from rich.progress import BarColumn, MofNCompleteColumn, Progress, TextColumn, TimeRemainingColumn

from ..db import transaction
from ..kinds import SITE_NAMESPACES, is_talk_ns

log = logging.getLogger("epedia.classify")

ITEM_NS = {110, 112, 114, 116, 118}
SOFT_REDIRECT_MARKER = 'class="redirectMsg"'

_LOG_TITLE = re.compile(r"^[^/]+/Logs?(/|$)")
_OBSOLETE_TITLE = re.compile(r"\((?:obsolete|Obsolete)\)$|\s\(2\.0\)$")
_PLAYER_FACET = re.compile(r"^Player (Prime|Platinum|Fallen|Test)$")
_GUIDE_TITLE = re.compile(r"( guide| hunting ladder|walkthroughs?)$")
_CRAFTING_TITLE = re.compile(r" (discipline|craft|techniques|products)$")
_NEWS_TITLE = re.compile(r"^(News \d|DragonRealms Updates|The First Land Herald/)")
_RANIK = re.compile(r"^RanikMap\d")
_ARMS_OVERVIEW = re.compile(r"\b(?:weapons|armou?rs|shields)(?: \(crafted\))?$", re.I)

NPC_CATS = {"NPCs", "Shopowner", "Shopowners", "Alterers", "Guild Leaders", "Emperor", "Crafting Society Masters"}
PLACE_CATS = {"Cities", "Provinces", "Locations", "Landmarks", "Islands", "Docks", "Waterways", "Guildhalls",
              "Crafting halls", "Housing", "Ships", "Ranik Maps"}
SPELL_CATS = {"Spells", "Signature spells", "Obsolete spells", "Khri", "Barbarian abilities"}
GUIDE_CATS = {"Player guides", "Guild player guides", "New player guides", "Crafting player guides",
              "Hunting ladders"}
CRAFT_CATS = {"Creation systems", "Techniques", "Crafting scripts"}
MATERIAL_CATS = {"Materials", "Crafting materials", "Metals", "Woods", "Leather", "Fabric", "Gems", "Stones",
                 "Bones"}
LORE_CATS = {"Immortals", "History", "Lore", "Player-created lore", "Book", "Books", "Organizations",
             "Heavenly bodies", "Holidays", "Languages", "Artifacts", "Libraries"}
EVENT_CATS = {"Events", "Festivals", "Quests", "Event Logs", "Current Prime Events", "Auctions", "Raffles"}
NEWS_CATS = {"News", "DragonRealms Updates", "First Land Herald articles", "General announcements"}
ARMS_CATS = {"Weapons", "Armor", "Shields"}


@dataclass(frozen=True)
class Classification:
    kind: str
    is_obsolete: bool = False
    is_log: bool = False


def classify_page(
    ns: int,
    title: str,
    categories: list[str],
    *,
    soft_redirect: bool = False,
    is_disambig: bool = False,
    parent_kind: str | None = None,
) -> Classification:
    """First matching rule wins; see the table in the project plan. `title` includes any namespace prefix."""
    cats = set(categories)
    obsolete = bool(
        {"Obsolete articles", "Obsolete spells"} & cats or _OBSOLETE_TITLE.search(title)
    )

    def done(kind: str) -> Classification:
        return Classification(kind, obsolete, kind == "log")

    if soft_redirect:
        return done("soft_redirect")
    if is_talk_ns(ns):  # the discussion behind a page (Talk, User talk, Item talk...): players working things out
        return done("talk")
    if ns == 2:  # a player's own page and subpages (analyses, formulas, drafts), whatever categories it borrows
        return done("user")
    if ns in SITE_NAMESPACES:  # file descriptions and the wiki's machinery (templates, forms, properties...)
        return done(SITE_NAMESPACES[ns][0])
    if ns in (4, 12):
        return done("meta")
    if ns == 14:
        return done("category")
    if ns == 120:
        return done("post")
    if ns in ITEM_NS:
        return done("recipe" if title.endswith(" (crafted)") else "item")
    if "Disambiguation" in cats or is_disambig:
        return done("disambig")
    # Event logs are a legitimate event kind; character role-play logs are not.
    if _LOG_TITLE.match(title) or ("Logs" in cats and "Event Logs" not in cats):
        return done("log")
    if ns == 208 and "Races" in cats:
        return done("race")
    if "Player Characters" in cats or any(_PLAYER_FACET.match(c) for c in cats):
        return done("pc")
    if {"GameMasters", "Past GameMasters"} & cats:
        return done("gm")
    if NPC_CATS & cats:
        return done("npc")
    if {"Shops", "Merchants"} & cats or any(c.endswith(" shops") for c in cats):
        return done("shop")
    if PLACE_CATS & cats or any(c.endswith(" cities") for c in cats) or _RANIK.match(title):
        return done("place")
    if "Bestiary" in cats or any(c.startswith(("Creatures with ", "Creatures without ")) or c.endswith("creatures")
                                 for c in cats):
        return done("creature")
    if SPELL_CATS & cats or any(c.endswith((" spells", " abilities")) for c in cats):
        return done("spell")
    if "Guilds" in cats:
        return done("guild")
    if "Skills" in cats or title.endswith(" skill"):
        return done("skill")
    if "Commands" in cats or any(c.endswith(" commands") for c in cats) or title.endswith(" command"):
        return done("command")
    if GUIDE_CATS & cats or _GUIDE_TITLE.search(title):
        return done("guide")
    if CRAFT_CATS & cats or _CRAFTING_TITLE.search(title):
        return done("crafting")
    if MATERIAL_CATS & cats:
        return done("material")
    if "Scripts" in cats or title.endswith("(script)"):
        return done("script")
    if LORE_CATS & cats or title.endswith("(book)"):
        return done("lore")
    if EVENT_CATS & cats:
        return done("event")
    if NEWS_CATS & cats or _NEWS_TITLE.match(title):
        return done("news")
    # A weapon, armor or shield described in the main namespace ("Svidaw sis", "Broadsword", "Ribbed bone armor") is
    # the thing itself, though the wiki files the base nouns under Definitions; overviews have plural titles.
    if ARMS_CATS & cats and not _ARMS_OVERVIEW.search(title):
        return done("recipe" if title.endswith(" (crafted)") else "item")
    if "Definitions" in cats:
        return done("mechanics")
    if "/" in title and parent_kind not in (None, "general", "category"):
        return done(parent_kind)
    return done("general")


# ------------------------------------------------------------------ HTML helpers (regex: ~7x faster than bs4)

_REDIRECT_TARGET = re.compile(r'class="redirectText".*?<a [^>]*title="([^"]+)"', re.S)
_LINK = re.compile(r'<a href="/([^"]+)"([^>]*)>')
_RED_LINK = re.compile(r'class="[^"]*\bnew\b')
_SKIP_PREFIXES = ("File:", "Category:", "Special:", "Property:", "Property_talk:", "Template:", "Help:",
                  "User:", "Talk:", "Form:", "Module:", "images/", "MediaWiki:")
# index.php?title=X links that are about a page rather than to it (editing, history, a diff, a missing page)
_NOT_A_VISIT = frozenset({"action", "redlink", "oldid", "diff", "curid", "printable"})
MAX_LINKS_PER_PAGE = 300


def soft_redirect_target(html: str) -> str | None:
    """Target title if this page is only a 'Redirect to: X' stub (MediaWiki marks it with div.redirectMsg)."""
    if SOFT_REDIRECT_MARKER not in html:
        return None
    m = _REDIRECT_TARGET.search(html)
    if not m:
        return ""
    return re.sub(r" \(page does not exist\)$", "", m.group(1).replace("&#39;", "'").replace("&amp;", "&"))


def _link_title(href: str) -> str | None:
    """The page a wiki href points at, or None. The wiki writes the same link three ways: `/Title`, `/index.php/Title`
    (item pages, through their templates) and `/index.php?title=Title`; `#section` only says where on the page."""
    if href.startswith("index.php?"):
        query = parse_qs(href[len("index.php?"):].replace("&amp;", "&").split("#", 1)[0])
        if _NOT_A_VISIT & query.keys() or "title" not in query:
            return None
        return query["title"][0].replace("_", " ")
    if href.startswith("index.php/"):
        href = href[len("index.php/"):]
    path = href.split("#", 1)[0]
    if not path or "?" in path or path.startswith("index.php"):  # "/Page?action=edit" and the like
        return None
    return unquote(path).replace("_", " ")


def extract_links(html: str) -> list[str]:
    """Distinct article-like link targets (as titles with spaces), in page order, red links skipped."""
    seen: dict[str, None] = {}
    for m in _LINK.finditer(html):
        href, attrs = m.group(1), m.group(2)
        if _RED_LINK.search(attrs):
            continue
        title = _link_title(href)
        if not title or title.startswith(_SKIP_PREFIXES):
            continue
        seen.setdefault(title, None)
        if len(seen) >= MAX_LINKS_PER_PAGE:
            break
    return list(seen)


def resolve_titles(conn: sqlite3.Connection) -> dict[str, int]:
    """Exact title -> page_id, including redirect names pointing at their target's page."""
    out: dict[str, int] = {}
    # Join against pages: a redirect's stored target id can outlive the page it pointed at.
    for r in conn.execute(
        "SELECT r.from_title, r.to_page_id FROM redirects r JOIN pages p ON p.page_id = r.to_page_id"
    ):
        out[r[0]] = r[1]
    for r in conn.execute("SELECT title, page_id FROM pages WHERE is_redirect=0"):
        out[r[0]] = r[1]  # a real page beats a redirect of the same name
    return out


# ------------------------------------------------------------------ driver


def classify_all(
    conn: sqlite3.Connection,
    *,
    all_pages: bool = False,
    links: bool = True,
    show_progress: bool = True,
    batch: int = 1000,
) -> dict[str, int]:
    """Fill page_kind / flags (and page_links) for pages whose classification lags their fetched revision."""
    from ..wiki.discover import upsert_redirect

    where = "is_redirect=0 AND fetched_revid IS NOT NULL"
    if not all_pages:
        where += " AND classified_revid IS NOT fetched_revid"
    rows = conn.execute(
        f"SELECT page_id, ns, title, categories, is_disambig, fetched_revid FROM pages WHERE {where} ORDER BY title"
    ).fetchall()
    stats = {"pages": len(rows), "soft_redirects": 0, "links": 0}
    if not rows:
        return stats

    kinds_by_title: dict[str, str] = {
        r[0]: r[1] for r in conn.execute("SELECT title, page_kind FROM pages WHERE page_kind IS NOT NULL")
    }
    titles = resolve_titles(conn) if links else {}
    progress = Progress(TextColumn("classify"), BarColumn(), MofNCompleteColumn(), TimeRemainingColumn(),
                        disable=not show_progress)
    task = progress.add_task("classify", total=len(rows))
    with progress:
        for start in range(0, len(rows), batch):
            chunk = rows[start:start + batch]
            with transaction(conn):
                for r in chunk:
                    page_id, ns, title = r["page_id"], r["ns"], r["title"]
                    cats = json.loads(r["categories"] or "[]")
                    html = _load_html(conn, page_id)
                    target = soft_redirect_target(html) if html else None
                    parent = kinds_by_title.get(title.rsplit("/", 1)[0]) if "/" in title else None
                    c = classify_page(ns, title, cats, soft_redirect=target is not None,
                                      is_disambig=bool(r["is_disambig"]), parent_kind=parent)
                    kinds_by_title[title] = c.kind
                    conn.execute(
                        "UPDATE pages SET page_kind=?, is_obsolete=?, is_log=?, classified_revid=? WHERE page_id=?",
                        (c.kind, int(c.is_obsolete), int(c.is_log), r["fetched_revid"], page_id),
                    )
                    if links:
                        conn.execute("DELETE FROM page_links WHERE from_page_id=?", (page_id,))
                        if target is None and html and c.kind not in ("log",):
                            ids = {titles[t] for t in extract_links(html) if t in titles} - {page_id}
                            conn.executemany("INSERT OR IGNORE INTO page_links VALUES (?, ?)",
                                             [(page_id, i) for i in ids])
                            stats["links"] += len(ids)
                    if target is not None:
                        stats["soft_redirects"] += 1
                        if target:  # marker present but target unparseable -> still classified, no redirect row
                            upsert_redirect(conn, title, target, None)
            progress.update(task, advance=len(chunk))
    return stats


def _load_html(conn: sqlite3.Connection, page_id: int) -> str:
    row = conn.execute("SELECT html FROM page_html WHERE page_id=?", (page_id,)).fetchone()
    return zlib.decompress(row[0]).decode("utf-8") if row else ""


def kind_counts(conn: sqlite3.Connection) -> list[tuple[str, int]]:
    return [(r[0] or "(unclassified)", r[1]) for r in conn.execute(
        "SELECT page_kind, count(*) FROM pages WHERE is_redirect=0 GROUP BY page_kind ORDER BY 2 DESC")]
