"""Question kinds (what the user is asking about) and page kinds (what a wiki page is), with the retrieval
weights that connect them. No imports, so config, db, search and prompts can all use it."""

from __future__ import annotations

import re
from dataclasses import dataclass

QUESTION_KINDS = (
    "item", "crafting", "place", "character", "lore", "creature", "mechanics", "event_news", "meta", "general",
)

# What each question kind means, as the planner is told (llm/prompts.py builds its list from this, in this order).
KIND_DESCRIPTIONS = {
    "item": 'a specific weapon, armor, shield, item or title, its stats, or where to get it. Always "item" when mode is "items".',
    "crafting": 'making things: forging, outfitting, engineering, alchemy, enchanting, techniques, recipes, materials, crafting societies, tools, workorders. "How do I forge/craft/make X" is "crafting" even though a skill is involved.',
    "place": "towns, provinces, regions, shops, guildhalls, maps, docks, how to travel somewhere, what is sold where, local currency.",
    "character": 'a named mortal person: an NPC, shopowner, guild leader, GameMaster, or a player\'s character; who someone is. Gods and legendary figures (Hodierna, Lanival, the Immortals) are "lore", not "character".',
    "lore": "history, the Immortals (the gods) and legendary figures, races as peoples and cultures, organizations, in-world books, languages, holidays, legends, the timeline.",
    "creature": "monsters and animals: where they are, levels, attacks, loot, skinning, hunting ladders.",
    "mechanics": "game systems and rules: skills, commands, spells and abilities, experience, death, combat, housing, how X works.",
    "event_news": 'festivals, quests, auctions, raffles, dated events, announcements, updates, forum or Discord posts, "what changed".',
    "meta": "greetings and chit-chat, or questions about you, this assistant, or Elanthipedia the wiki itself (not the game).",
    "general": "anything else or mixed.",
}

PAGE_KINDS = (
    "item", "recipe", "material", "crafting", "guide", "script", "shop", "place", "pc", "npc", "gm", "creature",
    "spell", "skill", "command", "guild", "mechanics", "lore", "race", "event", "news", "post", "category",
    "disambig", "meta", "log", "soft_redirect", "talk", "user", "file", "site", "general",
)

# ----------------------------------------------------------------------------------------------- namespaces
# Every namespace is indexed, so all the wiki's metadata is searchable, but not all of it is content. Discussion pages
# (every odd namespace: Talk, User talk, Item talk, ...) and the wiki's own machinery (file descriptions, interface
# messages, templates, forms, Semantic MediaWiki properties and schemas, modules) rank below articles, are never offered
# as a thread to pull on, and never compete for names ("Template:Armor" and "Talk:Binu" share their article's name).
TALK_WEIGHT = 0.75
SITE_NAMESPACES: dict[int, tuple[str, float]] = {  # ns -> (page kind, rank weight)
    6: ("file", 0.5),      # File: an image's description page
    8: ("site", 0.3),      # MediaWiki: interface messages, site CSS and JS
    10: ("site", 0.4),     # Template
    106: ("site", 0.3),    # Form
    202: ("site", 0.6),    # Property: what each Semantic MediaWiki property means
    212: ("site", 0.3),    # smw/schema
    828: ("site", 0.3),    # Module (Lua)
}


def is_talk_ns(ns: int) -> bool:
    return ns > 0 and ns % 2 == 1


def namespace_weight(ns: int) -> float:
    """How a namespace ranks against articles, before any per-namespace setting in `[retrieval.ns_weights]`."""
    if is_talk_ns(ns):
        return TALK_WEIGHT
    return SITE_NAMESPACES[ns][1] if ns in SITE_NAMESPACES else 1.0


def names_a_thing(ns: int) -> bool:
    """Whether a page's title may stand for a name in a question (title matches, the name resolver)."""
    return not is_talk_ns(ns) and ns not in SITE_NAMESPACES


# SQL for the same rule, on a column holding the namespace
NAMES_A_THING_SQL = "({col} % 2 = 0 AND {col} NOT IN (" + ",".join(str(n) for n in sorted(SITE_NAMESPACES)) + "))"

# Page kinds with a weight column (the rest, e.g. log/soft_redirect/disambig/category, are handled by flags).
_COLS = ("item", "recipe", "material", "crafting", "guide", "script", "shop", "place", "pc", "npc", "gm",
         "creature", "spell", "skill", "command", "guild", "mechanics", "lore", "race", "event", "news", "post")

_ROWS: dict[str, tuple[float, ...]] = {
    #          item recipe mat  craft guide script shop place pc  npc  gm   creat spell skill cmd  guild mech lore race event news post
    "item":      (1.3, 1.1, 1.1, 0.9, 0.9, 0.8, 1.1, 1.0, 0.6, 0.8, 0.8, 0.9, 0.9, 0.9, 0.9, 0.9, 0.9, 0.8, 0.8, 0.9, 0.9, 0.9),
    "crafting":  (0.9, 1.2, 1.2, 1.4, 1.3, 1.0, 1.1, 0.9, 0.6, 0.9, 0.8, 0.7, 0.8, 1.2, 1.1, 0.9, 1.0, 0.7, 0.7, 0.8, 0.9, 1.0),
    "place":     (0.8, 0.7, 0.7, 0.9, 0.9, 0.8, 1.2, 1.4, 0.6, 1.1, 0.8, 1.0, 0.8, 0.8, 0.8, 1.0, 0.8, 0.9, 0.9, 0.9, 0.9, 0.8),
    "character": (0.7, 0.6, 0.6, 0.7, 0.8, 0.8, 1.0, 0.9, 1.3, 1.4, 1.3, 0.7, 0.7, 0.7, 0.7, 0.9, 0.7, 1.0, 0.9, 1.0, 0.9, 0.9),
    "lore":      (0.7, 0.6, 0.7, 0.7, 0.8, 0.7, 0.8, 1.0, 0.7, 1.0, 0.9, 0.9, 0.9, 0.8, 0.7, 1.0, 0.8, 1.4, 1.3, 1.1, 0.9, 1.0),
    "creature":  (0.9, 0.7, 1.0, 0.8, 1.1, 0.9, 0.8, 1.1, 0.6, 0.8, 0.8, 1.4, 0.9, 1.0, 0.9, 0.9, 0.9, 0.9, 0.8, 0.8, 0.8, 0.9),
    "mechanics": (0.9, 0.8, 0.8, 0.9, 1.2, 1.0, 0.8, 0.8, 0.6, 0.8, 0.9, 0.9, 1.2, 1.3, 1.3, 1.2, 1.3, 0.8, 1.0, 0.8, 1.0, 1.1),
    "event_news": (0.8, 0.7, 0.7, 0.8, 0.8, 0.8, 0.9, 0.9, 0.8, 0.9, 1.1, 0.8, 0.8, 0.8, 0.8, 0.9, 0.9, 1.0, 0.9, 1.4, 1.4, 1.3),
}

DEFAULT_KIND_WEIGHTS: dict[str, dict[str, float]] = {
    q: dict(zip(_COLS, row, strict=True)) for q, row in _ROWS.items()
}
DEFAULT_KIND_WEIGHTS["meta"] = {**{c: 0.9 for c in _COLS}, "meta": 1.4}
DEFAULT_KIND_WEIGHTS["general"] = {"script": 0.9, "pc": 0.8}

# Talk and user pages are context for an answer (who worked something out), never a thread to offer on their own.
OFFER_EXCLUDED_PAGE_KINDS = frozenset({"log", "soft_redirect", "disambig", "category", "meta", "script", "talk", "user",
                                       "file", "site"})
# A page linked from more articles than this is a hub everything mentions (Zoluren 924, Shard 525), not a next thread;
# one the answer's own page names in its opening may be better known ("the Rakash", 236, for a Rakash weapon).
OFFER_MAX_INLINKS = 200
OFFER_MAX_INLINKS_NAMED = 400
# Pages that list things rather than being about one: an event's auction and loot lists, yearly update digests.
OFFER_LISTING_TITLE = re.compile(
    r"/[^/]*\b(?:auctions?|loot|prizes|raffles?|merchants?|items|gifts?|darkbox)$|"
    r"^(?:DragonRealms Updates|News Archives|elanthipedia:News Archives)\b|\b(?:gift|prize) list\b", re.I)
# What an answer's page can lead to. An event's booths and their wares are a list, not a thread (Guildfest -> "Large
# bin of miniature figurines"); a player's own character is a thread only when the question was about characters.
OFFER_NOT_AFTER: dict[str, frozenset[str]] = {
    "event": frozenset({"shop", "item", "recipe"}),
    "news": frozenset({"shop", "item", "recipe"}),
}
OFFER_ONLY_FOR: dict[str, frozenset[str]] = {"pc": frozenset({"character"})}
# An answer about a category page is a list of its members (the code-written tables): nothing to follow from it.
OFFER_NO_ANCHOR_KINDS = frozenset({"category", "disambig", "meta", "site"})


# ----------------------------------------------------------------------------------------------- layers
# "Tell me about X" reads several sorts of page, not eight passages of X's stat sheet (core/layers.py). A hierarchy over
# what pages are about: namespaces are only page formats (storyline, systems and people all sit in Main).


@dataclass(frozen=True)
class Layer:
    key: str
    heading: str  # exactly as the answer shows it; "" for the opening part, which has no heading
    page_kinds: frozenset[str]  # what sort of page fills it
    slots: int  # how many of the answer's passages it is given
    mentions: bool = False  # filled by pages that name the subject in their text, not only by links
    logs: bool = False  # players' role-play logs may fill what no other page does (and are labelled in code)
    meanings: bool = False  # what the subject's name means belongs here
    practical: bool = False  # shops, loot and training: not what "the story behind X" asks for


def _layer(key: str, heading: str, kinds: str, slots: int, **flags: bool) -> Layer:
    return Layer(key, heading, frozenset(kinds.split()), slots, **flags)


_ITS_OWN = _layer("own", "", "", 3)
# "general" pages only mention things in passing (trade routes, prestige boards, players' hunting ladders): never story.
_STORY_KINDS = "event news lore"
LAYERS: dict[str, tuple[Layer, ...]] = {
    "thing": (_layer("own", "", "", 2),
              _layer("source", "Where it comes from", "shop recipe crafting guide", 2, practical=True),
              _layer("people", "The name and its people", "race lore mechanics", 2, mentions=True, meanings=True),
              _layer("story", "In the story", _STORY_KINDS, 2, mentions=True, logs=True)),
    "person": (_ITS_OWN,
               _layer("deeds", "What they've done", _STORY_KINDS, 3, mentions=True, logs=True),
               _layer("ties", "Who and what they're tied to", "guild place shop race npc gm", 2)),
    "lore": (_ITS_OWN,
             _layer("around", "Related people and places", "place guild race lore", 2),
             _layer("story", "In the story", "event news", 3, mentions=True, logs=True)),
    "place": (_ITS_OWN,
              _layer("there", "What's there", "shop creature npc guild place", 3, practical=True),
              _layer("history", "Its history", _STORY_KINDS, 2, mentions=True, logs=True)),
    "system": (_ITS_OWN,
               _layer("train", "How to train it", "guide skill crafting", 2, practical=True),
               _layer("why", "Why it is so", "post news lore", 3, mentions=True)),
    "creature": (_ITS_OWN,
                 _layer("loot", "Loot and skinning", "material recipe", 1, practical=True),
                 _layer("story", "In the story", _STORY_KINDS, 3, mentions=True)),
}
# What a page is -> which table a question about it reads. Anything else (an event, a guide, a post) is answered as usual.
LAYER_TABLE = {
    "item": "thing", "recipe": "thing", "material": "thing",
    "pc": "person", "npc": "person", "gm": "person",
    "lore": "lore", "race": "lore",
    "place": "place", "shop": "place",
    "spell": "system", "skill": "system", "command": "system", "guild": "system", "mechanics": "system", "crafting": "system",
    "creature": "creature",
}
# How the closing offer names the rest, after a narrow question (the page's chip then asks "Tell me about <this>").
LAYER_OFFER = {"thing": "the story behind {x}", "person": "the story of {x}", "lore": "the story of {x}",
               "place": "the history of {x}", "system": "the reasoning behind {x}", "creature": "the story behind {x}"}


# ----------------------------------------------------------------------------------------------- kind rules
# One declarative table of what each kind of page is and how a question points at it. The name resolver and the
# clarifying question read this instead of carrying their own word lists (the planner reads KIND_DESCRIPTIONS above).

# page kind -> the word a person uses for it ("the spell Sidasas Sedra", "the weapon Svidaw sis")
KIND_LABEL = {
    "spell": "spell", "item": "item", "recipe": "crafted item", "material": "crafting material", "crafting": "crafting page",
    "guide": "guide", "creature": "creature", "pc": "character", "npc": "character", "gm": "staff member", "place": "place",
    "shop": "shop", "skill": "skill", "command": "command", "guild": "guild", "race": "race", "mechanics": "game mechanic",
    "lore": "lore page", "event": "event", "news": "news post", "general": "page", "user": "player page",
    "talk": "talk page", "file": "file page", "site": "wiki page",
}

# page kinds that are the same sort of thing to a reader: two names in one group are not a real conflict
KIND_GROUP = {
    "spell": "spell", "creature": "creature", "pc": "character", "npc": "character", "gm": "character",
    "place": "place", "shop": "place", "skill": "skill", "command": "skill", "guild": "skill", "race": "race",
    "item": "thing", "recipe": "thing", "material": "thing", "crafting": "thing", "guide": "thing",
    "mechanics": "thing", "lore": "thing", "general": "thing", "event": "event", "news": "event",
    "user": "character",  # User:Dandin and a character page named Dandin are the same sort of thing: a person
}

# words in a question that say which group it is about
INTENT_WORDS = {
    "spell": r"spells?|cast(?:s|ing)?|prep(?:are|aring)?|incantations?|metaspells?|mana|ritual|invoke",
    "creature": r"creatures?|monsters?|kill(?:s|ing)?|hunt(?:s|ing)?|bestiary|spawns?|critters?",
    "character": r"who|characters?|players?|npcs?|person|lives|member of",
    "place": r"where|located|directions?|town|city|shops?|vendors?|map",
    "skill": r"skills?|train(?:s|ing)?|ranks?|commands?|verbs?|guild",
    "thing": r"weapons?|armou?r|items?|wield(?:s|ing)?|wear(?:s|ing)?|stats?|forge[sd]?|crafted?|recipes?|worth|price|"
             r"sells?|buy|materials?",
    "event": r"events?|festival|news|when is|prizes?",
}
INTENT_RES = {g: re.compile(r'\b(?:' + w + r')\b', re.I) for g, w in INTENT_WORDS.items()}


def intent_groups(question: str) -> set[str]:
    """Kind groups a question points at through its own words; empty when it names no kind."""
    return {g for g, rx in INTENT_RES.items() if rx.search(question)}
