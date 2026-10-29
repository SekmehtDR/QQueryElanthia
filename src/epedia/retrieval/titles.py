"""Title normalization and matching helpers."""

from __future__ import annotations

import re

# Namespace prefixes we strip for matching: a user asking about "broadsword" should hit "Weapon:Broadsword (1)".
NS_PREFIXES = {
    "weapon", "armor", "shield", "item", "title", "post", "category", "file", "talk",
    "user", "template", "help", "elanthipedia", "form", "property", "concept", "module",
}

_PAREN_SUFFIX = re.compile(r"\s*\((\d+|disambiguation)\)\s*$", re.IGNORECASE)
_QUESTION_LEAD = re.compile(
    r"^(what|who|where|which|how)\s+(is|are|was|were|does|do|did)\s+(a|an|the)?\s*"
    r"|^(tell me about|describe|explain|info on|information on|about)\s+(a|an|the)?\s*",
    re.IGNORECASE,
)
_TRAILING_PUNCT = re.compile(r"[\s?!.]+$")


def normalize(title: str) -> str:
    t = title.replace("_", " ").strip()
    if ":" in t:
        prefix, rest = t.split(":", 1)
        if prefix.strip().lower() in NS_PREFIXES:
            t = rest
    t = _PAREN_SUFFIX.sub("", t)
    t = t.lower()
    t = re.sub(r"\s+", " ", t).strip()
    return t


def query_as_title(query: str) -> str:
    """Strip question scaffolding ('what is the ...?') so the remainder can be matched against titles."""
    q = _TRAILING_PUNCT.sub("", query.strip())
    q = _QUESTION_LEAD.sub("", q)
    return normalize(q)


def tokens(text: str) -> list[str]:
    return [t for t in re.split(r"[^0-9a-z']+", text.lower()) if t]


# Words that start or fill a question and are not the name of anything, so a lookup of them alone is skipped.
STOP_NAMES = frozenset({
    "a", "an", "the", "is", "are", "was", "were", "of", "to", "in", "on", "for", "and", "or", "do", "does", "did",
    "how", "what", "who", "where", "when", "which", "why", "can", "could", "would", "should", "will", "i", "you", "me",
    "my", "it", "its", "with", "at", "by", "from", "that", "this", "be", "as", "about", "tell", "get", "much", "many",
    "best", "good", "better", "any", "some", "there", "have", "has", "use", "make", "need", "want", "know", "give",
    "spell", "spells", "weapon", "weapons", "item", "items", "cast", "creature", "monster", "skill", "guild", "place",
    "work", "works", "mean", "means", "thing", "things", "kind", "type", "like", "than", "then", "also", "just",
    "dragonrealms", "mud", "elanthia", "elanthipedia", "game", "wiki",
})

# Names of the game itself: a page called this is not what a question that merely mentions the game is about.
GAME_WORDS = frozenset({"dragonrealms", "mud", "elanthia", "elanthipedia"})


_GAME_TAIL = re.compile(r"\s+(?:in|of|for|on|within)\s+(?:the\s+)?(?:(?:MUD|game|text game)\s+)?(?:DragonRealms|Dragon Realms|"
                        r"Elanthia|Elanthipedia)\s*(\?*)$", re.IGNORECASE)


def strip_game_name(question: str) -> str:
    """'How does crafting work in MUD DragonRealms?' -> 'How does crafting work?'. Every page of the wiki is about the
    game, so naming it only drags the pages called DragonRealms and MUD into the results."""
    out = _GAME_TAIL.sub(lambda m: m.group(1), question.strip())
    return out or question
