"""Page edit history: who changed a page, when, and how steadily, so answers can say how far to trust it.

Wiki pages are edited by anyone. A fact set by a long run of small additions is a different thing from one that was
rewritten last week or changed back and forth, so each answer about a page can carry its recent edits and a plain
reading of them. The reading is computed here, in code; the model only words it, and never decides which version of
a disputed fact is true.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import quote

from ..db import get_state, set_state, transaction
from . import timeline

DEFAULT_EDITS = 10
MAX_EDITS = 100
SETTING_KEY = "setting.history_edits"
CURSOR_KEY = "history.cursor"
COMPLETE_KEY = "history.complete"  # set when a bulk read reached the end; updates only top up a complete history

_IP = re.compile(r"^(?:\d{1,3}\.){3}\d{1,3}$|^[0-9a-f:]+:[0-9a-f:]+$", re.I)
_REVERT_COMMENT = re.compile(r"^(?:undo|undid|revert|reverted|rv|rollback|rolled back)\b", re.I)
BIG_SHARE = 0.5  # an edit that adds or removes at least half the page is a rewrite, not a tweak
BIG_MIN_BYTES = 400  # ...unless the page was tiny to begin with
BUILT_UP = 1000  # a page this size is established; growing a stub into a page is not a rewrite
_UNDONE_USER = re.compile(r"\bby \[\[(?:Special:Contributions/|User:)?([^|\]]+)", re.I)


def edits_setting(conn: sqlite3.Connection, default: int = DEFAULT_EDITS) -> int:
    """How many recent edits each answer may see (0 switches the history off). Set from the page, kept in the db."""
    raw = get_state(conn, SETTING_KEY)
    try:
        return max(0, min(MAX_EDITS, int(raw))) if raw is not None else default
    except ValueError:
        return default


def set_edits_setting(conn: sqlite3.Connection, n: int) -> int:
    n = max(0, min(MAX_EDITS, int(n)))
    set_state(conn, SETTING_KEY, str(n))
    return n


# ------------------------------------------------------------------------------------------------ storing

def rows_from(page: dict[str, Any]) -> list[tuple]:
    """(revid, title, ts, user, comment, size, parentid, tags) for each revision under one allrevisions page."""
    out = []
    for r in page.get("revisions", []):
        out.append((r["revid"], page["title"], r["timestamp"], r.get("user") or "", r.get("comment") or "",
                    r.get("size"), r.get("parentid"), " ".join(r.get("tags") or [])))
    return out


def store(conn: sqlite3.Connection, rows: list[tuple]) -> int:
    if not rows:
        return 0
    with transaction(conn):
        conn.executemany(
            "INSERT OR IGNORE INTO revisions(revid, title, ts, user, comment, size, parentid, tags) "
            "VALUES (?,?,?,?,?,?,?,?)", rows)
    return len(rows)


ARV_PROPS = "ids|timestamp|user|comment|size|tags"


async def backfill(client: Any, conn: sqlite3.Connection, namespaces: list[int], progress: Any = None) -> int:
    """Read every revision's metadata, oldest first, from where the last run stopped. Metadata only: about 500
    revisions per request. Safe to stop and resume (the cursor is the newest timestamp stored)."""
    since = get_state(conn, CURSOR_KEY)
    params: dict[str, Any] = {
        "action": "query", "list": "allrevisions", "arvdir": "newer", "arvlimit": 500, "arvprop": ARV_PROPS,
        "arvnamespace": "|".join(str(n) for n in sorted(namespaces)),
    }
    if since:
        params["arvstart"] = since
    total = 0
    async for data in client.paginate(params):
        batch: list[tuple] = []
        for page in data.get("query", {}).get("allrevisions", []):
            batch.extend(rows_from(page))
        if batch:
            total += store(conn, batch)
            set_state(conn, CURSOR_KEY, max(r[2] for r in batch))
            if progress:
                progress(total, max(r[2] for r in batch))
    set_state(conn, COMPLETE_KEY, "1")
    return total


# ------------------------------------------------------------------------------------------------ reading

@dataclass
class Edit:
    ts: str
    user: str
    comment: str
    size: int | None
    delta: int | None  # bytes added (+) or removed (-) relative to the edit before it
    tags: str

    @property
    def is_revert(self) -> bool:
        tags = self.tags.replace("mw-reverted", "")  # that tag marks the edit that was undone, not the one that undid it
        return "revert" in tags or "undo" in tags or "rollback" in tags or bool(
            _REVERT_COMMENT.match(self.comment.strip()))

    @property
    def is_anonymous(self) -> bool:
        return bool(_IP.match(self.user))

    @property
    def is_big(self) -> bool:
        if self.delta is None or self.size is None:
            return False
        before = self.size - self.delta
        if self.delta > 0 and before < BUILT_UP:
            return False
        return abs(self.delta) >= BIG_MIN_BYTES and abs(self.delta) >= BIG_SHARE * max(before, 1)

    @property
    def is_self_revert(self) -> bool:
        """Someone undoing their own edit ("Undo revision 5 by BINU" made by BINU) is tidying, not a dispute."""
        m = _UNDONE_USER.search(self.comment)
        return bool(m) and m.group(1).strip().casefold() == self.user.casefold()


def recent_edits(conn: sqlite3.Connection, title: str, n: int) -> list[Edit]:
    """The last n edits, newest first, with each one's size change (it needs one older edit to measure against)."""
    try:
        rows = conn.execute(
            "SELECT ts, user, comment, size, tags FROM revisions WHERE title=? ORDER BY ts DESC, revid DESC LIMIT ?",
            (title, n + 1)).fetchall()
    except sqlite3.OperationalError:
        return []
    edits = []
    for i, r in enumerate(rows[:n]):
        older = rows[i + 1]["size"] if i + 1 < len(rows) else None
        delta = r["size"] - older if r["size"] is not None and older is not None else None
        edits.append(Edit(r["ts"], r["user"] or "", r["comment"] or "", r["size"], delta, r["tags"] or ""))
    return edits


def _hours(a: str, b: str) -> float:
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    return abs((datetime.strptime(a, fmt) - datetime.strptime(b, fmt)).total_seconds()) / 3600


def assess(edits: list[Edit]) -> tuple[str, list[str]]:
    """('steady' | 'mixed' | 'contested', the reasons), from sizes, reverts, editors and bursts of edits."""
    if not edits:
        return "steady", []
    reasons: list[str] = []
    reverts = [e for e in edits if e.is_revert and not e.is_self_revert]
    big = [e for e in edits if e.is_big]
    anon = [e for e in edits if e.is_anonymous]
    editors = {e.user for e in edits if e.user}
    flurry = any(
        len(near := [o for o in edits if _hours(e.ts, o.ts) <= 24]) >= 4 and len({o.user for o in near}) >= 2
        for e in edits)
    if reverts:
        reasons.append(f"{len(reverts)} edit{'s' if len(reverts) != 1 else ''} undid or reverted earlier changes")
    for e in big[:2]:
        sign = "added" if (e.delta or 0) > 0 else "removed"
        reasons.append(f"on {e.ts[:10]} {e.user or 'someone'} {sign} about {abs(e.delta or 0):,} bytes in one edit "
                       "(a large rewrite, not a tweak)")
    if anon:
        reasons.append(f"{len(anon)} edit{'s' if len(anon) != 1 else ''} came from anonymous (IP) editors")
    if flurry:
        reasons.append("several edits by different people landed within a day of each other")
    if len(reverts) >= 2 or (reverts and big) or (flurry and (reverts or big)):
        return "contested", reasons
    if reverts or big or anon or flurry:
        return "mixed", reasons
    small = sum(1 for e in edits if e.delta is not None and e.delta >= 0)
    if len(edits) >= 3 and small >= 0.7 * len(edits):
        reasons.append("mostly small additions, which fits a page being kept up to date")
    elif len(editors) == 1 and editors:
        reasons.append(f"all of these edits are by {next(iter(editors))}")
    return "steady", reasons


GUIDANCE = {
    "steady": "Nothing here suggests the page was disputed; treat it as an ordinary, maintained page.",
    "mixed": ("A few changes here were undone, which on its own is ordinary upkeep. Do not remark on the page's edits in "
              "general; add a caution only for a detail the key facts below do not call credible."),
    "contested": ("This page has been changed back and forth or heavily rewritten. Be sceptical and say so plainly; "
                  "give the details the passages support but do not present disputed ones as settled fact, and do not "
                  "decide which version is true. Where the key facts below call a detail credible, you may state it."),
}


def history_url(base_url: str, title: str) -> str:
    return f"{base_url.rstrip('/')}/index.php?title={quote(title.replace(' ', '_'), safe=':/(),!*')}&action=history"


def passage(conn: sqlite3.Connection, title: str, n: int, base_url: str) -> dict | None:
    """A passage listing a page's last n edits and a plain reading of them, or None if none are stored."""
    if n <= 0:
        return None
    edits = recent_edits(conn, title, n)
    if not edits:
        return None
    verdict, reasons = assess(edits)
    if verdict != "contested" and all(r.verdict == "credible" for r in timeline.readings(conn, title)):
        return None  # nothing to hedge: shown the margin anyway, a small model remarks on it ("not contested in recent edits")
    lines = [f"Reading of the history: {verdict}.", *[f"- {r}" for r in reasons], GUIDANCE[verdict], ""]
    facts = timeline.lines(conn, title)
    if facts:
        lines += ["Key facts that changed (who set each value, and whether to trust it):", *facts, ""]
    lines.append("Newest first (date, editor, size change, summary):")
    for e in edits:
        delta = "" if e.delta is None else f"{e.delta:+,} bytes"
        flags = " [revert]" if e.is_revert and not e.is_self_revert else ""
        lines.append(f"- {e.ts[:10]} · {e.user or '?'} · {delta} · {e.comment[:90] or '(no summary)'}{flags}")
    return {
        "title": f"{title}: recent edits",
        "section": f"last {len(edits)} edits",
        "url": history_url(base_url, title),
        "text": "\n".join(lines),
        "chunk_id": None,
        "history": True,
    }


REVISED_DAYS = 90  # an edit younger than this is worth mentioning when an answer rests on the page


def revised_note(conn: sqlite3.Connection, title: str, now: datetime | None = None) -> str:
    """For any page: one in-voice sentence when a real person changed it lately, with their edit summary if they left one,
    else ''. It needs no old page text, only the stored edit metadata, so it covers every page, not just characters."""
    edits = recent_edits(conn, title, 6)
    if len(edits) < 2:
        return ""  # a page with one revision was only created
    now = now or datetime.utcnow()
    for e in edits:
        if timeline._SYSTEM_USER.search(e.user) or e.is_revert:
            continue
        if not e.comment.strip() and (e.delta is None or abs(e.delta) < 30):
            continue  # a typo fix with nothing to say
        when = datetime.strptime(e.ts, "%Y-%m-%dT%H:%M:%SZ")
        if (now - when).days > REVISED_DAYS:
            return ""  # the newest real change is old: the page has stood a while
        sec = re.match(r"\s*/\*\s*(.*?)\s*\*/\s*(.*)", e.comment)  # "/* Section */ what they did" as the wiki writes it
        said, section = (sec.group(2), sec.group(1)) if sec else (e.comment, "")
        said = re.sub(r"\[\[(?:[^\]|]*\|)?([^\]]*)\]\]", r"\1", said).strip(" -:")[:80]
        if said.lower().startswith(("created page", "redirected", "moved page")):
            said = ""
        said = ", ".join(x for x in (said, f"in the {section[:50]} section" if section else "") if x)
        label = f"{when.day} {when:%b %Y}"
        shown = f" ({said})" if said else ""
        return f"That said, my notes on {title} were last revised on {label}{shown}, so it may be newer than other write-ups."
    return ""


def caution(conn: sqlite3.Connection, titles: list[str], default_n: int = DEFAULT_EDITS) -> str:
    """One in-voice sentence for a code-written answer when a page it rests on has been heavily rewritten or reverted
    lately, else ''. The tables are the whole evidence of such an answer, so a shaky page must not pass unremarked."""
    n = edits_setting(conn, default_n)
    if n <= 0:
        return ""
    shaky = []
    for title in dict.fromkeys(titles):
        edits = recent_edits(conn, title, n)
        if edits and assess(edits)[0] != "steady":
            shaky.append(title)
    if not shaky:
        return ""
    names = " and ".join(f"the {t} page" if not t.startswith("Category:") else f"the {t[9:]} page" for t in shaky)
    return f"Mind you, my notes on {names} were scribbled over a fair bit lately, so check the numbers before you commit."
