"""Which page does a name in the question mean? Names resolve through the title index (pages and their redirects),
and when one name reaches pages of different kinds ("Sidasas Sedra" the spell, a creature of the same name) the
question's own words pick the kind (cast, wield, hunt...) or, when it names none, the caller asks which was meant.

Short all-capital names ("SIS") also name an exact page, so pages that only contain the letters ("Svidaw sis", a
different weapon) are set aside instead of competing with it.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field

from ..kinds import KIND_GROUP, KIND_LABEL, NAMES_A_THING_SQL, intent_groups
from .titles import STOP_NAMES, normalize

_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9'’\-]*")
_ACRONYM = re.compile(r"^[A-Z0-9]{2,5}$")
MAX_SPAN = 6
LOOSE_KINDS = {"general", "command"}  # pages that share a name with a word in everyday use
SKIP_KINDS = {"disambig", "soft_redirect", "category", "meta", "log", "script", "post", "talk", "file", "site"}


@dataclass
class Entity:
    page_id: int
    title: str
    kind: str

    @property
    def group(self) -> str:
        return KIND_GROUP.get(self.kind, "thing")

    @property
    def label(self) -> str:
        return KIND_LABEL.get(self.kind, "page")


@dataclass
class Resolution:
    span: str  # the words in the question that named it
    candidates: list[Entity]  # every page that name reaches, one per page
    chosen: list[Entity] = field(default_factory=list)  # narrowed by the question's own kind words
    ambiguous: bool = False  # still several kinds and nothing to choose between them
    acronym: bool = False  # a short all-capital name: pages that merely contain these letters are set aside


def _lookup(conn: sqlite3.Connection, norm: str) -> list[Entity]:
    phrase = '"' + norm.replace('"', '""') + '"'
    try:
        rows = conn.execute(
            "SELECT DISTINCT t.page_id, p.title, p.page_kind FROM titles_fts t JOIN pages p ON p.page_id = t.page_id "
            "WHERE titles_fts MATCH ? AND t.title_norm = ? AND coalesce(p.page_kind, '') NOT IN "
            "('disambig','soft_redirect','category','meta','log','script','post','talk','file','site') "
            "AND p.ns NOT IN (116, 120) AND " + NAMES_A_THING_SQL.format(col="p.ns") + " LIMIT 30",
            (phrase, norm)).fetchall()
    except sqlite3.OperationalError:
        return []
    return [Entity(r[0], r[1], r[2] or "general") for r in rows]


def _weight(cands: list[Entity]) -> int:
    """How much a name says: a spell, creature, place or character beats a generic page or a command, which beat items."""
    if any(c.group != "thing" and c.kind not in LOOSE_KINDS for c in cands):
        return 2
    return 1 if any(c.group != "thing" for c in cands) else 0


def _shouting(question: str) -> bool:
    letters = [ch for ch in question if ch.isalpha()]
    return len(letters) > 6 and sum(ch.isupper() for ch in letters) > len(letters) / 2


def resolve(conn: sqlite3.Connection, question: str) -> Resolution | None:
    """The longest name in the question that reaches a page, or None. Single common words are not names. Among names of
    the same length the most specific wins ("hunt rats": the creature, not the command), then the rarer, then the first."""
    words = _WORD.findall(question)
    lows = [w.lower() for w in words]
    shouting = _shouting(question)
    for size in range(min(MAX_SPAN, len(words)), 0, -1):
        found: list[tuple[tuple[int, int], int, list[Entity]]] = []
        for start in range(0, len(words) - size + 1):
            span = lows[start:start + size]
            if all(w in STOP_NAMES for w in span):
                continue
            if size == 1 and (span[0] in STOP_NAMES or (len(span[0]) < 3 and not words[start][0].isupper())):
                continue  # a short word counts as a name only when it is written like one
            norm = normalize(" ".join(span))
            cands = _lookup(conn, norm)
            if not cands and len(norm) > 3 and norm.endswith("s") and not norm.endswith("ss"):
                cands = _lookup(conn, norm[:-1])  # a plural: "rats" names the Rat
            if cands:
                found.append(((-_weight(cands), len(cands)), start, cands))
        if not found:
            continue
        _rank, start, cands = min(found, key=lambda f: (f[0], f[1]))
        original = " ".join(words[start:start + size])
        res = Resolution(original, cands, acronym=size == 1 and not shouting and bool(_ACRONYM.match(words[start])))
        wanted = intent_groups(question)
        groups = {c.group for c in cands}
        if len(groups) > 1:
            picked = [c for c in cands if c.group in wanted]
            # Items share names with almost everything (a rat, a title, a guild), so with no word to go on the one
            # other kind wins quietly; only two specific kinds (a spell and a character) are worth asking about.
            specific = [c for c in cands if c.group != "thing"]
            if picked:
                res.chosen = picked
            elif specific and len({c.group for c in specific}) == 1:
                res.chosen = specific
            else:
                res.ambiguous = True
        else:
            res.chosen = cands
        return res
    return None


def clarify(res: Resolution) -> str:
    """The question to ask when a name reaches different kinds of page and the question does not say which."""
    seen: dict[str, Entity] = {}
    for c in res.candidates:
        seen.setdefault(c.group, c)
    picks = list(seen.values())[:3]
    named = [f"the {c.label} {c.title.split(':', 1)[-1]}" for c in picks]
    return f"{res.span} could mean " + ", or ".join(named) + ". Which did you mean?"


def set_aside(conn: sqlite3.Connection, res: Resolution) -> set[int]:
    """Pages to leave out of a search because the question named something else: for an acronym, the pages whose
    names merely contain it as a word."""
    if not res.acronym or not res.chosen:
        return set()
    keep = {c.page_id for c in res.candidates}
    token = res.span.lower()
    out: set[int] = set()
    for pid, norm in conn.execute(
            "SELECT page_id, title_norm FROM titles_fts WHERE titles_fts MATCH ? LIMIT 200", (f'"{token}"',)):
        if pid not in keep and token in re.split(r"[^a-z0-9]+", norm):
            out.add(pid)
    return out
