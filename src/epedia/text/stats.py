"""Render a page's SMW properties as a compact, embeddable 'stat sheet' text block."""

from __future__ import annotations

import re

# Preferred display order; anything else follows alphabetically.
ORDER = [
    "Type", "Combat type", "Range", "Noun", "Weight", "Appraised cost",
    "Puncture damage", "Slice damage", "Impact damage", "Fire damage", "Cold damage", "Electric damage",
    "FOI", "Balance", "Suitedness", "Construction",
]
HIDE = {
    "Needs checking", "ItemReview", "Item review", "Page source", "Source",
}
LABELS = {
    "FOI": "Force of impact",
    "Weight of": "Weight",
    "Is combat type": "Combat type",
    "Is metal": "Metal",
    "Is outdated": "Outdated",
    "Item type": "Item type",
    "Page type": "Page type",
}

_TAGS = re.compile(r"<[^>]+>")


def clean_value(v: str) -> str:
    return re.sub(r"\s+", " ", _TAGS.sub("", v)).strip()


def label_for(prop: str) -> str:
    base = re.sub(r"_(is|of)(_number)?$", "", prop)
    base = base.replace("_", " ").strip()
    base = LABELS.get(base, base)
    return base[:1].upper() + base[1:] if base else prop


def render(title: str, props: list[dict]) -> str:
    """props: [{prop, value_text, value_num, value_type}] -> multi-line stat sheet."""
    by_prop: dict[str, list[dict]] = {}
    for p in props:
        by_prop.setdefault(p["prop"], []).append(p)

    lines: dict[str, str] = {}
    for prop, vals in by_prop.items():
        if prop.endswith("_number") and prop[: -len("_number")] in by_prop:
            continue  # merged into its text sibling below
        label = label_for(prop)
        if label in HIDE or prop in HIDE:
            continue
        texts = [clean_value(v["value_text"]) for v in vals]
        if vals[0]["value_type"] == "_boo":
            texts = ["yes" if t.lower() in ("t", "true", "1") else "no" for t in texts]
            if label == "Outdated" and texts[0] == "no":
                continue
        number = by_prop.get(prop + "_number")
        if number:
            texts = [f"{t} ({number[0]['value_num']:g})" if number[0]["value_num"] is not None else t for t in texts[:1]]
        value = "; ".join(t for t in texts if t)
        if value:
            lines[label] = value
    for prop, vals in by_prop.items():
        if prop == "Page_source_is":
            names = "; ".join(clean_value(v["value_text"]) for v in vals)
            if names:
                lines["Sources"] = names

    ordered = [k for k in ORDER if k in lines] + sorted(k for k in lines if k not in ORDER)
    page_type = next((clean_value(v["value_text"]) for v in by_prop.get("Page_type_is", [])), "item")
    head = f"{title.split(':', 1)[-1]} — {page_type} stats"
    return head + "\n" + "\n".join(f"{k}: {lines[k]}" for k in ordered if k != "Page type")
