"""Split a page's section blocks into retrieval chunks sized for the embedding model and the LLM context."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from ..config import ChunkingCfg
from ..util import token_estimate
from .html2md import Block

_SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(\[])")


@dataclass
class Chunk:
    ord: int
    kind: str
    section_path: str
    anchor: str | None
    text: str
    token_est: int
    embed_text: str
    content_hash: str


def embed_text_for(title: str, section_path: str, text: str, categories: list[str] | None = None) -> str:
    head = f"search_document: {title} | {section_path or 'Overview'}\n{text}"
    if categories and not section_path:
        head += "\nCategories: " + ", ".join(categories[:8])
    return head


NOTICE_LABELS = {"incomplete": "incomplete", "stub": "a stub", "obsolete": "obsolete",
                 "dr3": "needing an update for DR 3.0"}


def notice_line(notices: list[str]) -> str:
    """One-line stand-in for the maintenance banners that were stripped from the page."""
    labels = [NOTICE_LABELS[n] for n in notices if n in NOTICE_LABELS]
    return f"(Marked {', '.join(labels)} on the wiki.)" if labels else ""


def chunk_page(
    title: str,
    blocks: list[Block],
    cfg: ChunkingCfg,
    categories: list[str] | None = None,
    notices: list[str] | None = None,
) -> list[Chunk]:
    line = notice_line(notices or [])
    if line:  # lead only, so only the first chunk's text (and hash) changes
        if blocks and blocks[0].level is None:
            blocks = [Block(None, None, None, [line, *blocks[0].parts]), *blocks[1:]]
        else:
            blocks = [Block(None, None, None, [line]), *blocks]
    # 1) section paths from the heading stack
    stack: list[tuple[int, str]] = []
    sections: list[tuple[str, str | None, str, int]] = []  # path, anchor, text, parent_level
    for b in blocks:
        if b.level is None:
            path = ""
        else:
            while stack and stack[-1][0] >= b.level:
                stack.pop()
            stack.append((b.level, b.heading or ""))
            path = " > ".join(h for _, h in stack)
        sections.append((path, b.anchor, b.text, b.level or 0))

    # 2) merge tiny sections into the following one (keeps heading context in the text)
    merged: list[tuple[str, str | None, str]] = []
    # A merged chunk keeps the path/anchor of its first section (where a citation link should land);
    # later sections folded into it are labelled inline with their own heading.
    started = False
    gpath, ganchor = "", None
    parts: list[str] = []
    for i, (path, anchor, text, _lvl) in enumerate(sections):
        label = path.split(" > ")[-1] if path else ""
        if not started:
            gpath, ganchor, parts, started = path, anchor, ([text] if text else []), True
        else:
            parts.append(f"{label}\n{text}".strip() if label else text)
        body = "\n\n".join(p for p in parts if p)
        if token_estimate(body) < cfg.min_section_tokens and i < len(sections) - 1:
            continue
        merged.append((gpath, ganchor, body))
        started = False

    # 3) split oversize sections
    chunks: list[Chunk] = []
    for path, anchor, body in merged:
        if not body.strip():
            continue
        pieces = _split(body, cfg)
        for n, piece in enumerate(pieces):
            p = path if len(pieces) == 1 else f"{path} (part {n + 1})" if path else f"Overview (part {n + 1})"
            et = embed_text_for(title, p, piece, categories if not chunks else None)
            chunks.append(
                Chunk(
                    ord=len(chunks),
                    kind="section",
                    section_path=p,
                    anchor=anchor,
                    text=piece,
                    token_est=token_estimate(piece),
                    embed_text=et,
                    content_hash=hashlib.sha1(et.encode("utf-8")).hexdigest(),
                )
            )
    return chunks


def _split(text: str, cfg: ChunkingCfg) -> list[str]:
    if token_estimate(text) <= cfg.max_tokens:
        return [text]
    units = _units(text, cfg)
    target_chars = cfg.target_tokens * 4
    overlap_chars = cfg.overlap_tokens * 4
    out: list[str] = []
    cur: list[str] = []
    cur_len = 0
    for u in units:
        if cur and cur_len + len(u) > target_chars:
            out.append("\n\n".join(cur))
            tail = _tail(cur, overlap_chars)
            cur, cur_len = ([tail] if tail else []), len(tail)
        cur.append(u)
        cur_len += len(u) + 2
    if cur:
        out.append("\n\n".join(cur))
    # A short trailing piece is either only the overlap tail (already in the previous piece: drop it) or a real
    # closing paragraph (fold it into the previous piece). Dropping it unconditionally loses that text for good.
    if len(out) > 1 and token_estimate(out[-1]) < cfg.overlap_tokens:
        last = out.pop()
        if last not in out[-1]:
            out[-1] = out[-1] + "\n\n" + last
    return out


def _tail(parts: list[str], chars: int) -> str:
    if chars <= 0:
        return ""
    last = parts[-1]
    if last.lstrip().startswith("|"):
        return ""  # never carry a table fragment as overlap
    return last[-chars:].lstrip() if len(last) > chars else ""


def _units(text: str, cfg: ChunkingCfg) -> list[str]:
    """Break text into atomic units: paragraphs, table row groups (header repeated), list items, sentences."""
    max_chars = cfg.max_tokens * 4
    units: list[str] = []
    for para in re.split(r"\n{2,}", text):
        para = para.strip()
        if not para:
            continue
        if len(para) <= max_chars:
            units.append(para)
        elif para.lstrip().startswith("|"):
            units.extend(_table_pieces(para, max_chars))
        elif re.match(r"^\s*(- |\d+\. )", para):
            units.extend(_group_lines(para.split("\n"), max_chars))
        else:
            sentences = _SENTENCE.split(para)
            units.extend(_group_lines(sentences, max_chars, sep=" "))
    return units


def _group_lines(lines: list[str], max_chars: int, sep: str = "\n") -> list[str]:
    out: list[str] = []
    cur = ""
    for ln in lines:
        while len(ln) > max_chars:  # pathological unbroken text
            out.append(ln[:max_chars])
            ln = ln[max_chars:]
        if cur and len(cur) + len(sep) + len(ln) > max_chars:
            out.append(cur)
            cur = ln
        else:
            cur = f"{cur}{sep}{ln}" if cur else ln
    if cur:
        out.append(cur)
    return out


def _table_pieces(table: str, max_chars: int) -> list[str]:
    lines = table.split("\n")
    header = lines[:2]
    body = lines[2:]
    head_txt = "\n".join(header)
    out: list[str] = []
    cur: list[str] = []
    size = len(head_txt)
    for row in body:
        if len(row) + len(head_txt) > max_chars:  # a single row that holds a whole article: split it like prose
            if cur:
                out.append(head_txt + "\n" + "\n".join(cur))
                cur, size = [], len(head_txt)
            text = re.sub(r"^\|\s*|\s*\|$", "", row).replace("\\|", "|")
            pieces = [s for part in re.split(r";\s+", text) for s in _SENTENCE.split(part) if s.strip()]
            out.extend(_group_lines(pieces, max_chars, sep=" "))
            continue
        if cur and size + len(row) + 1 > max_chars:
            out.append(head_txt + "\n" + "\n".join(cur))
            cur, size = [], len(head_txt)
        cur.append(row)
        size += len(row) + 1
    if cur:
        out.append(head_txt + "\n" + "\n".join(cur))
    return out
