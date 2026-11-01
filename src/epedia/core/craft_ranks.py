"""'How many ranks to mastercraft a half-handled riste in glaes?' answered in code, every step shown and cited.

Three lookups and one sum: the material's workability (Category:Crafting materials), the multiplier that gives (the Crafting
page's formula), the item's tier (the Difficulty column of `<discipline> products`), and that tier's rank figure (the Crafting
page's chart, then the Tailoring player guide where the chart has a "?"). A real conversation showed why this cannot be left to
the model: it took "Tier 6" from a Talk page, read "11- v.difficult" as 11 ranks, and could not be steered back.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass

from . import materials
from .materials import CATEGORY

CRAFTING = "Crafting"
GUIDE = "Tailoring player guide"
TRAINED = 1750  # Player suggestions for Crafting: "fully capped out with techniques (1750 ranks)"
BUFF = 0.30  # DragonRealms Updates (2019): buff limits, crafting skills included, extended to 30%
TOP = round(TRAINED * (1 + BUFF))
CELL_TOLERANCE = 0.05  # the chart's own cell is used when it agrees with the sum; it holds typos ("610 / ???" at Tier 6)
T12_FLOOR = 40

# Rules the answers rest on, with the wording that must stay on the page. `epedia doctor` checks each one.
FORMULA_LOW = "Multiplier = 100 + ((100 - workability) - 50) * 4)"
FORMULA_HIGH = "Multiplier = (((100 - workability) * 120 / 100) + 40)"
T12_RULE = "impossible to mastercraft a T12 recipe with materials below 40 workability"
TRAINED_NOTE = "(1750 ranks)"
BUFF_NOTE = "bonus buff limits extended"
ASSIST_NOTE = "Up to 3 characters may assist"
RULES = ((CRAFTING, FORMULA_LOW), (CRAFTING, FORMULA_HIGH), (CRAFTING, T12_RULE), (CRAFTING, ASSIST_NOTE),
         ("Player suggestions for Crafting", TRAINED_NOTE), ("DragonRealms Updates (2019)", BUFF_NOTE))

_ASK = re.compile(r"\b(ranks?|workability|difficult\w*|how hard|master ?craft\w*|masterful\w*|the math)\b", re.I)
_CRAFT = re.compile(r"\b(craft\w*|make|making|made|forge\w*|forging|shape|shaping|carve|carving|sew|sewing|tailor\w*|"
                    r"master ?craft\w*|masterful\w*|workability|template|the math)\b", re.I)
_OTHER_USE = re.compile(r"\b(wield\w*|use|using|hunt\w*|train\w*|skin\w*|appraise\w*|swing\w*|teach\w*)\b", re.I)
_TIER = re.compile(r"\b(?:t|tier)\s*(\d{1,2})\b", re.I)

# Names the materials table does not use for its steels.
_ALIASES = {"Steel (HCS)": ("high carbon steel", "hcs"), "Steel (MCS)": ("medium carbon steel", "mcs"),
            "Steel (LCS)": ("low carbon steel", "lcs")}
_SAY = {"Steel (HCS)": "high carbon steel", "Steel (MCS)": "medium carbon steel", "Steel (LCS)": "low carbon steel"}
# What a row is made of when it says so itself (<metal>), then its page section, then its discipline. None: the discipline's
# skill decides, which suits Remedies. Artificing has no materials table at all, so nothing fits it.
_PLACEHOLDER = {"metal": "metal", "stone": "stone", "bone": "bone", "wood": "wood", "wooden": "wood", "cloth": "cloth",
                "knitted": "cloth", "fabric": "cloth", "leather": "leather"}
_SECTION_MATTER = {"stone": "stone", "bone": "bone", "wood": "wood", "cloth": "cloth", "leather": "leather"}
_DISCIPLINE_MATTER = {"Armorsmithing": {"metal"}, "Blacksmithing": {"metal"}, "Weaponsmithing": {"metal"}, "Shaping": {"wood"},
                      "Carving": {"stone", "bone"}, "Tailoring": {"cloth", "leather"}}


@dataclass
class Row:
    name: str
    discipline: str
    section: str
    tier: int | None = None
    raw: str = ""
    technique: str = ""
    source: str = ""
    placeholder: str = ""


@dataclass
class Material:
    entity: str
    workability: float
    matter: str
    skill: str
    section: str


@dataclass
class Figure:
    lo: float
    hi: float
    approx: bool
    guide: bool


def norm(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text.lower().replace("'", "").replace("’", "")).split())


def multiplier(workability: float) -> float:
    """The Crafting page's formula, as a percentage."""
    if workability < 50:
        return 100 + ((100 - workability) - 50) * 4
    return ((100 - workability) * 120 / 100) + 40


def _sum_text(workability: float) -> str:
    w = f"{workability:g}"
    if workability < 50:
        return f"100 + ((100 − {w}) − 50) × 4 = **{multiplier(workability):g}%**"
    return f"((100 − {w}) × 120 / 100) + 40 = **{multiplier(workability):g}%**"


def _say(entity: str) -> str:
    return _SAY.get(entity, materials._name(entity).lower())


def _load_materials(conn: sqlite3.Connection) -> dict[str, list[Material]]:
    out: dict[str, list[Material]] = {}
    for r in conn.execute("SELECT entity, grp, value_num, section FROM facts WHERE domain='material' AND attribute='workability' "
                          "AND value_num IS NOT NULL"):
        matter, _, skill = r["grp"].split("|")
        out.setdefault(r["entity"], []).append(Material(r["entity"], r["value_num"], matter, skill, r["section"]))
    return out


def _load_rows(conn: sqlite3.Connection) -> list[Row]:
    rows: dict[tuple, Row] = {}
    for r in conn.execute("SELECT p.title, f.entity, f.grp, f.attribute, f.value_num, f.value_text FROM facts f "
                          "JOIN pages p USING (page_id) WHERE f.domain='craft_product'"):
        key = (r["title"], r["entity"], r["grp"])
        row = rows.setdefault(key, Row(r["entity"], r["title"].removesuffix(" products"), r["grp"]))
        if r["attribute"] == "tier":
            row.tier = int(r["value_num"]) if r["value_num"] is not None else None
            row.raw = r["value_text"]
        elif r["attribute"] in ("technique", "source", "placeholder"):
            setattr(row, r["attribute"], r["value_text"])
    return list(rows.values())


def _find(variants: dict[str, str], text: str) -> tuple[str, str] | None:
    """The longest name in `text`, on whole words: (what it stands for, the words as found)."""
    padded = f" {text} "
    best = max((v for v in variants if f" {v} " in padded), key=len, default=None)
    return (variants[best], best) if best else None


def _without(text: str, words: str) -> str:
    return " ".join(f" {text} ".replace(f" {words} ", " ", 1).split())


@dataclass
class Named:
    item: str | None = None
    material: str | None = None
    tier: int | None = None
    plain_steel: bool = False
    several_materials: bool = False


def _read(text: str, items: dict[str, str], mats: dict[str, str]) -> Named:
    """What the words name. Item and material must not claim the same words: the longer name keeps them."""
    q = norm(text)
    item, mat = _find(items, q), _find(mats, q)
    if item and mat and (f" {mat[1]} " in f" {item[1]} " or f" {item[1]} " in f" {mat[1]} "):
        if len(mat[1]) >= len(item[1]):
            item = _find(items, _without(q, mat[1]))
        else:
            mat = _find(mats, _without(q, item[1]))
    rest = _without(q, mat[1]) if mat else q
    if item:
        rest = _without(rest, item[1])
    tier = next((int(m.group(1)) for m in _TIER.finditer(text) if 1 <= int(m.group(1)) <= 12), None)
    return Named(item[0] if item else None, mat[0] if mat else None, tier,
                 not mat and bool(re.search(r"\bsteel\b", rest)), bool(mat and _find(mats, rest)))


def _variants(names, extra=None) -> dict[str, str]:
    out: dict[str, str] = {}
    for name in names:
        n = norm(name)
        out.update({n: name, n + "s": name, n.replace(" ", ""): name})
    for name, words in (extra or {}).items():
        if name in names:
            out.update({w: name for w in words})
    return out


class _Sources:
    """The numbered passages, added as the answer cites them."""

    def __init__(self, conn: sqlite3.Connection, base_url: str):
        self.conn, self.base = conn, base_url.rstrip("/")
        self.out: list[dict] = []
        self.seen: dict[tuple, int] = {}

    def has(self, title: str, contains: str) -> bool:
        return self.conn.execute("SELECT 1 FROM chunks WHERE title=? AND instr(text, ?) LIMIT 1", (title, contains)).fetchone() is not None

    def cite(self, title: str, section: str = "", contains: str = "") -> str:
        key = (title, section, contains)
        if key not in self.seen:
            tries = [("section_path LIKE ? AND instr(text, ?)", (f"{section}%", contains)), ("section_path LIKE ?", (f"{section}%",)),
                     ("instr(text, ?)", (contains,))]
            r = None
            for where, args in tries:
                r = self.conn.execute(f"SELECT chunk_id, section_path, anchor, text FROM chunks WHERE title=? AND {where} "
                                      "ORDER BY ord LIMIT 1", (title, *args)).fetchone()
                if r:
                    break
            same = next((i for i, p in enumerate(self.out, 1) if r and p["chunk_id"] == r["chunk_id"]), None)
            if same:
                self.seen[key] = same
                return f"[{same}]"
            url = self.base + "/" + title.replace(" ", "_") + (f"#{r['anchor']}" if r and r["anchor"] else "")
            self.out.append({"title": title, "section": r["section_path"] if r else section, "url": url,
                             "text": r["text"] if r else contains, "chunk_id": r["chunk_id"] if r else None})
            self.seen[key] = len(self.out)
        return f"[{self.seen[key]}]"


def _figures(conn: sqlite3.Connection, tier: int) -> dict[str, Figure | str | None]:
    """{'name': ..., 'techs': Figure, 'base': Figure}: the chart first, the guide for what the chart leaves blank."""
    got: dict[tuple[str, str], sqlite3.Row] = {
        (r["grp"], r["attribute"]): r for r in conn.execute(
            "SELECT grp, attribute, value_num, value_text FROM facts WHERE domain='craft_tier' AND entity=?", (f"Tier {tier}",))}
    out: dict[str, Figure | str | None] = {"name": None, "techs": None, "base": None}
    for source in ("chart", "guide"):
        if out["name"] is None and (source, "name") in got:
            out["name"] = got[(source, "name")]["value_text"]
        for side in ("techs", "base"):
            r = got.get((source, side))
            if out[side] is None and r is not None and r["value_num"] is not None:
                numbers = [float(n.replace(",", "")) for n in re.findall(r"\d[\d,]*", r["value_text"])]
                out[side] = Figure(min(numbers), max(numbers), "~" in r["value_text"], source == "guide")
    return out


def _cell(conn: sqlite3.Connection, tier: int, side: str, workability: float) -> float | None:
    r = conn.execute("SELECT value_num FROM facts WHERE domain='craft_tier' AND entity=? AND grp='chart' AND attribute=?",
                     (f"Tier {tier}", f"{side}@{workability:g}")).fetchone()
    return r["value_num"] if r else None


def _result(conn: sqlite3.Connection, fig: Figure, tier: int, side: str, mult: float, workability: float) -> tuple[float, float, str]:
    """(lowest, highest, the sum as text). The chart's cell for this workability wins when it agrees with the sum."""
    factor = mult / 100
    lo, hi = fig.lo * factor, fig.hi * factor
    start = f"{fig.lo:,.0f}-{fig.hi:,.0f}" if fig.lo != fig.hi else ("~" if fig.approx else "") + f"{fig.lo:,.0f}"
    sum_text = f"{start} × {factor:g}"
    if fig.lo != fig.hi:
        return lo, hi, f"{sum_text} = **{lo:,.0f}-{hi:,.0f} ranks**"
    cell = _cell(conn, tier, side, workability)
    if cell is not None and lo and abs(cell - lo) / lo <= CELL_TOLERANCE:
        lo = hi = cell
    about = "about " if fig.approx or lo != round(lo) else ""
    return lo, hi, f"{sum_text} = **{about}{lo:,.0f} ranks**"


def _can_it_be_done(src: _Sources, with_techs: tuple[float, float], without: float, technique: str) -> list[str]:
    """Compared with the most anyone can reach, and only when that is in question."""
    if without <= TRAINED and with_techs[1] <= TRAINED:
        return []
    if not (src.has("Player suggestions for Crafting", TRAINED_NOTE) and src.has("DragonRealms Updates (2019)", BUFF_NOTE)):
        return []
    trained = src.cite("Player suggestions for Crafting", contains=TRAINED_NOTE)
    buff = src.cite("DragonRealms Updates (2019)", contains=BUFF_NOTE)
    limit = (f"A player's note puts the most you can train at {TRAINED:,} ranks {trained}, and with buffs limited to {BUFF:.0%} {buff}, "
             f"that's {TOP:,} at best.")
    named = f"the technique, {technique}" if technique else "techs"
    if with_techs[1] <= TRAINED:
        if without <= TOP:
            return [f"**Can it be done?** Yes with {named}, even without a buff. Without it, you'd need about a "
                    f"{without / TRAINED - 1:.0%} buff on the {TRAINED:,} a player's note gives as the most you can train {trained}, "
                    f"within the {BUFF:.0%} buff limit {buff}."]
        return [f"**Can it be done?** Yes with {named}, even without a buff. Without it, it's out of reach. {limit}"]
    if with_techs[0] > TOP:
        text = f"**Can it be done?** Not by the numbers: it's out of reach even with {named}. {limit}"
    elif with_techs[1] > TOP:
        text = f"**Can it be done?** Only at the very top, and only with {named}. {limit} That reaches the low end of the range but not the high end."
    else:
        text = f"**Can it be done?** Only with {named} and a large buff. {limit}"
    if src.has(CRAFTING, ASSIST_NOTE):
        text += (" Up to 3 people in your group can also use CRAFT ASSIST for a small boost, though the page doesn't say how much "
                 f"{src.cite(CRAFTING, 'Group Assistance', ASSIST_NOTE)}.")
    return [text]


def _ooc(used_guide: bool, similar: str = "") -> str:
    note = (f"(OOC) {similar}The multiplier formula was reverse engineered by players. These figures assume 70-quality material and treat "
            "workability 50 as 100%, as the Crafting page's table does, though its footnote says 35-40. A career or hobby, good tools "
            "and better material all lower the real number, but the wiki doesn't say by how much.")
    if used_guide:
        note += " Figures from the player guide are its author's, who says they checked up to Tier 5."
    return note


def _span(fig: Figure) -> str:
    return f"{fig.lo:,.0f}-{fig.hi:,.0f}" if fig.lo != fig.hi else ("~" if fig.approx else "") + f"{fig.lo:,.0f}"


def _cite_figure(src: _Sources, fig: Figure) -> str:
    return src.cite(GUIDE, "Skills and Techniques") if fig.guide else src.cite(CRAFTING, "Difficulty", "Instruction Difficulty")


def _ranks(src: _Sources, conn: sqlite3.Connection, mat: Material, tier: int, technique: str = "") -> tuple[list[str], bool]:
    """The page's hard rule, the sum on each side, and whether it can be done: (lines, whether the guide was used)."""
    mult = multiplier(mat.workability)
    figs = _figures(conn, tier)
    techs, base = figs["techs"], figs["base"]
    lines: list[str] = []
    rule = tier == 12 and mat.workability < T12_FLOOR and src.has(CRAFTING, T12_RULE)
    if rule:
        lines += [f"**It can't be mastercrafted.** The Crafting page says a Tier 12 recipe is currently impossible to mastercraft with "
                  f"materials below {T12_FLOOR} workability, even with 2,100 effective ranks, the techniques, and 99-quality tools and "
                  f"materials {src.cite(CRAFTING, 'Difficulty', T12_RULE)}. {_say(mat.entity).capitalize()} is {mat.workability:g}.", ""]
    if not isinstance(techs, Figure) or not isinstance(base, Figure):
        return [*lines, f"My notes have no rank figure for Tier {tier}, so I can't work out the ranks, and I won't guess."], False
    tcite, bcite = _cite_figure(src, techs), _cite_figure(src, base)
    both = tcite if tcite == bcite else tcite + bcite
    lines += [f"Tier {tier} takes {_span(techs)} ranks with techs and {base.lo:,.0f} without {both}.", "",
              f"- **With techs:** {_result(conn, techs, tier, 'techs', mult, mat.workability)[2]}",
              f"- **Without techs:** {_result(conn, base, tier, 'base', mult, mat.workability)[2]}"]
    factor = mult / 100
    done = _can_it_be_done(src, (techs.lo * factor, techs.hi * factor), base.lo * factor, technique)
    if rule and techs.lo * factor <= TOP:  # the page has already said no; the sum only backs it up when it agrees
        done = []
    if done:
        lines += ["", *done]
    return lines, techs.guide or base.guide


def _bullets(src: _Sources, mat: Material) -> list[str]:
    return [f"- **{_say(mat.entity).capitalize()}:** workability {mat.workability:g} {src.cite(CATEGORY, mat.section, materials._name(mat.entity))}",
            f"- **Multiplier:** {_sum_text(mat.workability)} {src.cite(CRAFTING, 'Difficulty > Workability', FORMULA_LOW)}"]


def _fits(row: Row, mat: Material, skill_of: dict[str, str]) -> bool:
    wanted = _matters(row)
    if wanted:
        return mat.matter in wanted
    return skill_of.get(row.discipline) == mat.skill


def _matters(row: Row) -> set[str]:
    if row.placeholder in _PLACEHOLDER:
        return {_PLACEHOLDER[row.placeholder]}
    top = row.section.split(" > ")[0].split(" (")[0].casefold()
    if top in _SECTION_MATTER:
        return {_SECTION_MATTER[top]}
    return _DISCIPLINE_MATTER.get(row.discipline, set())


def _item_line(src: _Sources, row: Row, tier_name: str) -> str:
    cite = src.cite(f"{row.discipline} products", row.section, row.name)
    line = f"- **{row.name[:1].upper() + row.name[1:]}:** Tier {row.tier}, {tier_name}, in {row.discipline} {cite}"
    if row.source:
        line += f". It needs its own instructions, which the page lists as coming from {row.source} {cite}"
    return line


def _answer_for_item(conn: sqlite3.Connection, src: _Sources, item: str, rows: list[Row], choices: list[Material], all_rows: list[Row]) -> str:
    skill_of = {r[0]: r[1] for r in conn.execute("SELECT entity, grp FROM facts WHERE domain='crafting' AND attribute='discipline_of'")}
    pairs = [(row, mat) for row in rows for mat in choices if _fits(row, mat, skill_of)]
    if not pairs:
        row, mat = rows[0], choices[0]
        cite = src.cite(CATEGORY, mat.section, materials._name(mat.entity))
        page = src.cite(f"{row.discipline} products", row.section, row.name)
        wanted = " or ".join(sorted(_matters(row))) or "materials without a workability I can read"
        return (f"I can't give you a number for that. {_say(mat.entity).capitalize()} is {mat.matter} {cite}, and {row.name} is a "
                f"{row.discipline} item {page}, which is made from {wanted}.")
    mat = pairs[0][1]
    distinct = {(p[0].discipline, p[0].tier): p[0] for p in pairs if p[1] is mat}
    title = item[:1].upper() + item[1:]
    lines = [f"{title} in {_say(mat.entity)}, to mastercraft:", "", *_bullets(src, mat)]
    guided = False
    for row in distinct.values():
        if row.tier is None:
            cite = src.cite(f"{row.discipline} products", row.section, row.name)
            shown = f'"{row.raw}"' if row.raw else "nothing"
            lines.append(f"- **{title}:** the {row.discipline} products page gives {shown} for its difficulty {cite}, so I have no tier to work from.")
            continue
        figs = _figures(conn, row.tier)
        name = figs["name"] if isinstance(figs["name"], str) else row.raw
        lines += [_item_line(src, row, name), ""]
        block, used = _ranks(src, conn, mat, row.tier, row.technique)
        guided |= used
        lines += [*block, ""]
    others = sorted({(r.tier, r.name) for r in all_rows
                     if r.tier and r.name.casefold() != item.casefold() and f" {norm(item)} " in f" {norm(r.name)} " and _fits(r, mat, skill_of)})
    similar = "Other templates by a similar name: " + ", ".join(f"{name} is Tier {tier}" for tier, name in others[:3]) + ". " if others else ""
    if any(r.tier for r in distinct.values()):
        lines.append(_ooc(guided, similar))
    return "\n".join(lines).strip()


def _answer_for_tier(conn: sqlite3.Connection, src: _Sources, tier: int, mat: Material) -> str:
    figs = _figures(conn, tier)
    name = figs["name"] if isinstance(figs["name"], str) else ""
    lines = [f"A Tier {tier} item in {_say(mat.entity)}, to mastercraft:", "", *_bullets(src, mat)]
    if name:
        lines.append(f"- **Tier {tier}:** {name} {src.cite(CRAFTING, 'Difficulty', 'Instruction Difficulty')}")
    block, used = _ranks(src, conn, mat, tier)
    return "\n".join([*lines, "", *block, "", _ooc(used)])


def _answer_needs_material(conn: sqlite3.Connection, src: _Sources, item: str, rows: list[Row]) -> str:
    row = next((r for r in rows if r.tier), None)
    if row is None:
        cite = src.cite(f"{rows[0].discipline} products", rows[0].section, rows[0].name)
        return f"The {rows[0].discipline} products page lists {item} but gives no tier for it {cite}, so I have nothing to work from."
    figs = _figures(conn, row.tier)
    name = figs["name"] if isinstance(figs["name"], str) else row.raw
    lines = ["That depends on the material, because its workability multiplies the ranks. Which material do you mean?", "",
             _item_line(src, row, name)]
    techs, base = figs["techs"], figs["base"]
    if isinstance(techs, Figure) and isinstance(base, Figure):
        tcite, bcite = _cite_figure(src, techs), _cite_figure(src, base)
        both = tcite if tcite == bcite else tcite + bcite
        lines.append(f"- **At workability 50 (100%):** {_span(techs)} ranks with techs and {base.lo:,.0f} without, to mastercraft {both}")
    return "\n".join(lines)


def _answer_needs_item(src: _Sources, mat: Material) -> str:
    return "\n".join([f"I can't find that item on the crafting products pages, so I don't have its tier and can't give you the ranks. "
                      f"Here's what {_say(mat.entity)} does to any of them:", "", *_bullets(src, mat)])


def passage(conn: sqlite3.Connection, question: str, base_url: str, earlier: list[str] | None = None) -> list[dict] | None:
    """[a passage carrying the code-written `answer`, the passages it cites], or None when this is not such a question.
    `earlier` (newest first) fills in what a follow-up leaves out: "show me the math, how many ranks?"."""
    if not _ASK.search(question) or materials.wants(question):
        return None
    if _OTHER_USE.search(question) and not _CRAFT.search(question):
        return None
    all_mats = _load_materials(conn)
    all_rows = _load_rows(conn)
    if not all_mats or not all_rows:
        return None
    items = _variants({r.name for r in all_rows})
    mats = _variants(set(all_mats), _ALIASES)
    named = _read(question, items, mats)
    if named.several_materials:
        return None
    short = len(norm(question).split()) <= 5
    if earlier and (_CRAFT.search(question) or short):
        for past in earlier:
            if (named.item or named.tier) and (named.material or named.plain_steel):
                break
            before = _read(past, items, mats)
            if not (named.material or named.plain_steel) and before.material and not before.several_materials:
                named.material = before.material
            if not (named.item or named.tier):
                named.item, named.tier = before.item, before.tier
    src = _Sources(conn, base_url)
    if named.plain_steel:
        choices = ", ".join(f"{_say(e)} ({all_mats[e][0].workability:g})" for e in _SAY if e in all_mats)
        src.cite(CATEGORY, "Forging materials > Common metals", "Steel (HCS)")
        answer = f"Which steel do you mean? Workability changes the answer, and there are three: {choices} [1]."
    elif named.item and named.material:
        rows = [r for r in all_rows if r.name == named.item]
        answer = _answer_for_item(conn, src, named.item, rows, all_mats[named.material], all_rows)
    elif named.tier and named.material:
        answer = _answer_for_tier(conn, src, named.tier, all_mats[named.material][0])
    elif named.item and (_CRAFT.search(question) or re.search(r"\brank|\bdifficult|\bhard\b", question, re.I)):
        answer = _answer_needs_material(conn, src, named.item, [r for r in all_rows if r.name == named.item])
    elif named.material and (_CRAFT.search(question) or re.search(r"\bhow many ranks\b", question, re.I)) and "workability of" not in question.lower():
        answer = _answer_needs_item(src, all_mats[named.material][0])
    else:
        return None
    out = src.out
    out[0]["answer"] = answer
    return out
