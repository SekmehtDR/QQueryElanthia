"""'What alteration sessions are on at Hollow Eve?' answered in code from the festival's own schedule page.

A session page lists each instance (Prime, Platinum, The Fallen) in its own table. A real thumbs-down showed the model
given the Platinum table and five copies of the same "Where" text from other years' pages, but never Prime, so it
answered half the schedule as if it were all of it. Here every instance is listed under its own heading, the sessions
still to come first, and an instance with nothing announced says so.

The wiki's layout changed over the years (no Day column before 436, "Plat" for Platinum, long tables split over several
sections without their header), so rows are read by the header's column names and a split table keeps its header.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime

from ..retrieval.resolve import explicit_pages, named_pages
from ..util import page_url
from .event_prizes import _event

SUFFIX = "/Alteration sessions"
INSTANCES = (("Prime", r"prime"), ("Platinum", r"plat(?:inum)?"), ("The Fallen", r"(?:the )?fallen|tf"))
SHOWN_COMING = 30
SHOWN_HELD = 5  # with sessions still to come, the last few held are enough; the page has every one
SHOWN_HELD_OVER = 15  # a festival that is over: its last sessions

_ALTER = re.compile(r"\balter(?:ation|ations|er|ers|ing)?\b", re.I)
_SCHEDULE = re.compile(r"\b(sessions?|schedul\w*|when|times?|dates?|coming up|upcoming|calendar|merchants?|available|"
                       r"on (?:now|today|tonight)|happening|next)\b", re.I)
_DATE = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$|^(\d{1,2})/(\d{1,2})/(\d{4})\b")
_YEAR = re.compile(r"(?<![\w#/])(\d{3})(?![\w/])")


@dataclass
class Session:
    when: date
    day: str
    time: str
    merchant: str
    done: str
    items: str


def wants(question: str) -> bool:
    """A question about when alteration merchants are on, not about how alterations work."""
    return bool(_ALTER.search(question) and _SCHEDULE.search(question))


def _pages(conn: sqlite3.Connection) -> dict[str, tuple[str, int]]:
    """Every schedule page: title -> (festival, year)."""
    out = {}
    for (title,) in conn.execute("SELECT title FROM pages WHERE ns=0 AND is_redirect=0 AND substr(title, -?)=?",
                                 (len(SUFFIX), SUFFIX)):
        m = re.match(r"^(.*?)\s+(\d{3})" + re.escape(SUFFIX) + "$", title)
        if m:
            out[title] = (m.group(1), int(m.group(2)))
    return out


def which_page(conn: sqlite3.Connection, question: str) -> str | None:
    """The schedule asked about: one named outright, the named festival's, the newest of a festival named without its year
    ("Hollow Eve" is this year's), or the newest of all when none is named."""
    pages = _pages(conn)
    if not pages:
        return None
    for title in explicit_pages(conn, question):
        if title in pages:
            return title
    for title in named_pages(conn, question):
        if title + SUFFIX in pages:
            return title + SUFFIX
    events = {e for e, _y in pages.values()}
    event = _event(question, events) or ""
    years = {int(y) for y in _YEAR.findall(question)}
    candidates = [(y, t) for t, (e, y) in pages.items() if (not event or e == event) and (not years or y in years)]
    if not candidates and years:  # a year that has no schedule page: the festival's newest is still the best guess
        candidates = [(y, t) for t, (e, y) in pages.items() if not event or e == event]
    return max(candidates)[1] if candidates else None


def _instance(section: str) -> str:
    """'Session List > Prime (part 2)' -> 'Prime'; '' when the section names none (the oldest pages have one table)."""
    for part in reversed([re.sub(r"\s*\(part \d+\)$", "", p).strip() for p in section.split(">")]):
        for name, rx in INSTANCES:
            if re.fullmatch(rx, part, re.I):
                return name
    return ""


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _column(header: list[str], *names: str) -> int | None:
    for i, h in enumerate(header):
        if any(h.lower().startswith(n) for n in names):
            return i
    return None


def _when(cell: str) -> date | None:
    m = _DATE.match(cell.strip())
    if not m:
        return None
    try:
        if m.group(1):
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        return date(int(m.group(6)), int(m.group(4)), int(m.group(5)))
    except ValueError:
        return None


def read(conn: sqlite3.Connection, title: str) -> tuple[dict[str, list[Session]], dict[str, dict]]:
    """({instance: sessions in date order}, {instance: the chunk its table starts in}). An instance whose table holds only
    the page's blank template row is there with no sessions: "none announced yet" is something the page says."""
    found: dict[str, list[Session]] = {}
    chunk_of: dict[str, dict] = {}
    header: list[str] = []
    current = None
    for r in conn.execute("SELECT c.chunk_id, c.section_path, c.anchor, c.text FROM chunks c JOIN pages p ON p.page_id=c.page_id "
                          "WHERE p.title=? ORDER BY c.ord", (title,)):
        named = _instance(r["section_path"] or "")
        if named or current is None:
            current = named if named else current or ""
        for line in (r["text"] or "").splitlines():
            if not line.startswith("|"):
                if line.strip() and (folded := _instance(line.strip())):
                    current = folded  # a small section the chunker folded into this one leaves only its heading line
                continue
            if re.fullmatch(r"\|(?:\s*:?-+:?\s*\|)+", line.strip()):
                continue
            cells = _cells(line)
            low = [c.lower() for c in cells]
            if low and low[0] == "date" and any(c.startswith("merchant") for c in low):
                header = cells  # a header row, or the blank template row ("Date | DayOfWeek | Time | MerchantName")
                if not any(c.startswith("merchantname") for c in low):
                    found.setdefault(current, [])
                    chunk_of.setdefault(current, r)
                continue
            when = _when(cells[0]) if cells else None
            if when is None or not header:
                continue
            col = {k: _column(header, *names) for k, names in (
                ("day", ("day",)), ("time", ("time",)), ("merchant", ("merchant",)), ("done", ("alterations",)),
                ("items", ("items", "details", "description", "notes")))}

            def cell(key: str) -> str:
                i = col[key]
                return cells[i].strip("[] ") if i is not None and i < len(cells) else ""
            time = cell("time") or (cells[0][len(cells[0].split()[0]):].strip() if " " in cells[0] else "")
            found.setdefault(current, []).append(Session(when, cell("day"), time, cell("merchant"), cell("done"), cell("items")))
            chunk_of.setdefault(current, r)
    for sessions in found.values():
        sessions.sort(key=lambda s: s.when)
    if "" in found and "Prime" not in found and len(found) > 1:  # older pages: the untitled table, beside "Platinum", is Prime's
        found["Prime"] = found.pop("")
        if "" in chunk_of:
            chunk_of["Prime"] = chunk_of.pop("")
    return found, chunk_of


def _line(s: Session) -> str:
    day = s.day[:3] if s.day else f"{s.when:%a}"
    when = f"{day} {s.when:%b} {s.when.day}" + (f", {s.time}" if s.time else "")
    done = f" ({s.done} alterations)" if s.done and s.done != "?" else ""
    items = f": {s.items}" if s.items else ""
    return f"- {when}: **{s.merchant or 'unnamed'}**{items}{done}"


def _instances_asked(question: str) -> list[str]:
    return [name for name, rx in INSTANCES if re.search(rf"\b(?:{rx})\b", question, re.I)]


def passage(conn: sqlite3.Connection, question: str, base_url: str, today: date | None = None) -> list[dict] | None:
    """[the schedule as a code-written `answer` with the page's sections it cites], or None when this is not a schedule
    question or no schedule page is indexed."""
    if not wants(question):
        return None
    title = which_page(conn, question)
    if title is None:
        return None
    found, chunk_of = read(conn, title)
    if not found:
        return None
    today = today or datetime.now().astimezone().date()
    festival = title[: -len(SUFFIX)]
    order = [n for n, _ in INSTANCES if n in found] + [n for n in found if n not in dict(INSTANCES)]
    asked = _instances_asked(question)
    if asked:
        order = [n for n in order if n in asked] or order
    out: list[dict] = []

    def cite(instance: str) -> int:
        r = chunk_of.get(instance)
        if r is None:
            return 0
        out.append({"title": title, "section": r["section_path"], "url": page_url(base_url, title, r["anchor"]),
                    "text": r["text"], "chunk_id": r["chunk_id"]})
        return len(out)

    every = [s for n in order for s in found[n]]
    over = bool(every) and max(s.when for s in every) < today
    body = [f"**{festival} alteration sessions**, from the festival's own schedule page (times Eastern, as the page gives them):"]
    for name in order:
        sessions = found[name]
        n = cite(name)
        tag = f" [{n}]" if n else ""
        heading = f"**{name or 'Sessions'}**{tag}"
        if not sessions:
            body += ["", f"{heading}: none listed yet."]
            continue
        coming = [s for s in sessions if s.when >= today]
        held = [s for s in sessions if s.when < today]
        body += ["", heading]
        if coming:
            body += ["Coming up:", *[_line(s) for s in coming[:SHOWN_COMING]]]
            if len(coming) > SHOWN_COMING:
                body.append(f"- ...and {len(coming) - SHOWN_COMING} more on the page.")
        if held:
            keep = SHOWN_HELD if coming else SHOWN_HELD_OVER
            body.append("Already held:" if coming else ("Held:" if over else "Held so far:"))
            if len(held) > keep:
                body.append(f"- ...{len(held) - keep} earlier on the page, then:")
            body += [_line(s) for s in held[-keep:]]
    if not over:
        body += ["", "(OOC) These are the sessions announced so far: more may be added (FESTIVAL ALTERATIONS in game lists the "
                 "schedule), and merchants can turn up unannounced."]
    out_answer = "\n".join(body)
    if not out:  # nothing to stand on but the page itself
        out.append({"title": title, "section": "", "url": page_url(base_url, title), "text": out_answer, "chunk_id": None})
    out[0]["answer"] = out_answer
    return out


def readable(conn: sqlite3.Connection) -> tuple[bool, str]:
    """For `epedia doctor`: the newest schedule page still reads as sessions."""
    pages = _pages(conn)
    if not pages:
        return True, "no schedule pages indexed"
    title = max(pages, key=lambda t: pages[t][1])
    found, _ = read(conn, title)
    n = sum(len(v) for v in found.values())
    return n > 0, f"{title}: {n} sessions in {len(found)} instances ({', '.join(k or 'unnamed' for k in found)})"
