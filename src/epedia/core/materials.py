"""'Best material for X?' answered from the crafting-material tables, in code.

The materials (metals, woods...) and every stat come from `facts` (domain 'material', read from the wiki's
Category:Crafting materials tables by index/extract/materials.py). The rules for what counts are the wiki's own and
each is tied to the page that states it: hardness drives weapon damage and density helps a weapon only up to 7.5
(Forging guide, Metallurgy), armor protection follows the resistances and keeps improving with density, and bows
follow the wood's affinity for the bow type. The model never sees the table, so it cannot misread or embellish it.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass

WEAPON_CAP = 7.5  # Forging guide: weapon templates cap at 7.5 density
MIN_DENSITY = 3.0  # nothing can be crafted from an ingot below this
LATIN = r"(?:[a-z'’\-]+)"

_BEST = re.compile(r"\b(best|better|good|great|ideal|strongest|hardest|top|recommend\w*|should i (?:use|make)|"
                   r"worth|optimal|most effective|choose|pick)\b", re.I)
_MATERIAL = re.compile(r"\b(material|materials|metal|metals|wood|woods|alloy|steel|ingot|ingots|what to make|"
                       r"made (?:of|from|out of)|make (?:it|them) (?:of|from|out of)|out of)\b", re.I)
WEAPONS = (r"weapon|weapons|sword|swords|blade|dagger|scimitar|rapier|sabre|saber|axe|mace|maul|hammer|spear|halberd|pike|lance|"
           r"flail|katana|claymore|falchion|cutlass|dirk|stiletto|misericorde|khopesh|glaive|poleaxe|morning star|"
           r"warhammer|estoc|greatsword|longsword|broadsword|shortsword")
ARMOR = (r"armor|armour|plate|chain|mail|helm|helmet|gauntlets?|greaves|breastplate|hauberk|vambraces?|pauldrons?|"
         r"cuirass|gloves|aventail|shield")
BOWS = r"bow|bows|crossbow|crossbows|arbalest|shortbow|longbow|composite"
_ITEM = {"bow": re.compile(rf"\b({BOWS})\b", re.I), "armor": re.compile(rf"\b({ARMOR})\b", re.I),
         "weapon": re.compile(rf"\b({WEAPONS})\b", re.I)}
_BOW_TYPE = (("short", re.compile(r"\b(short ?bow|arbalest)\b", re.I)),
             ("long", re.compile(r"\b(long ?bow|heavy crossbow|hx)\b", re.I)),
             ("composite", re.compile(r"\b(composite|light crossbow|lx)\b", re.I)))
BOW_LABEL = {"short": "shortbow / arbalest", "long": "longbow / heavy crossbow", "composite": "composite bow / light crossbow"}
FORGING = "Forging guide"
CATEGORY = "Category:Crafting materials"


# Materials other than forged metal and bow wood: leather, cloth, bone, stone. Named in the question, they pick the table.
OTHER_MATTER = {"leather": ("leather", "Outfitting"), "hide": ("leather", "Outfitting"), "cloth": ("cloth", "Outfitting"),
                "silk": ("cloth", "Outfitting"), "fabric": ("cloth", "Outfitting"), "bone": ("bone", "Engineering"),
                "bones": ("bone", "Engineering"), "stone": ("stone", "Engineering"), "rock": ("stone", "Engineering")}
_OTHER = re.compile(r"\b(" + "|".join(OTHER_MATTER) + r")\b", re.I)


@dataclass
class Ask:
    kind: str  # weapon | armor | bow | other (a named material with no item we know)
    item: str  # the word that named it
    bow: str | None = None  # short | long | composite
    matter: str | None = None  # leather | cloth | bone | stone when the question names one


def wants(question: str) -> Ask | None:
    """The item class being asked about, if this is a 'best material for X' question for a kind we rank."""
    other = _OTHER.search(question)
    if not (_BEST.search(question) and (_MATERIAL.search(question) or other)):
        return None
    matter = other.group(1).lower() if other else None
    for kind in ("bow", "armor", "weapon"):  # a "plate sword" is a weapon, but "bow" and "armor" words win first
        m = _ITEM[kind].search(question)
        if m:
            if matter and kind == "bow":
                break  # a bow made of bone is not in the tables' rules; it is answered as the named material
            bow = next((b for b, rx in _BOW_TYPE if rx.search(question)), None) if kind == "bow" else None
            return Ask(kind, m.group(1).lower(), bow, matter)
    return Ask("other", matter, None, matter) if matter else None


def _load(conn: sqlite3.Connection, matter: str, craft: str) -> dict[tuple[str, str], dict[str, float]]:
    """{(name, rarity): {attribute: number}} for one kind of material."""
    out: dict[tuple[str, str], dict[str, float]] = {}
    for r in conn.execute(
            "SELECT entity, grp, attribute, value_num FROM facts WHERE domain='material' AND grp LIKE ?",
            (f"{matter}|%|{craft}",)):
        if r["value_num"] is None:
            continue
        rarity = r["grp"].split("|")[1]
        out.setdefault((r["entity"], rarity), {})[r["attribute"]] = r["value_num"]
    return out


def _name(entity: str) -> str:
    return re.sub(r" \((?:material|metal|wood|stone|bone)\)$", "", entity)


def _fmt(x: float | None) -> str:
    return "?" if x is None else f"{x:g}"


def _rank(rows: dict[tuple[str, str], dict[str, float]], key, need: tuple[str, ...]) -> list[tuple[str, str, dict]]:
    ranked = [(n, rar, s) for (n, rar), s in rows.items() if all(a in s for a in need)]
    ranked.sort(key=lambda t: key(t[2]), reverse=True)
    return ranked


def _split(ranked, n_common: int = 4, n_rare: int = 4):
    common = [t for t in ranked if t[1] == "Common"][:n_common]
    rare = [t for t in ranked if t[1] != "Common"][:n_rare]
    return common, rare


def _chunk(conn: sqlite3.Connection, title: str, section_like: str, base_url: str) -> dict | None:
    r = conn.execute("SELECT chunk_id, section_path, anchor, text FROM chunks WHERE title=? AND section_path LIKE ? "
                     "ORDER BY ord LIMIT 1", (title, section_like)).fetchone()
    if not r:
        return None
    url = base_url.rstrip("/") + "/" + title.replace(" ", "_") + (f"#{r['anchor']}" if r["anchor"] else "")
    return {"title": title, "section": r["section_path"], "url": url, "text": r["text"], "chunk_id": r["chunk_id"],
            "page_kind": None}


# Words that describe a material rather than name one ("common metal", "strongest wood"): never a missing material.
_DESCRIBES = {"the", "a", "an", "best", "good", "of", "high", "low", "medium", "carbon", "any", "what", "which", "this",
              "that", "my", "hcs", "lcs", "mcs", "better", "for", "in", "hard", "soft", "common", "rare", "special",
              "uncommon", "heavy", "light", "cheap", "expensive", "strong", "tough", "sturdy", "durable", "solid", "fine",
              "basic", "plain", "standard", "normal", "regular", "ordinary", "precious", "exotic", "some", "every", "each",
              "pure", "raw", "other", "another", "same", "new", "old", "great", "ideal", "top", "that's", "whats"}


def _unknown_named(question: str, known: set[str]) -> str | None:
    """'damascus steel', 'dragon metal': a name the table does not list. Returns it, or None."""
    for m in re.finditer(rf"\b({LATIN}) (steel|metal|wood|bone|stone)\b", question, re.I):
        w = m.group(1).lower()
        if w in known or w in _DESCRIBES or w.endswith("est"):  # "strongest", "hardest", "lightest"
            continue
        return f"{m.group(1)} {m.group(2).lower()}"
    return None


def passage(conn: sqlite3.Connection, question: str, base_url: str) -> list[dict] | None:
    """[ranking passage with a code-written `answer`, rule passages it cites], or None when this is not such a
    question or the tables are not indexed. 'Which metals are blue?' is answered here too (`colour_passage`)."""
    ask = wants(question)
    if ask is None:
        return colour_passage(conn, question, base_url)
    if ask.matter:
        matter, craft = OTHER_MATTER[ask.matter]
    else:
        matter, craft = ("wood", "Engineering") if ask.kind == "bow" else ("metal", "Forging")
    rows = _load(conn, matter, craft)
    if not rows:
        return None
    known = {w for (n, _r) in rows for w in re.split(r"[^a-z]+", _name(n).lower()) if w}
    if ask.matter:
        return _other(conn, ask, matter, rows, question, known, base_url)
    if ask.kind == "weapon":
        need = ("hardness", "density")
        ranked = _rank(rows, lambda s: (s["hardness"], min(s["density"], WEAPON_CAP), s.get("workability", 0)), need)
        intro = (f"For a {ask.item}, hardness is what drives the damage, so here is how the metals stack up in my "
                 "notes: hardest first, with density as the tiebreak (it only helps a weapon up to 7.5).")

        def line(n, s, i=1):
            return (f"- **{_name(n)}**: hardness {_fmt(s['hardness'])}, density {_fmt(s['density'])}, "
                    f"workability {_fmt(s.get('workability'))}, durability {_fmt(s.get('durability'))} [1]")
        rules = [("{dens}", FORGING, "Metallurgy > Mixing Metals to Adjust Density/Weight (part 1)%"),
                 ("{work}", FORGING, "Metallurgy")]
    elif ask.kind == "armor":
        need = ("physical", "density")
        ranked = _rank(rows, lambda s: (s["physical"], s.get("thermal", 0) + s.get("electrical", 0), s["density"]), need)
        intro = (f"For {ask.item} armor, protection follows the metal's physical resistance (thermal and electrical "
                 "resistance cover fire, cold and lightning), and unlike a weapon its density keeps helping past 7.5. "
                 "Best physical resistance first:")

        def line(n, s, i=1):
            return (f"- **{_name(n)}**: physical {_fmt(s['physical'])}, thermal {_fmt(s.get('thermal'))}, "
                    f"electrical {_fmt(s.get('electrical'))}, density {_fmt(s['density'])}, "
                    f"workability {_fmt(s.get('workability'))} [1]")
        rules = [("{prop}", CATEGORY, "Forging materials > Metal properties%"),
                 ("{dens}", FORGING, "Metallurgy > Mixing Metals to Adjust Density/Weight (part 1)%")]
    else:
        attr = {"short": "affinity_short", "long": "affinity_long", "composite": "affinity_composite"}
        types = [ask.bow] if ask.bow else ["short", "long", "composite"]
        blocks = []
        for t in types:
            a = attr[t]
            ranked = _rank(rows, lambda s, a=a: (s[a], s.get("stiffness", 0), s.get("hardness", 0)), (a,))
            common, rare = _split(ranked, 3, 3)
            def wood(n, s, a=a):
                return (f"- **{_name(n)}**: affinity {_fmt(s[a])}, stiffness {_fmt(s.get('stiffness'))}, "
                        f"hardness {_fmt(s.get('hardness'))}, durability {_fmt(s.get('durability'))}, "
                        f"workability {_fmt(s.get('workability'))} [1]")
            blocks.append(f"For a {BOW_LABEL[t]}, by the wood's affinity for it:\n\nCommon woods:\n"
                          + "\n".join(wood(n, s) for n, _r, s in common)
                          + "\n\nRare and special woods:\n" + "\n".join(wood(n, s) for n, _r, s in rare))
        answer = ("Woods carry an affinity for each kind of bow, and that is what matters most when picking one. "
                  "From the wood table in my notes:\n\n" + "\n\n".join(blocks))
        answer += "\n\n(OOC) Workability sets how hard the wood is to craft well: a lower number needs more skill."
        return [{"answer": _finish(answer, question, known), "title": CATEGORY, "section": "Engineering materials > wood",
                 "url": base_url.rstrip("/") + "/Category:Crafting_materials#Engineering_materials",
                 "text": answer, "chunk_id": None}]
    common, rare = _split(ranked)
    body = [intro, "", "Common metals (easy to come by):", *[line(n, s) for n, _r, s in common]]
    if rare:
        body += ["", "Rare and special metals (strongest, but their low workability asks for more skill):",
                 *[line(n, s) for n, _r, s in rare]]
    out = [{"title": CATEGORY, "section": "Forging materials > metals",
            "url": base_url.rstrip("/") + "/Category:Crafting_materials#Forging_materials", "chunk_id": None}]
    notes = []
    for tag, title, like in rules:
        p = _chunk(conn, title, like, base_url)
        if p:
            out.append(p)
            n = len(out)
            if tag == "{dens}":
                notes.append(
                    f"Weapon templates cap at {WEAPON_CAP:g} density: more only adds weight, not damage, while armor keeps "
                    f"improving beyond it [{n}]. Nothing can be made from metal under {MIN_DENSITY:g} density, so a light "
                    f"metal has to be mixed with a heavier one; a mix keeps a metal's properties while it is at least 67% "
                    f"of the ingot [{n}].")
            elif tag == "{work}":
                notes.append(f"Higher workability makes a piece easier to craft masterfully; rare metals mostly have low "
                             f"workability, so they need more skill [{n}].")
            elif tag == "{prop}":
                notes.append(f"Physical resistance sets the protection of metal armor, thermal and electrical resistance the "
                             f"fire/cold and lightning absorption, and density the absorption and the weight [{n}].")
    answer = "\n".join(body) + "\n\n(OOC) " + " ".join(notes)
    out[0]["answer"] = _finish(answer, question, known)
    out[0]["text"] = out[0]["answer"].replace(" [1]", "")
    return out


def _finish(answer: str, question: str, known: set[str]) -> str:
    odd = _unknown_named(question, known)
    if odd:
        answer = (f"I don't have \"{odd}\" as a craftable material in my notes: it isn't in the crafting materials "
                  "tables (a name like that may only be a decoration in an item's description). Here is what is listed.\n\n"
                  + answer)
    return answer


def _other(conn: sqlite3.Connection, ask: Ask, matter: str, rows: dict, question: str, known: set[str], base_url: str):
    """Leather, cloth, bone or stone: ranked on the one stat that decides the item class, with every stat the table
    gives shown. The wiki states the stat meanings for metals; for these the table headers are the only rule, so the
    answer says so rather than inventing a cap or a formula."""
    if ask.kind == "weapon":
        key, label = (lambda s: (s.get("hardness", 0), s.get("durability", 0))), "hardness (what drives weapon damage)"
        need = ("hardness",)
    elif matter in ("leather", "cloth") or ask.kind == "armor":
        key = lambda s: (s.get("physical", 0), s.get("thermal", 0) + s.get("electrical", 0), s.get("durability", 0))  # noqa: E731
        label, need = "physical resistance (protection)", ("physical",)
    else:
        key, label, need = (lambda s: (s.get("hardness", 0), s.get("durability", 0))), "hardness", ("hardness",)
    ranked = _rank(rows, key, need)
    if not ranked:
        return None
    common, rare = _split(ranked, 5, 5)
    order = ("hardness", "durability", "physical", "thermal", "electrical", "workability", "density", "value")

    def line(n, s):
        shown = ", ".join(f"{a} {_fmt(s[a])}" for a in order if a in s)
        return f"- **{_name(n)}**: {shown} [1]"
    what = f"for {ask.item}" if ask.kind != "other" else f"{ask.matter}"
    body = [f"Going by {label}, here are the {matter} materials in my notes, best first ({what}):", "",
            "Common:", *[line(n, s) for n, _r, s in common]]
    if rare:
        body += ["", "Rare and special:", *[line(n, s) for n, _r, s in rare]]
    note = ("(OOC) The crafting materials table lists these stats; how much each one counts for a particular item is in "
            "that discipline's own guide, so treat this as a ranking by the stat that matters most, not a full formula.")
    answer = "\n".join(body) + "\n\n" + note
    return [{"answer": _finish(answer, question, known), "title": CATEGORY, "section": f"{matter} materials",
             "url": base_url.rstrip("/") + "/Category:Crafting_materials", "text": answer, "chunk_id": None}]


# "Which metals are blue?": from the "at a Glance" box on each material's own page (facts domain 'material_page').
# A search can only show the model a handful of pages, so it lists blue *things* (a blue-flecked jar); this lists every
# material whose box names the colour, each cited to its own page.

# Colour words as the pages and people write them, folded to the colour asked for. Indigo and teal count for both.
SHADES = {"blue": ("blue", "bluish", "azure", "indigo", "cobalt", "navy", "sapphire", "cerulean", "teal"),
          "purple": ("purple", "purplish", "violet", "lavender", "indigo", "plum", "lilac", "mauve"),
          "red": ("red", "reddish", "crimson", "scarlet", "ruby", "maroon"),
          "green": ("green", "greenish", "emerald", "jade", "teal"), "yellow": ("yellow", "yellowish"),
          "orange": ("orange",), "pink": ("pink", "pinkish", "rose", "rosy"), "brown": ("brown", "brownish", "tan", "umber"),
          "black": ("black", "blackish", "ebon", "ebony"), "white": ("white", "whitish", "ivory"),
          "grey": ("grey", "gray", "greyish", "grayish"), "clear": ("clear", "translucent", "transparent"),
          "silver": ("silver", "silvery", "platinum"), "gold": ("gold", "golden"), "copper": ("copper", "coppery", "bronze")}
_FAMILY: dict[str, set[str]] = {}
for _fam, _words in SHADES.items():
    for _w in _words:
        _FAMILY.setdefault(_w, set()).add(_fam)
# A metal's own name is a colour only when the question says so: "silver metals" may mean silver itself.
_METAL_NAMES = {"silver", "gold", "copper", "bronze", "platinum"}
_SAYS_COLOUR = re.compile(r"\b(colou?r(?:ed|s)?|hue|hued|tint|shade|looks?)\b", re.I)
_LISTING = re.compile(r"\b(which|what|list|all|any|every|give me|show me|name|are there|is there|options?)\b", re.I)
_MATTER_WORDS = {"metal": "metal", "metals": "metal", "alloy": "metal", "alloys": "metal", "wood": "wood",
                 "woods": "wood", "leather": "leather", "leathers": "leather", "hide": "leather", "hides": "leather",
                 "pelt": "leather", "pelts": "leather", "cloth": "cloth", "cloths": "cloth", "fabric": "cloth",
                 "fabrics": "cloth", "silk": "cloth", "silks": "cloth", "stone": "stone", "stones": "stone",
                 "bone": "bone", "bones": "bone", "material": "", "materials": ""}
# A blue sword, where to buy blue metal, what a thing is made of: about one item, not a list of materials.
_NOT_A_LIST = re.compile(rf"\b(buy|sell|sells|shop|store|merchant|price|cost|where|made (?:of|from|out of)|"
                         rf"{WEAPONS}|{ARMOR}|{BOWS})\b", re.I)
PLURAL = {"metal": "metals", "wood": "woods", "leather": "leathers", "cloth": "cloths", "stone": "stones",
          "bone": "bones"}
UNSURE = ("various", "unknown")


@dataclass
class ColourAsk:
    colours: list[str]  # families, in the order asked
    matters: set[str]  # metal, wood...; empty = every kind of material
    rarity: str | None = None  # Common | Rare | Quest-Only when the question narrows it


def wants_colour(question: str) -> ColourAsk | None:
    """'Give me all metals that are blue or partially blue' -> ColourAsk(['blue'], {'metal'})."""
    words = re.findall(r"[a-z]+", question.lower())
    matters = {_MATTER_WORDS[w] for w in words if w in _MATTER_WORDS}
    if not matters or not _LISTING.search(question) or _NOT_A_LIST.search(question):
        return None
    says = bool(_SAYS_COLOUR.search(question))
    colours: list[str] = []
    for w in words:
        if w in _METAL_NAMES and not says:
            continue
        colours += [f for f in sorted(_FAMILY.get(w, ())) if f not in colours]
    if not colours:
        return None
    rarity = next((r for r, rx in (("Common", r"\bcommon\b"), ("Rare", r"\brare\b"), ("Quest-Only", r"\bquest"))
                   if re.search(rx, question, re.I)), None)
    return ColourAsk(colours, matters - {""}, rarity)


def _families(colours: str, wanted: set[str]) -> tuple[set[str], bool, bool]:
    """'Blue, Black, Various' -> ({'blue', 'black'}, only the wanted colours?, any 'various'/'unknown'?). A shade
    between two colours ('indigo') is still only blue when blue is asked for."""
    words = [w for w in re.findall(r"[a-z]+", colours.lower()) if w in _FAMILY]
    fams = set().union(*(_FAMILY[w] for w in words))
    only = bool(words) and all(_FAMILY[w] & wanted for w in words)
    return fams, only, any(w in UNSURE for w in re.findall(r"[a-z]+", colours.lower()))


def colour_passage(conn: sqlite3.Connection, question: str, base_url: str) -> list[dict] | None:
    """[one passage per matching material page, the first carrying a code-written `answer`], or None when this is
    not such a question or the boxes have not been read yet."""
    ask = wants_colour(question)
    if ask is None:
        return None
    boxes: dict[str, dict[str, str]] = {}
    page_of: dict[str, int] = {}  # each box was read from the material's own page
    for r in conn.execute("SELECT page_id, entity, attribute, value_text FROM facts WHERE domain='material_page'"):
        boxes.setdefault(r[1], {})[r[2]] = r[3]
        page_of[r[1]] = r[0]
    if not boxes:
        return None
    matter_of = {r[0]: r[1].split("|")[0] for r in conn.execute(
        "SELECT entity, min(grp) FROM facts WHERE domain='material' GROUP BY entity")}
    wanted = set(ask.colours)
    only: list[str] = []
    mixed: list[str] = []
    unsure: list[str] = []
    seen: set[str] = set()  # the colours this kind of material does come in, for a "none" answer
    for name in sorted(boxes, key=lambda n: _name(n).lower()):
        box = boxes[name]
        if ask.matters and matter_of.get(name) not in ask.matters:
            continue
        if ask.rarity and not box.get("rarity", "").startswith(ask.rarity):
            continue
        fams, solely, vague = _families(box.get("colors", ""), wanted)
        seen |= fams
        if fams & wanted:
            (only if solely and not vague else mixed).append(name)
        elif not fams:
            unsure.append(name)

    what = " and ".join(PLURAL[m] for m in sorted(ask.matters)) or "materials"
    if ask.rarity:
        what = f"{ask.rarity.lower()} {what}"
    colour = " or ".join(ask.colours)
    out: list[dict] = []

    def line(name: str) -> str:
        box = boxes[name]
        r = conn.execute("SELECT chunk_id FROM chunks WHERE page_id=? ORDER BY ord LIMIT 1", (page_of[name],)).fetchone()
        out.append({"title": name, "section": "at a Glance",
                    "url": base_url.rstrip("/") + "/" + name.replace(" ", "_"),
                    "text": f"{_name(name)}: colors {box.get('colors', '?')}; rarity {box.get('rarity', '?')}",
                    "chunk_id": r[0] if r else None})
        kind = f"{box['rarity'].lower()}, " if box.get("rarity") else ""
        if len(ask.matters) != 1 and matter_of.get(name):
            kind += f"{matter_of[name]}, "
        return f"- **{_name(name)}** ({kind.rstrip(', ')}): {box.get('colors', '?').lower()} [{len(out)}]"

    if not only and not mixed:
        answer = f"None of the {what} in my notes is listed as {colour}."
        if seen:
            answer += f" The colours they do come in: {', '.join(sorted(seen))}."
        out.append({"title": CATEGORY, "section": "", "url": base_url.rstrip("/") + "/Category:Crafting_materials",
                    "text": answer, "chunk_id": None})
    else:
        body = [f"Going by the \"at a Glance\" box on each material's own page, these {what} are listed as {colour}:"]
        if only:
            body += ["", f"Only {colour}:" if mixed else f"{colour.capitalize()}:", *[line(n) for n in only]]
        if mixed:
            body += ["", f"{colour.capitalize()} with other colours:" if only else f"{colour.capitalize()} among other colours:",
                     *[line(n) for n in mixed]]
        answer = "\n".join(body)
    if unsure:
        names = ", ".join(_name(n) for n in unsure) if len(unsure) <= 12 else f"{len(unsure)} of them"
        answer += (f"\n\n(OOC) Some {what} are listed only as \"various\" or \"unknown\" colours, so they could be "
                   f"{colour} too: {names}.")
    out[0]["answer"] = answer
    return out
