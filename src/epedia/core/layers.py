"""Layered answers: "tell me about svidaw sis" reads the weapon, where it comes from, what its name means and who its
people are, and where it turns up in the story, instead of eight passages of one stat sheet.

Code decides what each part reads (`kinds.LAYERS`), supplies the headings and drops a part nothing supports; the model only
writes under them. No model call and no wiki request: a few queries, and a vector distance per candidate, ranked by how
close a page is to what the subject is about rather than to the question, so pages that echo its words are not favoured.
"""

from __future__ import annotations

import difflib
import re
import sqlite3
from dataclasses import dataclass

from ..index.extract.names import LANGUAGES, bare_name
from ..kinds import LAYER_OFFER, LAYER_TABLE, LAYERS, OFFER_LISTING_TITLE, Layer
from ..retrieval import resolve
from ..retrieval.resolve import Entity, Resolution
from ..retrieval.titles import normalize
from ..util import page_url
from . import names
from .attribution import Evidence, unit_words
from .verify import SUPPORT_COVERAGE

LAYER_MAX_DISTANCE = 0.40  # cosine distance to the subject's opening; further than this a page is about something else
MENTION_MAX_DISTANCE = 0.34  # a page that only names it must be closer: past this it is in passing (Estate Rights, a musk hog)
CANDIDATES = 200  # pages looked at per layer: a hub's links and mentions run to thousands, and the closest are wanted
FOCUSED_OWN_SLOTS = 2  # "the story behind X" still says what X is, but only that much
LOG_LABEL = "(A player's role-play log, not official lore.)"

_POLITE = r"(?:please |can you |could you |would you )?"
_OPEN = re.compile(
    rf"^{_POLITE}(?:tell me (?:all |more |everything )?about|describe|explain|what do you know about|what can you tell me about|"
    r"info(?:rmation)? (?:on|about)|who(?:'s| is| was| are| were)|what(?:'s| is| was| are| were)|whats)\s+(?P<x>.+?)[\s?!.]*$", re.I)
_WHY_IT_MATTERS = re.compile(r"^why (?:is|are|was|were|does|do) (?P<x>.+?) (?:so )?(?:important|matters?)[\s?!.]*$", re.I)
_FOCUS = re.compile(
    rf"^{_POLITE}(?:(?:tell me|give me|what(?:'s| is| was| are)|whats)\s+)?(?:about\s+)?(?:the\s+)?"
    r"(?:story|stories|lore|history|background|legend|legends|reasoning)\s+(?:behind|of|about|for)\s+(?P<x>.+?)[\s?!.]*$", re.I)
_DETERMINER = re.compile(r"^(?:a|an|the)\s+", re.I)
_CITE = re.compile(r"\[\d+[*?]?\]")


@dataclass
class Subject:
    """What an open question is about."""

    main: Entity
    pages: list[Entity]
    focused: bool  # asks for the story only: the practical parts are left out

    @property
    def name(self) -> str:
        return bare_name(self.main.title)


def _subject(conn: sqlite3.Connection, res: Resolution | None, focused: bool) -> Subject | None:
    if res is None or res.ambiguous or not res.chosen:
        return None
    # the article, not an item page of the same name, says what the thing is
    in_main = {r[0] for r in conn.execute(
        f"SELECT page_id FROM pages WHERE ns=0 AND page_id IN ({','.join('?' * len(res.chosen))})",
        [c.page_id for c in res.chosen])}
    main = next((c for c in res.chosen if c.page_id in in_main), res.chosen[0])
    table = LAYER_TABLE.get(main.kind)
    if table is None:
        return None
    # its article first, then same-named pages of the same sort (Weapon:Svidaw sis; not the player's own User: page)
    pages = [main, *[c for c in res.chosen if c is not main and LAYER_TABLE.get(c.kind) == table]]
    return Subject(main, pages, focused)


def open_question(conn: sqlite3.Connection, message: str) -> Subject | None:
    """The subject of a question that asks about a whole thing, or None. The words after "what is" must be exactly a
    page's name, so "what is the weight of a svidaw sis?" stays narrow without a list of attribute words."""
    text = message.strip().replace("’", "'")
    m = _FOCUS.match(text)
    focused = bool(m)
    m = m or _WHY_IT_MATTERS.match(text) or _OPEN.match(text)
    if not m:
        return None
    x = _DETERMINER.sub("", m["x"].strip(" \"'")).strip(" \"'")
    res = resolve.resolve(conn, x)
    if res is None or normalize(res.span) != normalize(x):
        return None
    return _subject(conn, res, focused)


def about(conn: sqlite3.Connection, question: str) -> Subject | None:
    """The one thing a narrow question names, for the offer of the rest."""
    return _subject(conn, resolve.resolve(conn, question), False)


def table_for(subject: Subject) -> tuple[Layer, ...]:
    return LAYERS[LAYER_TABLE[subject.main.kind]]


def _lead_vector(conn: sqlite3.Connection, page_id: int) -> bytes | None:
    row = conn.execute("SELECT v.embedding FROM chunks c JOIN chunk_vec v ON v.chunk_id=c.chunk_id WHERE c.page_id=? "
                       "ORDER BY c.ord LIMIT 1", (page_id,)).fetchone()
    return row[0] if row else None


def _linked(conn: sqlite3.Connection, page_ids: list[int], kinds: frozenset[str]) -> set[int]:
    if not kinds:
        return set()
    ids, want = ",".join("?" * len(page_ids)), ",".join("?" * len(kinds))
    return {r[0] for r in conn.execute(
        f"SELECT p.page_id FROM pages p WHERE p.is_redirect=0 AND p.is_log=0 AND p.is_obsolete=0 AND p.page_kind IN ({want}) AND "
        f"p.page_id IN (SELECT to_page_id FROM page_links WHERE from_page_id IN ({ids}) UNION "
        f"SELECT from_page_id FROM page_links WHERE to_page_id IN ({ids})) LIMIT {CANDIDATES}",
        [*kinds, *page_ids, *page_ids])}


def _mentioning(conn: sqlite3.Connection, name: str, kinds: frozenset[str], logs: bool) -> dict[int, int]:
    """page_id -> the chunk that names the subject, for pages of these kinds (or logs)."""
    who = "p.is_log=1" if logs else f"p.is_log=0 AND p.page_kind IN ({','.join('?' * len(kinds))})"
    args = [] if logs else list(kinds)
    out: dict[int, int] = {}
    phrase = '"' + re.sub(r"[^\w]+", " ", name) + '"'
    rx = re.compile(r"(?<!\w)" + re.escape(name) + r"(?!\w)", re.I)
    for pid, cid, text in conn.execute(
            f"SELECT p.page_id, c.chunk_id, c.text FROM chunks_fts f JOIN chunks c ON c.chunk_id=f.rowid "
            f"JOIN pages p ON p.page_id=c.page_id WHERE chunks_fts MATCH ? AND p.is_redirect=0 AND p.is_obsolete=0 AND {who} "
            f"ORDER BY f.rank LIMIT {CANDIDATES}", [f"text:{phrase}", *args]):
        if pid not in out and rx.search(text):  # the index stems words; the name itself must really be there
            out[pid] = cid
    return out


def _chunks_for(conn: sqlite3.Connection, page_ids: set[int], name: str) -> dict[int, int]:
    """page_id -> its first chunk naming the subject, else its opening."""
    if not page_ids:
        return {}
    marks = ",".join("?" * len(page_ids))
    out: dict[int, tuple[int, int]] = {}
    for pid, cid, order, named in conn.execute(
            f"SELECT page_id, chunk_id, ord, instr(lower(text), ?) > 0 FROM chunks WHERE page_id IN ({marks}) ORDER BY ord",
            [name.casefold(), *page_ids]):
        rank = 0 if named else 1
        if pid not in out or rank < out[pid][0]:
            out[pid] = (rank, cid)
    return {pid: cid for pid, (_, cid) in out.items()}


def _closest(conn: sqlite3.Connection, chunks: dict[int, int], lead: bytes, slots: int, only_named: set[int]) -> list[int]:
    """Up to `slots` chunk ids, one per page, the closest to the subject's opening and near enough to it; pages in
    `only_named` have no link to the subject, just a mention, and must be nearer."""
    if not chunks:
        return []
    marks = ",".join("?" * len(chunks))
    rows = conn.execute(f"SELECT chunk_id, vec_distance_cosine(embedding, ?) AS d FROM chunk_vec WHERE chunk_id IN ({marks})",
                        [lead, *chunks.values()]).fetchall()
    named_chunks = {cid for pid, cid in chunks.items() if pid in only_named}
    near = sorted((r[1], r[0]) for r in rows if r[1] is not None
                  and r[1] <= (MENTION_MAX_DISTANCE if r[0] in named_chunks else LAYER_MAX_DISTANCE))
    return [cid for _, cid in near[:slots]]


def _passage(conn: sqlite3.Connection, chunk_id: int, layer: str, base_url: str, label: str = "") -> dict:
    r = conn.execute("SELECT c.chunk_id, c.title, c.section_path, c.anchor, c.text, p.page_kind FROM chunks c "
                     "JOIN pages p ON p.page_id=c.page_id WHERE c.chunk_id=?", (chunk_id,)).fetchone()
    out = {"title": r[1], "section": r[2], "url": page_url(base_url, r[1], r[3]), "chunk_id": r[0], "page_kind": r[5],
           "text": f"{label}\n{r[4]}" if label else r[4], "layer": layer}
    if label:
        out["log"] = True
    return out


def _language_pages(conn: sqlite3.Connection, meanings: list[names.Meaning]) -> set[int]:
    titles = {LANGUAGES[m.language] for m in meanings if m.language in LANGUAGES}
    if not titles:
        return set()
    return {r[0] for r in conn.execute(f"SELECT page_id FROM pages WHERE title IN ({','.join('?' * len(titles))})", list(titles))}


def _fill_layer(conn: sqlite3.Connection, layer: Layer, subject: Subject, lead: bytes, taken: set[int], base_url: str,
                meanings: list[names.Meaning]) -> list[dict]:
    own = [c.page_id for c in subject.pages]
    out: list[dict] = []
    if layer.meanings:
        for m in meanings[:layer.slots]:
            out.append({**names.passage(m, base_url), "layer": layer.key})
    room = layer.slots - len(out)
    linked = _linked(conn, own, layer.page_kinds)
    if layer.meanings:
        linked |= _language_pages(conn, meanings)
    chunks = _chunks_for(conn, linked - taken - set(own), subject.name)
    if layer.mentions:
        chunks.update(_mentioning(conn, subject.name, layer.page_kinds, False))
    only_named = set(chunks) - linked
    keep = {pid: cid for pid, cid in chunks.items() if pid not in taken and pid not in own}
    keep = _not_listings(conn, keep)
    for cid in _closest(conn, keep, lead, room, only_named) if room > 0 else []:
        out.append(_passage(conn, cid, layer.key, base_url))
    room = layer.slots - len(out)
    if layer.logs and room > 0:
        logs = {pid: cid for pid, cid in _mentioning(conn, subject.name, frozenset(), True).items() if pid not in own}
        for cid in _closest(conn, logs, lead, room, set(logs)):
            out.append(_passage(conn, cid, layer.key, base_url, LOG_LABEL))
    return out


def _not_listings(conn: sqlite3.Connection, chunks: dict[int, int]) -> dict[int, int]:
    """Auctions, loot lists and yearly digests list things rather than being about one."""
    if not chunks:
        return chunks
    rows = conn.execute(f"SELECT page_id, title FROM pages WHERE page_id IN ({','.join('?' * len(chunks))})", list(chunks))
    return {r[0]: chunks[r[0]] for r in rows if not OFFER_LISTING_TITLE.search(r[1]) and not r[1].startswith("RanikMap")}


def _own_passages(conn: sqlite3.Connection, subject: Subject, slots: int, base_url: str, layer: str) -> list[dict]:
    """The subject's opening chunks, its article first and then its same-named pages in turn (Weapon:Svidaw sis)."""
    order = subject.pages
    per_page = {c.page_id: [r[0] for r in conn.execute("SELECT chunk_id FROM chunks WHERE page_id=? ORDER BY ord LIMIT ?",
                                                       (c.page_id, slots))] for c in order}
    out: list[int] = []
    while len(out) < slots and any(per_page.values()):
        for c in order:
            if per_page[c.page_id] and len(out) < slots:
                out.append(per_page[c.page_id].pop(0))
    return [_passage(conn, cid, layer, base_url) for cid in out]


def gather(conn: sqlite3.Connection, subject: Subject, budget: int, base_url: str) -> list[dict]:
    """The passages of a layered answer, grouped by layer, or [] when only the subject's own pages have anything: then
    there are no parts to write and it is an ordinary answer."""
    lead = _lead_vector(conn, subject.main.page_id)
    if lead is None:
        return []
    table = table_for(subject)
    meanings = names.meanings(conn, [c.title for c in subject.pages])
    parts: list[list[dict]] = []
    taken: set[int] = set()
    for layer in table[1:]:
        if subject.focused and layer.practical:
            continue
        filled = _fill_layer(conn, layer, subject, lead, taken, base_url, meanings)
        taken |= {r[0] for p in filled if p.get("chunk_id") for r in conn.execute(
            "SELECT page_id FROM chunks WHERE chunk_id=?", (p["chunk_id"],))}
        parts.append(filled)
    if not any(parts):
        return []
    used = sum(len(p) for p in parts)
    own = table[0]
    # a layer with nothing gives its slots to what the thing is
    slots = min(FOCUSED_OWN_SLOTS, budget - used) if subject.focused else max(own.slots, budget - used)
    opening = _own_passages(conn, subject, max(1, slots), base_url, own.key)
    return [*opening, *[p for part in parts for p in part]]


def headings(passages: list[dict], table: tuple[Layer, ...]) -> list[str]:
    """The headings of the parts that have passages, in the table's order."""
    present = {p.get("layer") for p in passages}
    return [ly.heading for ly in table if ly.heading and ly.key in present]


def outline(passages: list[dict], table: tuple[Layer, ...]) -> str:
    """What the model is told about the parts, beside the passages (where the 8B model follows style directives)."""
    def numbers(key: str) -> str:
        return ", ".join(f"[{n}]" for n, p in enumerate(passages, 1) if p.get("layer") == key)

    lines = []
    for ly in table:
        cited = numbers(ly.key)
        if not cited:
            continue
        lines.append(f"- First, with no heading, one or two sentences saying what it is, then the detail, from {cited}." if not ly.heading
                     else f"- A line with exactly **{ly.heading}** on it, then sentences from {cited}.")
    note = ""
    if any(p.get("log") for p in passages):
        note = " A passage marked as a player's role-play log is one player's story: say so when you use it, never as fact."
    return ("Answer in these parts, in this order, each only from its own passages. State the facts themselves and end each "
            "sentence with its [n]; never talk about the passages or begin a sentence with a number (not \"[4] mentions\"). If "
            "a part's passages say nothing useful about the subject, leave that part and its heading out. No other headings.\n"
            + "\n".join(lines) + note)


_HEADING_LINE = re.compile(r"^[\s#*_>-]*(?P<h>[^*_:\[\]]{3,60}?)[\s*_:.\-—]*$")
_HEADING_LEAD = re.compile(r"^[\s#*_>-]*(?P<h>[^*_:\[\]]{3,60}?)(?:\*\*|__)?\s*[:—]\s*(?:\*\*|__)?\s*(?P<rest>\S.*)$")
_NEAR = 0.8



def _match(text: str, wanted: list[str]) -> str | None:
    low = text.strip().casefold()
    best = max(wanted, key=lambda h: difflib.SequenceMatcher(None, low, h.casefold()).ratio(), default=None)
    return best if best and difflib.SequenceMatcher(None, low, best.casefold()).ratio() >= _NEAR else None


def _backing(body: str, numbers: list[int], passages: list[dict]) -> int | None:
    """Which of a part's own passages an uncited part says, if any: the 8B model often forgets the [n] on a sound part."""
    ev = Evidence([p["text"] for p in passages])
    words = unit_words(body)
    scored = [(ev.coverage(words, ev.bags[n - 1]), -n) for n in numbers]
    best = max(scored, default=(0.0, 0))
    return -best[1] if best[0] >= SUPPORT_COVERAGE else None


def tidy(answer: str, table: tuple[Layer, ...], passages: list[dict]) -> str:
    """The answer with near-miss headings made exact, in the table's order. A part with no citation is tied to the one of
    its own passages it says, or goes, with its heading, if none does. A draft that used no headings is left as written:
    it is still checked like any other answer."""
    wanted = headings(passages, table)
    parts: list[tuple[str | None, list[str]]] = [(None, [])]
    for line in answer.split("\n"):
        whole = _HEADING_LINE.match(line) if "[" not in line else None
        h = _match(whole["h"], wanted) if whole else None
        if h:
            parts.append((h, []))
            continue
        inline = _HEADING_LEAD.match(line)
        h = _match(inline["h"], wanted) if inline else None
        if h:
            parts.append((h, [inline["rest"]]))
            continue
        parts[-1][1].append(line)
    if len(parts) == 1:
        return answer
    keys = {ly.heading: ly.key for ly in table}
    order = {h: i for i, h in enumerate(wanted)}
    out = ["\n".join(parts[0][1]).strip()]
    seen: dict[str, int] = {}
    for h, body in sorted(parts[1:], key=lambda part: order[part[0]]):
        text = "\n".join(body).strip()
        if not _CITE.search(text):
            numbers = [n for n, p in enumerate(passages, 1) if p.get("layer") == keys[h]]
            n = _backing(text, numbers, passages) if text else None
            if n is None:
                continue
            text += f" [{n}*]"
        if h in seen:  # the same heading twice: one part
            out[seen[h]] += "\n\n" + text
        else:
            seen[h] = len(out)
            out.append(f"**{h}**\n\n{text}")
    return "\n\n".join(p for p in out if p)


def log_note(cited: list[int], passages: list[dict]) -> str:
    """Said by code whenever the answer leans on a role-play log, however the model worded it."""
    logs = [n for n in cited if 1 <= n <= len(passages) and passages[n - 1].get("log")]
    if not logs:
        return ""
    if len(logs) == 1:
        return f"(OOC) [{logs[0]}] is a player's role-play log, not official lore."
    return "(OOC) " + ", ".join(f"[{n}]" for n in logs) + " are players' role-play logs, not official lore."


def offer(conn: sqlite3.Connection, subject: Subject, base_url: str, budget: int) -> str | None:
    """After a narrow answer: the rest of the thing, in words the page's chip can ask, when there is any rest."""
    focused = Subject(subject.main, subject.pages, True)
    if not gather(conn, focused, budget, base_url):
        return None
    return LAYER_OFFER[LAYER_TABLE[subject.main.kind]].replace("{x}", subject.name)
