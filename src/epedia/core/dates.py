"""Real-world dates in wiki text, compared with today, so the model never has to do the arithmetic.

Festival, event and announcement pages write dates like "Friday, October 26, 2018". A small model asked "what festival is
next?" cannot reliably tell that those are years ago; this works it out in code and says so next to the passage.
In-game years ("Hollow Eve Festival 428", "457 AV") are not real dates and are ignored.
"""

from __future__ import annotations

import re
from datetime import date

_MONTHS = ("January February March April May June July August September October November December").split()
_ABBR = {m[:3].lower(): i + 1 for i, m in enumerate(_MONTHS)}
_MONTH = r"(January|February|March|April|May|June|July|August|September|October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sept|Sep|Oct|Nov|Dec)\.?"
_MDY = re.compile(r"\b" + _MONTH + r"\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b")  # "October 26, 2018", "Aug 29, 2024", "March 17th, 2018"
_DMY = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+" + _MONTH + r",?\s+(\d{4})\b")  # "17 March 2018"
_ISO = re.compile(r"\b((?:19|20)\d\d)-(\d\d)-(\d\d)\b")  # "2026-11-02"; in-game dates like "450-03-04" do not match
_COMPACT = re.compile(r"\b((?:19|20)\d\d)(\d\d)(\d\d)\b")  # "20270102"


def _make(y: int, m: int, d: int) -> date | None:
    try:
        return date(y, m, d)
    except ValueError:
        return None  # "February 30": a typo on the wiki, not a date


def real_dates(text: str) -> list[date]:
    found: list[date] = []
    for m in _MDY.finditer(text):
        found.append(_make(int(m.group(3)), _ABBR[m.group(1)[:3].lower()], int(m.group(2))))
    for m in _DMY.finditer(text):
        found.append(_make(int(m.group(3)), _ABBR[m.group(2)[:3].lower()], int(m.group(1))))
    for rx in (_ISO, _COMPACT):
        for m in rx.finditer(text):
            found.append(_make(int(m.group(1)), int(m.group(2)), int(m.group(3))))
    return [d for d in found if d is not None]


def keep_ahead_lines(text: str, today: date) -> str:
    """Drop the lines (table rows, list items) whose dates are all over, so an event list shows only what is still ahead."""
    kept = []
    for line in text.split("\n"):
        dates = real_dates(line)
        if dates and max(dates) < today:
            continue
        kept.append(line)
    return "\n".join(kept)


def _fmt(d: date) -> str:
    return f"{d:%B} {d.day}, {d.year}"


def date_note(text: str, today: date) -> str:
    """A one-line note on whether the dates in `text` are past or upcoming; '' when there are none."""
    dates = real_dates(text)
    if not dates:
        return ""
    lo, hi = min(dates), max(dates)
    span = _fmt(lo) if lo == hi else f"{_fmt(lo)} to {_fmt(hi)}"
    if hi < today:
        return f"[Date check: the dates in this passage ({span}) are all before today ({_fmt(today)}); it is over.]"
    if lo >= today:
        return f"[Date check: the dates in this passage ({span}) are all today or later ({_fmt(today)}); it is upcoming.]"
    return f"[Date check: this passage has dates before and after today ({_fmt(today)}): {span}. Check each one.]"


# Questions that only an event still ahead (or under way) can answer.
UPCOMING = re.compile(
    r"\b(upcoming|next|coming|soon|scheduled|future|ongoing|currently|current|happening|right now|"
    r"this (?:week|weekend|month|year))\b",
    re.I,
)


def split_by_time(passages: list[dict], today: date) -> tuple[list[dict], list[dict], list[dict]]:
    """(still ahead or under way, already over, no real-world dates) for event-style questions."""
    ahead: list[dict] = []
    over: list[dict] = []
    undated: list[dict] = []
    for p in passages:
        dates = real_dates(p.get("text", ""))
        if not dates:
            undated.append(p)
        elif max(dates) >= today:
            ahead.append(p)
        else:
            over.append(p)
    return ahead, over, undated


def over_answer(over: list[dict], today: date, persona: bool = True) -> tuple[str, list[dict]]:
    """The reply when every dated event found is over: say so, name the latest, and cite it. Returns (text, cited passages)."""
    ranked = sorted(over, key=lambda p: max(real_dates(p["text"])), reverse=True)
    latest = ranked[0]
    dates = real_dates(latest["text"])
    lo, hi = min(dates), max(dates)
    span = _fmt(hi) if lo == hi else f"{_fmt(lo)} to {_fmt(hi)}"
    cited = [latest]
    title = latest["title"]
    if persona:
        text = (f"I can't find anything on the wiki scheduled after today ({_fmt(today)}). The most recent I can see is "
                f"{title}, {span} [1], and that one is already over. The wiki may not list the next one yet.")
    else:
        text = (f"Nothing on the wiki is scheduled after {_fmt(today)}. The most recent is {title}, {span} [1], "
                "which is over; the next one may not be listed yet.")
    return text, cited
