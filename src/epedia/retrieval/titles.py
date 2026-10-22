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
