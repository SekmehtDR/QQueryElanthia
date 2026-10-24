"""Telling a question apart from text the person pasted along with it.

People paste a Discord post or an in-game message and ask about it ("What can you tell me about Tirost's message?
<the message>"). The question is what gets searched; the pasted text is material to understand, not a wiki source
and not something to search for verbatim.
"""

from __future__ import annotations

MIN_MATERIAL_CHARS = 120  # shorter than this is just a longer question, not pasted material
MAX_QUESTION_CHARS = 240
MAX_MATERIAL_CHARS = 3000
PURE_PASTE_CHARS = 600  # a long message with no question at all is a paste
PURE_PASTE_QUESTION = "Can you help me understand this?"


def split_message(text: str) -> tuple[str, str]:
    """(question, pasted material). The material is '' for an ordinary message."""
    text = text.strip()
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if len(lines) >= 2:
        first, rest = lines[0], "\n".join(lines[1:])
        last, body = lines[-1], "\n".join(lines[:-1])
        question_last = last.endswith("?") and not first.endswith("?")  # "<post>\nWhat does this mean?"
        if question_last and len(last) <= MAX_QUESTION_CHARS and len(body) >= MIN_MATERIAL_CHARS:
            return last, body[:MAX_MATERIAL_CHARS]
        if len(first) <= MAX_QUESTION_CHARS and len(rest) >= MIN_MATERIAL_CHARS:
            return first, rest[:MAX_MATERIAL_CHARS]
    if len(text) >= PURE_PASTE_CHARS and not text.rstrip().endswith("?"):
        return PURE_PASTE_QUESTION, text[:MAX_MATERIAL_CHARS]
    return text, ""
