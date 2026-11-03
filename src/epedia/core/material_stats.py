"""'What material has the least workability?' and any other question about a column of the crafting materials tables.

Search cannot answer these: comparing some 300 rows does not fit in a few passages, and given a few the model picked Glaes one
time and Lead the next, and could not keep straight which way the number runs. Every figure here comes from `facts` (domain
'material', read from Category:Crafting materials) and is written by code, so there is nothing to misread. Questions about an item
("hardest metal for a sword") and how to craft one stay with `materials` and `craft_ranks`.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field

from . import materials
from .craft_ranks import _ALIASES, _Sources, norm
from .materials import _MATTER_WORDS, CATEGORY, PLURAL

HIGH, LOW = "high", "low"
# The rule every workability answer states, as the page does. `epedia doctor` checks the wording is still there.
WORKABILITY_RULE = "Lower workability metals require more skill to masterfully craft"
RULE_SECTION = "Forging materials > Metal properties"
RULES = ((CATEGORY, WORKABILITY_RULE),)
_NAME_ALIASES = {**_ALIASES, "Silversteel": ("silver steel",)}
KINDS = ("metal", "stone", "bone", "wood", "cloth", "leather", "plant")  # the tables' order
AFFINITIES = ("affinity_short", "affinity_long", "affinity_composite")
SHOWN = 5  # how many rows a ranking of one kind shows, ties kept together

_BOW_ALONE = r"(?:highest|lowest|top|worst|least) {bow}|{bow} of"  # "the highest shortbow": "best shortbow" is asking for a bow
# attribute -> (what to call it, the words that ask for it). Affinities first: "composite" also names a bow.
STATS = {
    "affinity_short": ("shortbow / arbalest affinity", r"(?:short ?bow|arbalest)\w* affinity|affinity for (?:a )?(?:short ?bow|arbalest)\w*|" + _BOW_ALONE.format(bow=r"short ?bow")),
    "affinity_long": ("longbow / heavy crossbow affinity", r"(?:long ?bow|hx|heavy crossbow)\w* affinity|affinity for (?:a )?(?:long ?bow|heavy crossbow)\w*|" + _BOW_ALONE.format(bow=r"long ?bow")),
    "affinity_composite": ("composite bow / light crossbow affinity", r"(?:composite(?: bow)?|lx|light crossbow)\w* affinity|affinity for (?:a )?(?:composite|light crossbow)\w*|" + _BOW_ALONE.format(bow=r"composite(?: bow)?")),
    "workability": ("workability", r"work ?ab\w*"),
    "hardness": ("hardness", r"hardness"),
    "durability": ("durability", r"durabilit\w*|durable"),
    "density": ("density", r"densit\w*|dense|weight|weighs?"),
    "physical": ("physical resistance", r"physical(?: resistance)?"),
    "thermal": ("thermal resistance", r"thermal(?: resistance)?|(?:fire|cold|heat) resistance"),
    "electrical": ("electrical resistance", r"electrical(?: resistance)?|lightning resistance"),
    "ductility": ("ductility", r"ductil\w*"),
    "capacity": ("capacity", r"(?:enchant )?capacity"),
    "stiffness": ("stiffness", r"stiffness|stiff"),
    "potency": ("potency", r"potency|potent"),
    "efficacy": ("efficacy", r"efficacy"),
    "solubility": ("solubility", r"solubility|soluble"),
    "toxicity": ("toxicity", r"toxicity|toxic"),
    "value": ("value", r"value|valuable|cost of|price of|how much (?:\w+ ){0,4}(?:cost|costs|worth)"),
}
_WORDS = {attr: re.compile(rf"\b(?:{words})\b") for attr, (_label, words) in STATS.items()}
ORDER = ("hardness", "durability", "workability", "electrical", "thermal", "physical", "ductility", "capacity", "stiffness",
         "affinity_short", "affinity_long", "affinity_composite", "potency", "efficacy", "solubility", "toxicity", "density", "value")
# One word that is both the stat and which way: "hardest metal".
_SUPERLATIVE = {"hardest": ("hardness", HIGH), "softest": ("hardness", LOW), "stiffest": ("stiffness", HIGH),
                "densest": ("density", HIGH), "heaviest": ("density", HIGH),
                "lightest": ("density", LOW), "cheapest": ("value", LOW), "priciest": ("value", HIGH),
                "most expensive": ("value", HIGH), "least expensive": ("value", LOW)}
_COMPARATIVE = {"harder": "hardness", "softer": "hardness", "stiffer": "stiffness", "denser": "density", "heavier": "density", "lighter": "density",
                "cheaper": "value", "pricier": "value", "more expensive": "value"}
_HOW = {"hard": "hardness", "soft": "hardness", "heavy": "density", "light": "density", "expensive": "value"}
_WORKING = r"work|craft|master ?craft|forge|shape|carve|sew|tailor|make"
# Hard to WORK is the workability column, whichever way it is put; only "hardest metal" alone is the hardness one.
_EASE = re.compile(rf"\b(easiest|easier|simplest|hardest|harder|toughest|tougher|most difficult|more difficult|how hard|how easy|how difficult)\b"
                   rf"(?: \w+){{0,6}}? to (?:{_WORKING})\b")
_EASIEST = re.compile(r"\b(?:easiest|simplest)\b")
_OTHER_TASK = r"(?: \w+){0,3}? to \w+"  # "hardest metal to mine": hard to do something else, which no column says
_MOST = re.compile(r"\b(?:most|highest|greatest|maximum|max|best|top|biggest|largest)\b")
_LEAST = re.compile(r"\b(?:least|lowest|worst|minimum|min|smallest|poorest)\b")
_ABOVE = re.compile(r"(above|over|more than|greater than|higher than|at least|>=?)\s*(\d+(?:\.\d+)?)", re.I)
_BELOW = re.compile(r"(below|under|less than|lower than|at most|<=?)\s*(\d+(?:\.\d+)?)", re.I)
_OR_MORE = re.compile(r"(\d+(?:\.\d+)?) or (more|higher|above|less|lower|below)\b", re.I)
_BY = re.compile(r"\b(?:list|rank|sort|order|show)\b.*\bby\b")
_ALL_STATS = re.compile(r"\b(?:stats?|statistics|properties|numbers|compare|compared|comparing|vs|versus|difference between)\b")
_AFFINITY = re.compile(r"\baffinit(?:y|ies)\b")
_CATALYST = re.compile(r"\bcatalysts?\b")
# Ranks and tiers are `craft_ranks`' to answer or to decline: a table figure would look like an answer to a different question.
_NOT_THE_TABLE = re.compile(r"\b(?:buy|sell|sells|shop|store|merchant|where|why|explain|mean|means|affect|affects|obtain|acquire|to find|to get|come by|"
                            r"ranks?|tiers?|t\d{1,2}|multiplier)\b")
# What may follow a material's name when the question is about the material. Anything else is an item: "silversteel rasp".
_AFTER_NAME = {"", "or", "and", "vs", "versus", "than", "has", "have", "is", "are", "compare", "compared", "to", "with", "at", "on",
               "in", "for", "got", "metal", "metals", "wood", "woods", "stone", "bone", "cloth", "leather", "s", "stats", "properties", "work",
               "easier", "harder", "softer", "tougher", "stiffer", "denser", "heavier", "lighter", "cheaper", "pricier", "more", "less", "better",
               "worse", "cost", "costs", "worth", "weigh", "weighs", "hardness", "durability", "workability", "density", "stiffness", "capacity",
               "ductility", "value", "thermal", "electrical", "physical", "potency", "efficacy", "solubility", "toxicity", "affinity", "affinities",
               "shortbow", "longbow", "composite"}
_NOT_A_NAME = {"it", "this", "that", "these", "those", "them", "metal", "metals", "material", "materials", "wood", "woods", "stone", "stones",
               "bone", "bones", "cloth", "leather", "steel"}


@dataclass
class Mat:
    entity: str
    matter: str = ""
    rarity: str = ""
    stats: dict[str, tuple[float, str, str]] = field(default_factory=dict)  # attribute -> (number, as the table has it, section)

    @property
    def name(self) -> str:
        return materials._name(self.entity)

    def num(self, attr: str) -> float:
        return self.stats[attr][0]

    def shown(self, attr: str) -> str:
        return self.stats[attr][1]


@dataclass
class Ask:
    attr: str | None = None  # None: every stat of one named material
    way: str | None = None
    limit: tuple[str, float, bool] | None = None  # above|below, the number, whether it counts itself
    listed: bool = False  # "list metals by workability"


def _limit(question: str) -> tuple[str, float, bool] | None:
    if m := _OR_MORE.search(question):
        return ("above" if m[2].lower() in ("more", "higher", "above") else "below"), float(m[1]), True
    if m := _ABOVE.search(question):
        return "above", float(m[2]), m[1].lower() in ("at least", ">=")
    if m := _BELOW.search(question):
        return "below", float(m[2]), m[1].lower() in ("at most", "<=")
    return None


def read(question: str) -> Ask | None:
    """Which column is being asked about, and which way. None for a question about no column, the usual case, so that a
    question costs no table read unless it needs one."""
    q = norm(question)
    limit = _limit(question)
    plain = q.replace("at least", " ").replace("at most", " ")
    if m := _EASE.search(q):
        word = m[1]
        way = None if word.startswith("how") else HIGH if word in ("easiest", "easier", "simplest") else LOW
        return Ask("workability", way, limit)
    if _EASIEST.search(q):
        return None if re.search(_EASIEST.pattern + _OTHER_TASK, q) else Ask("workability", HIGH, limit)
    way = LOW if _LEAST.search(plain) else HIGH if _MOST.search(plain) else None
    for attr, rx in _WORDS.items():
        if rx.search(q):
            return Ask(attr, way, limit, bool(_BY.search(q)))
    if _AFFINITY.search(q):  # no bow named: all three
        return Ask("affinity", way, limit, bool(_BY.search(q)))
    for word, (attr, side) in _SUPERLATIVE.items():
        if f" {word} " in f" {q} ":
            return None if re.search(rf"\b{word}\b{_OTHER_TASK}", q) else Ask(attr, side, limit)
    for word, attr in _COMPARATIVE.items():
        if f" {word} " in f" {q} ":
            return Ask(attr)
    if m := re.search(r"\bhow (" + "|".join(_HOW) + r")\b", q):
        return Ask(_HOW[m[1]])
    if _ALL_STATS.search(q):
        return Ask()
    return None


def stands_alone(question: str) -> bool:
    """A question about the table, complete as asked. In a conversation about ranks it must not pick up the riste before it:
    "what is the hardest metal?" is not asking for that sum again, while "how hard is it in damite?" is a follow-up."""
    ask = read(question)
    q = norm(question)
    if ask is None or ask.attr is None or _NOT_THE_TABLE.search(q) or re.search(r"\b(?:it|this|that|one|them)\b", q):
        return False
    return bool(ask.way or ask.limit or ask.listed or ask.attr not in _WORDS or _WORDS[ask.attr].search(q))


def _load(conn: sqlite3.Connection) -> dict[str, Mat]:
    """A material in two tables has one row here; the two agree (`epedia doctor` compares the tables with the pages). Ordered so
    the forging table's section wins, the one a reader expects to be sent to."""
    out: dict[str, Mat] = {}
    for r in conn.execute("SELECT entity, grp, attribute, value_num, value_text, section FROM facts "
                          "WHERE domain='material' AND value_num IS NOT NULL ORDER BY grp"):
        matter, rarity, _skill = r["grp"].split("|")
        mat = out.setdefault(r["entity"], Mat(r["entity"]))
        mat.matter, mat.rarity = matter, rarity
        mat.stats[r["attribute"]] = (r["value_num"], r["value_text"], r["section"])
    return out


def _names(mats: dict[str, Mat]) -> dict[str, set[str]]:
    """Every way a material is written -> the rows it may mean. 'Lead' for 'Lead (material)', and plain 'steel' for all three."""
    out: dict[str, set[str]] = {}
    for entity in mats:
        full = norm(entity)
        bare = norm(re.sub(r"\s*\([^)]*\)$", "", entity))
        for form in {full, bare, bare.removesuffix(" leather")}:
            for written in (form, form + "s", form.replace(" ", "")):
                if written not in _NOT_A_NAME or written == "steel":
                    out.setdefault(written, set()).add(entity)
        for alias in _NAME_ALIASES.get(entity, ()):
            out.setdefault(alias, set()).add(entity)
    return out


def _named(question: str, names: dict[str, set[str]]) -> tuple[list[str], bool, str]:
    """(the materials the words name, in order; whether each stands alone rather than naming an item; the words left over)."""
    q = norm(question)
    pattern = r"\b(?:" + "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True)) + r")\b"
    found: list[str] = []
    alone = True
    for m in re.finditer(pattern, q):
        after = q[m.end():].split()
        if (after[0] if after else "") not in _AFTER_NAME:
            alone = False
        found += [e for e in sorted(names[m[0]]) if e not in found]
    return found, alone, re.sub(pattern, " ", q)


def _say_kind(matter: str) -> str:
    return PLURAL.get(matter, matter + "s")


def _cite(src: _Sources, mat: Mat, attr: str) -> str:
    return src.cite(CATEGORY, mat.stats[attr][2], mat.entity)


def _ties(pool: list[Mat], attr: str, way: str) -> list[Mat]:
    end = (max if way == HIGH else min)(m.num(attr) for m in pool)
    return [m for m in pool if m.num(attr) == end]


def _sorted(pool: list[Mat], attr: str, way: str) -> list[Mat]:
    return sorted(pool, key=lambda m: (-m.num(attr) if way == HIGH else m.num(attr), m.name.lower()))


def _join(parts: list[str]) -> str:
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]


def _tail(attr: str, src: _Sources, pool: list[Mat]) -> str:
    """What a reader needs in order not to misread these figures, in the table's own terms."""
    notes = []
    if attr == "workability":
        notes.append(f"Higher workability is easier to craft with; lower needs more skill {src.cite(CATEGORY, RULE_SECTION, WORKABILITY_RULE)}.")
    if attr == "density" and any(m.matter == "cloth" for m in pool):
        notes.append("Cloth density depends on thickness: the table lists fine to heavy, and I've gone by the first figure.")
    if attr == "value":
        notes.append("The table prices each kind its own way (metal per volume, stone per boulder, wood per piece of lumber), so compare within a kind.")
    return "(OOC) " + " ".join(notes) if notes else ""


def _with_tail(lines: list[str], attr: str, src: _Sources, pool: list[Mat]) -> str:
    tail = _tail(attr, src, pool)
    return "\n".join(lines) + (f"\n\n{tail}" if tail else "")


def _none_listed(label: str, what: str, have: list[str]) -> str:
    also = f" What they do list: {', '.join(have)}." if have else ""
    return f"The crafting materials tables don't give a {label} for {what}.{also}"


def _columns(mats: list[Mat]) -> list[str]:
    return [STATS[a][0] for a in ORDER if any(a in m.stats for m in mats)]


def _extreme(src: _Sources, ask: Ask, pool: list[Mat], kinds: list[str], what: str) -> str:
    attr, way = ask.attr, ask.way
    label = STATS[attr][0]
    side = "highest" if way == HIGH else "lowest"
    rows = [m for m in pool if attr in m.stats]
    if not rows:
        return _none_listed(label, what, _columns(pool))
    if len(kinds) == 1:
        top = _ties(rows, attr, way)
        rest = [m for m in _sorted(rows, attr, way) if m not in top]
        lines = [f"Among {what}, the {side} {label} is **{top[0].shown(attr)}**: "
                 f"{_join([f'{m.name} {_cite(src, m, attr)}' for m in top])}."]
        if rest:
            cut = rest[min(SHOWN, len(rest)) - 1].num(attr)
            nearby = [m for m in rest if (m.num(attr) >= cut if way == HIGH else m.num(attr) <= cut)]
            lines += ["", f"Next {side}:", *[f"- **{m.name}:** {m.shown(attr)} {_cite(src, m, attr)}" for m in nearby]]
        return _with_tail(lines, attr, src, rows)
    lines: list[str] = []
    if attr != "value":  # prices are not on one scale across kinds
        top = _ties(rows, attr, way)
        lines += [f"The {side} {label} in the crafting materials tables is **{top[0].shown(attr)}**: "
                  f"{_join([f'{m.name} {_cite(src, m, attr)}' for m in top])}.", ""]
    lines.append(f"{side.capitalize()} of each kind:")
    for kind in kinds:
        among = [m for m in rows if m.matter == kind]
        if among:
            best = _ties(among, attr, way)
            lines.append(f"- **{_say_kind(kind).capitalize()}:** {best[0].shown(attr)}, {_join([f'{m.name} {_cite(src, m, attr)}' for m in best])}")
    return _with_tail(lines, attr, src, rows)


def _listing(src: _Sources, ask: Ask, pool: list[Mat], kinds: list[str], what: str) -> str:
    attr = ask.attr
    label = STATS[attr][0]
    rows = [m for m in pool if attr in m.stats]
    if not rows:
        return _none_listed(label, what, _columns(pool))
    way = ask.way or HIGH
    head = f"{what.capitalize()} by {label}, {'lowest' if way == LOW else 'highest'} first:"
    if ask.limit:
        side, number, itself = ask.limit
        words = ("at least" if itself else "above") if side == "above" else ("at most" if itself else "below")
        keep = [m for m in rows if (m.num(attr) > number if side == "above" else m.num(attr) < number) or (itself and m.num(attr) == number)]
        if not keep:
            return f"None of the {what} in the crafting materials tables has a {label} {words} {number:g}."
        rows = keep
        way = ask.way or (HIGH if side == "above" else LOW)
        head = f"{what.capitalize()} with a {label} {words} {number:g}:"
    lines = [head]
    for kind in kinds or KINDS:
        among = _sorted([m for m in rows if m.matter == kind], attr, way)
        if not among:
            continue
        if len(kinds) != 1:
            lines += ["", f"{_say_kind(kind).capitalize()}:"]
        lines += [f"- **{m.name}:** {m.shown(attr)} {_cite(src, m, attr)}" for m in among]
    return _with_tail(lines, attr, src, rows)


def _compare(src: _Sources, attr: str, chosen: list[Mat]) -> str:
    label = STATS[attr][0]
    have = [m for m in chosen if attr in m.stats]
    lines = [f"{label.capitalize()} in the crafting materials tables:"]
    lines += [f"- **{m.name}:** {m.shown(attr)} {_cite(src, m, attr)}" for m in _sorted(have, attr, HIGH)]
    lines += [f"- **{m.name}:** not listed" for m in chosen if m not in have]
    if len(have) >= 2:
        top, low = _ties(have, attr, HIGH), _ties(have, attr, LOW)
        if len(top) == len(have):
            lines += ["", "They're level."]
        elif attr == "workability":
            lines += ["", f"So {_join([m.name for m in top])} is the easier to work, and {_join([m.name for m in low])} the harder."]
        else:
            lines += ["", f"So {_join([m.name for m in top])} is the higher."]
    return _with_tail(lines, attr, src, have)


def _lookup(src: _Sources, attr: str, mat: Mat, mats: dict[str, Mat]) -> str:
    label = STATS[attr][0]
    if attr not in mat.stats:
        return _none_listed(label, mat.name, _columns([mat]))
    lines = [f"**{mat.name}** has a {label} of **{mat.shown(attr)}** {_cite(src, mat, attr)}."]
    same = [m for m in mats.values() if m.matter == mat.matter and attr in m.stats]
    if attr != "value" and len(same) > 1:
        lines[0] += (f" {_say_kind(mat.matter).capitalize()} in the table run from {min(m.num(attr) for m in same):g} "
                     f"to {max(m.num(attr) for m in same):g}.")
    return _with_tail(lines, attr, src, [mat])


def _all_stats(src: _Sources, mat: Mat) -> str:
    cited = next(iter(mat.stats))
    shown = ", ".join(f"{STATS[a][0]} {mat.shown(a)}" for a in ORDER if a in mat.stats)
    rarity = f"{mat.rarity.lower()} {mat.matter}".strip()
    return f"**{mat.name}** ({rarity}): {shown} {_cite(src, mat, cited)}."


def _one_column(src: _Sources, ask: Ask, attr: str, pool: list[Mat], kinds: list[str], what: str, chosen: list[Mat], mats: dict[str, Mat]) -> str | None:
    one = Ask(attr, ask.way, ask.limit, ask.listed)
    if ask.limit or ask.listed:
        return _listing(src, one, pool, kinds, what)
    if len(chosen) >= 2:
        return _compare(src, attr, chosen)
    if ask.way:
        return _extreme(src, one, pool, kinds or list(KINDS), what)
    return _lookup(src, attr, chosen[0], mats) if chosen else None


def passage(conn: sqlite3.Connection, question: str, base_url: str) -> list[dict] | None:
    """[passages, the first carrying the code-written `answer`] when this asks about a column of the materials tables."""
    ask = read(question)
    q = norm(question)
    if ask is None or _NOT_THE_TABLE.search(q):
        return None
    affinity = (ask.attr or "").startswith("affinity")
    if any(rx.search(question) for kind, rx in materials._ITEM.items() if not (affinity and kind == "bow")):
        return None  # about an item, which `materials` ranks by what matters for that item
    mats = _load(conn)
    if not mats:
        return None
    found, alone, rest = _named(question, _names(mats))
    if not alone:
        return None
    words = rest.split()
    kinds = [k for k in KINDS if any(_MATTER_WORDS.get(w, w.removesuffix("s")) == k for w in words)]
    if _CATALYST.search(q):
        kinds = ["metal", "plant"]  # the page's alchemy tables: "Catalysts used in Alchemy"
    affinity = (ask.attr or "").startswith("affinity")
    if affinity and not kinds:
        kinds = ["wood"]  # only wood has a bow affinity
    scoped = bool(found or kinds or any(w in ("material", "materials") for w in words) or ask.attr == "workability")
    if not scoped:
        return None  # "what has the highest durability?" could be anything
    src = _Sources(conn, base_url)
    pool = [m for m in mats.values() if not kinds or m.matter in kinds]
    rarity = next((w for w in ("common", "rare", "special") if w in q.split()), None)
    if rarity == "common":
        pool = [m for m in pool if m.rarity == "Common"]
    elif rarity == "rare":  # the page's "Rare bone" section holds rows it labels Special: both are what a player calls rare
        pool = [m for m in pool if m.rarity != "Common"]
    elif rarity == "special":
        pool = [m for m in pool if m.rarity == "Special"]
    what = " and ".join(_say_kind(k) for k in kinds) or "materials"
    if rarity:
        what = f"{rarity} {what}"
    chosen = [mats[e] for e in found]
    if ask.attr is None:
        if not chosen:
            return None
        answer = "\n".join(_all_stats(src, m) for m in chosen)
    elif parts := [p for a in (AFFINITIES if ask.attr == "affinity" else (ask.attr,))
                   if (p := _one_column(src, ask, a, pool, kinds, what, chosen, mats))]:
        answer = "\n\n".join(parts)
    else:
        m = re.search(r"\bworkability of (?:an? |the )?([a-z][a-z ]*)$", q) if ask.attr == "workability" else None
        if not m or m[1].split()[0] in _NOT_A_NAME:
            return None
        answer = f"I don't have \"{m[1]}\" in the crafting materials tables, so I can't give you a workability for it."
    if not src.out:  # an answer that names no row still shows where it looked
        src.cite(CATEGORY)
    out = src.out
    out[0]["answer"] = answer
    return out
