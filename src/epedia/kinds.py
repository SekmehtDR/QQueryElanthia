"""Question kinds (what the user is asking about) and page kinds (what a wiki page is), with the retrieval
weights that connect them. No imports, so config, db, search and prompts can all use it."""

from __future__ import annotations

QUESTION_KINDS = (
    "item", "crafting", "place", "character", "lore", "creature", "mechanics", "event_news", "meta", "general",
)

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
