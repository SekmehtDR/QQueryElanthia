"""Question kinds (what the user is asking about) and page kinds (what a wiki page is), with the retrieval
weights that connect them. No imports, so config, db, search and prompts can all use it."""

from __future__ import annotations

import re

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
    "disambig", "meta", "log", "soft_redirect", "general",
)

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

# A related page may be offered after an answer only if its kind is at least this favoured for the question kind.
OFFER_MIN_WEIGHT = 1.0
OFFER_EXCLUDED_PAGE_KINDS = frozenset({"log", "soft_redirect", "disambig", "category", "meta", "script"})


# ----------------------------------------------------------------------------------------------- kind rules
# One declarative table of what each kind of page is and how a question points at it. The name resolver and the
# clarifying question read this instead of carrying their own word lists (the planner reads KIND_DESCRIPTIONS above).

# page kind -> the word a person uses for it ("the spell Sidasas Sedra", "the weapon Svidaw sis")
KIND_LABEL = {
    "spell": "spell", "item": "item", "recipe": "crafted item", "material": "crafting material", "crafting": "crafting page",
    "guide": "guide", "creature": "creature", "pc": "character", "npc": "character", "gm": "staff member", "place": "place",
    "shop": "shop", "skill": "skill", "command": "command", "guild": "guild", "race": "race", "mechanics": "game mechanic",
    "lore": "lore page", "event": "event", "news": "news post", "general": "page",
}

# page kinds that are the same sort of thing to a reader: two names in one group are not a real conflict
KIND_GROUP = {
    "spell": "spell", "creature": "creature", "pc": "character", "npc": "character", "gm": "character",
    "place": "place", "shop": "place", "skill": "skill", "command": "skill", "guild": "skill", "race": "race",
    "item": "thing", "recipe": "thing", "material": "thing", "crafting": "thing", "guide": "thing",
    "mechanics": "thing", "lore": "thing", "general": "thing", "event": "event", "news": "event",
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
