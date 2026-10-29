"""How a character's key facts (guild, race, gender, status) changed over time, and whether to trust the current value.

Edit metadata says a page changed; this says *which fact* changed, from the infobox of each old version. The trust
reading is plain rules, written down here so it can be checked and argued with:

- the page's owner (its creator, or an editor named like the page) setting a fact is the strongest evidence;
- a value an editor other than the owner set, and that has stood unchallenged for months, is accepted;
- a recent change by someone other than the owner is unconfirmed, and a fact changed back and forth is disputed;
- the owner restoring a value after someone else changed it is credible (the owner knows the character).

Fennick is told the reading and the values; he is never told which version is true when it is disputed.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from ..db import transaction
from ..util import utcnow_iso

FIELDS = ("guild", "race", "gender", "status")
_FIELD = {f: re.compile(r"\|\s*" + f + r"\s*=\s*((?:\[\[[^\]]*\]\]|[^\n|}])*)", re.I) for f in FIELDS}
_LINK = re.compile(r"\[\[(?:[^\]|]*\|)?([^\]]*)\]\]")
RECENT_DAYS = 30  # a change younger than this has not had time to be checked
SETTLED_DAYS = 180  # a change older than this and never challenged is accepted
FETCH_REVISIONS = 50  # the wiki's limit when revision text is requested


def parse_fields(wikitext: str) -> dict[str, str]:
    out = {}
    for f, pat in _FIELD.items():
        m = pat.search(wikitext)
        v = _LINK.sub(r"\1", m.group(1)) if m else ""
        out[f] = re.sub(r"\s+", " ", v).strip()
    return out


# Other pages (spells, creatures, places...) have infoboxes with their own parameters. Rather than list them by hand, every
# short parameter of the page's first template is read, and only its changes are kept.
INFOBOX_KINDS = ("spell", "creature", "material", "place", "shop", "guild", "race", "skill", "command")
_NOISE = re.compile(r"^(image\w*|img\w*|pic\w*|photo\w*|map\w*|icon\w*|caption|alt|desc\w*|note\w*|text|quote|links?|url|"
                    r"colou?r|bg\w*|style|width|height|size|sort\w*|name|title|displaytitle|new|updated?\w*|date|dr3|"
                    r"instance|footer|header|template|id|number)$", re.I)
MAX_VALUE = 60


def _first_template(text: str) -> str:
    """The body of the page's first {{template}}, with nested braces balanced, or ''."""
    start = text.find("{{")
    if start < 0:
        return ""
    depth, i = 0, start
    while i < len(text) - 1:
        pair = text[i:i + 2]
        if pair == "{{":
            depth, i = depth + 1, i + 2
        elif pair == "}}":
            depth, i = depth - 1, i + 2
            if depth == 0:
                return text[start + 2:i - 2]
        else:
            i += 1
    return ""


def _top_level_split(body: str) -> list[str]:
    parts, depth, cur, i = [], 0, [], 0
    while i < len(body):
        two = body[i:i + 2]
        if two in ("[[", "{{"):
            depth, i = depth + 1, i + 2
            cur.append(two)
        elif two in ("]]", "}}"):
            depth, i = depth - 1, i + 2
            cur.append(two)
        elif body[i] == "|" and depth == 0:
            parts.append("".join(cur))
            cur, i = [], i + 1
        else:
            cur.append(body[i])
            i += 1
    parts.append("".join(cur))
    return parts


def parse_infobox(wikitext: str) -> dict[str, str]:
    """{parameter: short plain value} of the first template. Long, markup-heavy and decorative parameters are left out."""
    out: dict[str, str] = {}
    for part in _top_level_split(_first_template(wikitext))[1:]:
        name, eq, value = part.partition("=")
        name = re.sub(r"[^a-z0-9]+", "_", name.strip().casefold()).strip("_")
        if not eq or not name or _NOISE.match(name):
            continue
        value = re.sub(r"\s+", " ", _LINK.sub(r"\1", re.sub(r"<!--.*?-->", "", value, flags=re.S))).strip()
        if len(value) > MAX_VALUE or "{{" in value or "<" in value or "[[" in value:
            continue
        out[name] = value
    return out


# ------------------------------------------------------------------------------------------------ fetching

def store_fields(conn: sqlite3.Connection, title: str, revs: list[dict[str, Any]], upto_revid: int,
                 generic: bool = False) -> int:
    """Keep each version's fields. `generic` (a page that is not a character) reads whatever the infobox holds and stores
    a value only when it differs from the version before it, so a page of many versions stays small."""
    rows = []
    seen: dict[str, str] = {}
    for r in sorted(revs, key=lambda r: (r["timestamp"], r["revid"])) if generic else revs:
        text = ((r.get("slots") or {}).get("main") or {}).get("content") or r.get("content") or ""
        for f, v in (parse_infobox(text) if generic else parse_fields(text)).items():
            if generic:
                if seen.get(f) == v:
                    continue
                seen[f] = v
            rows.append((title, r["revid"], r["timestamp"], r.get("user") or "", f, v))
    with transaction(conn):
        conn.executemany(
            "INSERT OR REPLACE INTO field_values(title, revid, ts, user, field, value) VALUES (?,?,?,?,?,?)", rows)
        conn.execute("INSERT OR REPLACE INTO fields_done(title, upto_revid, fetched_at) VALUES (?,?,?)",
                     (title, upto_revid, utcnow_iso()))
    return len(rows)


async def fetch_fields(client: Any, conn: sqlite3.Connection, title: str, upto_revid: int, generic: bool = False) -> int:
    """One request: the last 50 versions of a page, reduced to their guild/race/gender/status (a character) or to
    whatever its infobox holds (`generic`)."""
    data = await client.get({
        "action": "query", "prop": "revisions", "titles": title, "rvprop": "ids|timestamp|user|content",
        "rvslots": "main", "rvlimit": FETCH_REVISIONS, "format": "json", "formatversion": "2",
    })
    pages = data.get("query", {}).get("pages", [])
    revs = pages[0].get("revisions", []) if pages else []
    return store_fields(conn, title, revs, upto_revid, generic)


def pending_titles(conn: sqlite3.Connection, only: set[str] | None = None, others: bool = True) -> list[tuple[str, int]]:
    """Pages whose field history is missing or older than the page we hold (optionally just `only`): characters, and
    unless `others` is off the kinds with an infobox, when they have been edited more than once."""
    kinds = ("pc", "npc", *(INFOBOX_KINDS if others else ()))
    marks = ",".join("?" * len(kinds))
    return [(r[0], r[1]) for r in conn.execute(
        "SELECT p.title, p.latest_revid FROM pages p LEFT JOIN fields_done d ON d.title = p.title "
        f"WHERE p.is_redirect = 0 AND (p.ns = 0 OR p.page_kind IN ('pc','npc')) AND p.page_kind IN ({marks}) AND p.latest_revid IS NOT NULL "
        "AND (d.upto_revid IS NULL OR d.upto_revid != p.latest_revid) "
        "AND (p.page_kind IN ('pc','npc') OR (SELECT count(*) FROM revisions r WHERE r.title = p.title) >= 2) "
        "ORDER BY p.title", kinds)
        if only is None or r[0] in only]


async def fetch_all(client: Any, conn: sqlite3.Connection, progress: Any = None, only: set[str] | None = None,
                    limit: int | None = None) -> int:
    todo = pending_titles(conn, only)[:limit]
    done = 0
    for title, revid in todo:
        try:
            kind = conn.execute("SELECT page_kind FROM pages WHERE title=?", (title,)).fetchone()
            await fetch_fields(client, conn, title, revid, generic=bool(kind) and kind[0] not in ("pc", "npc"))
        except Exception as exc:  # one odd page must not stop a few thousand
            if progress:
                progress(done, len(todo), f"skipped {title}: {exc}")
            continue
        done += 1
        if progress and (done % 50 == 0 or done == len(todo)):
            progress(done, len(todo), title)
    return done


# ------------------------------------------------------------------------------------------------ reading

@dataclass
class Segment:
    value: str
    since: str  # when this value was first seen
    user: str  # who set it
    by_owner: bool


@dataclass
class Reading:
    field: str
    current: str
    verdict: str  # unchanged | credible | accepted | unverified | disputed
    why: str
    history: list[Segment]  # newest first


_SYSTEM_USER = re.compile(r"script|\bbot\b|maintenance|mediawiki|import|migrat", re.I)  # a page made by a tool has no owner
_PLACEHOLDER = {"", "-", "n", "a", "none", "unknown", "?", "n/a"}  # not a real value to compare against


def _norm(v: str) -> str:
    return re.sub(r"\s+", " ", v).strip().casefold()


def owners(conn: sqlite3.Connection, title: str) -> set[str]:
    """Who the page belongs to: the editor who created it, and any editor named like the page."""
    out = {title.casefold()}
    for sql in ("SELECT DISTINCT user FROM revisions WHERE title=? ORDER BY ts, revid LIMIT 10",
                "SELECT DISTINCT user FROM field_values WHERE title=? ORDER BY ts, revid LIMIT 10"):
        try:
            rows = conn.execute(sql, (title,)).fetchall()
        except sqlite3.OperationalError:
            rows = []
        creator = next((r[0] for r in rows if r[0] and not _SYSTEM_USER.search(r[0])), None)
        if creator:
            out.add(creator.casefold())
            break
    return out


def segments(conn: sqlite3.Connection, title: str, field: str) -> list[Segment]:
    """The values a field has held, oldest first, each with who set it."""
    own = owners(conn, title)
    segs: list[Segment] = []
    for ts, user, value in conn.execute(
            "SELECT ts, user, value FROM field_values WHERE title=? AND field=? ORDER BY ts, revid", (title, field)):
        if user and _SYSTEM_USER.search(user):
            continue  # a bulk tool reformatting infoboxes says nothing about who is right
        if segs and _norm(segs[-1].value) == _norm(value):
            continue
        segs.append(Segment(value, ts, user or "", (user or "").casefold() in own))
    return segs


def _days(since: str, now: datetime) -> float:
    return (now - datetime.strptime(since, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)).total_seconds() / 86400


def judge(field: str, segs: list[Segment], now: datetime | None = None) -> Reading | None:
    """The trust reading for one field, or None if no value was ever recorded."""
    now = now or datetime.now(timezone.utc)
    if not segs or all(not s.value for s in segs):
        return None
    last = segs[-1]
    newest_first = list(reversed(segs))
    if len(segs) == 1:
        return Reading(field, last.value, "unchanged", f"has been {last.value or 'blank'} in every stored version",
                       newest_first)
    values = [_norm(s.value) for s in segs]
    flipped = len(set(values)) < len(values)  # a value came back after something else
    others_before = any(not s.by_owner for s in segs[:-1])
    age = _days(last.since, now)
    who = f"{last.user} ({'the page owner' if last.by_owner else 'not the page owner'})"
    if last.by_owner:
        if others_before:
            why = f"set by {who} on {last.since[:10]}, after another editor had changed it"
        else:
            why = f"changed by {who} on {last.since[:10]}; a reroll or a correction"
        if flipped:
            why += "; it changed several times before that"
        return Reading(field, last.value, "credible", why, newest_first)
    owner_said = next((s for s in reversed(segs[:-1]) if s.by_owner and _norm(s.value) not in _PLACEHOLDER), None)
    if owner_said and _norm(owner_said.value) != _norm(last.value):
        return Reading(field, last.value, "suspect",
                       f"the page's owner ({owner_said.user}) last had it as {owner_said.value or 'blank'}; "
                       f"{who} changed it on {last.since[:10]}", newest_first)
    if flipped and len(segs) >= 3:
        return Reading(field, last.value, "disputed",
                       f"changed back and forth; latest set by {who} on {last.since[:10]}", newest_first)
    if age < RECENT_DAYS:
        return Reading(field, last.value, "unverified",
                       f"changed {int(age)} days ago by {who}; too recent to have been checked", newest_first)
    if age >= SETTLED_DAYS:
        return Reading(field, last.value, "accepted",
                       f"changed by {who} on {last.since[:10]} and left alone since", newest_first)
    return Reading(field, last.value, "unverified", f"changed by {who} on {last.since[:10]}", newest_first)


GUIDANCE = {
    "credible": "say it plainly; whoever started that entry set it, so it stands. Say nothing about the margin",
    "suspect": "say it, but add that the entry once said something else before someone else changed it, so you cannot vouch for it; name both values",
    "accepted": "say it; the entry was changed some time ago and has stood since",
    "unverified": "say it, but add that it was changed recently and nobody has confirmed it",
    "disputed": "say the entry has been swapped back and forth, give the values, and do not choose between them",
}


def readings(conn: sqlite3.Connection, title: str, now: datetime | None = None) -> list[Reading]:
    """Readings for the facts that changed (an unchanged fact needs no comment)."""
    out = []
    try:
        stored = [r[0] for r in conn.execute(
            "SELECT DISTINCT field FROM field_values WHERE title=? AND field NOT IN (?,?,?,?)", (title, *FIELDS))]
        for f in (*FIELDS, *sorted(stored)):
            r = judge(f, segments(conn, title, f), now)
            if r and r.verdict != "unchanged":
                out.append(r)
    except sqlite3.OperationalError:
        return []
    return out


def lines(conn: sqlite3.Connection, title: str, now: datetime | None = None) -> list[str]:
    out = []
    for r in readings(conn, title, now):
        if r.verdict == "credible" and r.field not in FIELDS:
            continue  # an infobox detail nobody doubts needs no line
        if r.verdict == "credible":
            # Nothing to hedge, and the older values only tempt the model to remark on the margin.
            out.append(f"- {r.field.capitalize()}: {r.current or 'blank'}: state it plainly; {GUIDANCE['credible']}.")
            continue
        trail = " <- ".join(f"{s.value or 'blank'} ({s.user or '?'}, {s.since[:10]})" for s in r.history[:5])
        out.append(f"- {r.field.capitalize()}: {r.current or 'blank'}: {r.verdict}. {r.why[0].upper() + r.why[1:]}. "
                   f"How to say it: {GUIDANCE[r.verdict]}. Values, newest first: {trail}")
    return out


def caveats(conn: sqlite3.Connection, title: str, answer: str, now: datetime | None = None,
            earlier_too: bool = True) -> list[str]:
    """Sentences in Fennick's voice, written by code, for changed facts the answer states: the model is told how to
    hedge but an 8B model forgets. The value whoever started the entry set is authoritative and is stated plainly, with
    only a "that said" note of what it used to be (`earlier_too`); a value someone else changed away from it, or one
    that has flipped between editors, is never given without a caution."""
    out = []
    generic = 0
    for r in readings(conn, title, now):
        if not r.current or (r.verdict == "credible" and not earlier_too):
            continue
        if not re.search(r"(?<![\w-])" + re.escape(r.current.casefold()) + r"(?![\w-])", answer.casefold()):
            continue
        label = r.field.replace("_", " ")
        if r.field not in FIELDS:
            # A page's own infobox detail ("level", "slot"): the answer must say both what it is and its value, and a page
            # gets at most two such notes, or a coincidence of numbers would litter the answer.
            if label not in answer.casefold() or generic >= 2:
                continue
            generic += 1
        earlier = []
        for s in reversed(r.history[1:]):  # oldest first, each with the year it first appeared
            if s.value and _norm(s.value) != _norm(r.current) and s.value not in [e for e, _ in earlier]:
                earlier.append((s.value, s.since[:4]))
        names = " and ".join(f"{v} ({y})" for v, y in earlier[:3])
        if r.verdict == "credible":
            if earlier:
                out.append(f"That said, my notes first had {title}'s {label} as {names}, before the entry settled on "
                           f"{r.current}.")
        elif r.verdict == "suspect":
            owner = next((s for s in r.history[1:] if s.by_owner), None)
            said = owner.value if owner else names
            out.append(f"Mind you, my notes first had {title}'s {label} as {said}, and someone else has since changed "
                       f"it to {r.current}. I can't vouch for that.")
        elif r.verdict == "disputed":
            out.append(f"Mind you, my notes on {title} have swapped the {label} back and forth (earlier: {names}), "
                       f"so I can't vouch for {r.current}.")
        elif r.verdict == "unverified" and earlier:
            out.append(f"Mind you, the {label} in my notes used to say {names}; it changed recently and nobody has "
                       f"confirmed it yet.")
    return out
