"""Convert MediaWiki-rendered HTML (action=parse output) into section blocks of clean, readable text.

Tables are flattened: key/value shaped tables (infoboxes, item stat blocks) become `Key: value` lines,
everything else becomes a Markdown table with rowspan/colspan expanded.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from bs4 import BeautifulSoup, Comment, Doctype, NavigableString, Tag

REMOVE_SELECTORS = [
    ".mw-editsection", "#toc", ".toc", ".navbox", ".noprint", ".printfooter", ".catlinks",
    ".mw-empty-elt", "style", "script", "sup.reference", ".mw-references-wrap", ".mw-cite-backlink",
    ".smwttcontent", ".thumbcaption .magnify", "noscript",
]
HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
SKIP_CLASSES = {"mw-empty-elt"}
WS = re.compile(r"[ \t\r\f\v\xa0]+")


@dataclass
class Block:
    level: int | None  # None = lead (text before the first heading)
    heading: str | None
    anchor: str | None
    parts: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n\n".join(p for p in self.parts if p.strip()).strip()


@dataclass
class ParsedPage:
    blocks: list[Block]
    notices: list[str] = field(default_factory=list)  # maintenance banners removed: incomplete, stub, obsolete, dr3
    soft_redirect: str | None = None  # target title when the page is only a "Redirect to: X" stub


# Maintenance banners the wiki puts on pages. They say "this page is weak", which is worth one line, but their
# boilerplate (and the "see Category: ..." links) is noise in every chunk and in every embedding.
BANNER_MARKERS = {
    "incomplete": re.compile(r"This article is incomplete|Missing or Incomplete Creature Information", re.I),
    "stub": re.compile(r"This article is a stub", re.I),
    "obsolete": re.compile(r"This article is obsolete", re.I),
    "dr3": re.compile(r"needs to be updated for consistency with DR ?3|^DR3\b", re.I),
}
BANNER_MAX_CHARS = 900  # a banner is short; never remove a container that holds real content
DROP_HEADINGS = {"related forum posts"}


# Cheap pre-check. Markup can sit inside the phrase ("This article is <b>incomplete</b>"), so "This article is"
# and the banner's own title are hints too.
_BANNER_HINTS = ("This article is", "Incomplete Article", "consistency with DR", "DR3", "Incomplete Creature")


def _strip_banners(root: Tag, html: str) -> list[str]:
    found: list[str] = []
    if not any(h in html for h in _BANNER_HINTS):
        return found  # most pages have no banner; skip the per-element text scan
    for el in list(root.find_all(["div", "table"])):
        if el is root or getattr(el, "decomposed", False):
            continue
        text = el.get_text(" ", strip=True)
        if not text or len(text) > BANNER_MAX_CHARS:
            continue
        head = text[:160]
        for key, pattern in BANNER_MARKERS.items():
            if pattern.search(head):
                if key not in found:
                    found.append(key)
                el.decompose()
                break
    return found


def html_to_blocks(html: str) -> list[Block]:
    return html_to_page(html).blocks


def html_to_page(html: str) -> ParsedPage:
    soup = BeautifulSoup(html, "lxml")
    root = soup.select_one("div.mw-parser-output") or soup.body or soup
    redirect = root.select_one("div.redirectMsg")
    if redirect is not None:
        link = redirect.select_one("ul.redirectText a")
        target = ((link.get("title") or link.get_text()) if link else "").strip()
        return ParsedPage([Block(None, None, None)], [], re.sub(r" \(page does not exist\)$", "", target))
    for sel in REMOVE_SELECTORS:
        for el in root.select(sel):
            el.decompose()
    for el in list(root.find_all(style=re.compile(r"display\s*:\s*none", re.I))):
        el.decompose()
    notices = _strip_banners(root, html)

    blocks = [Block(None, None, None)]

    def current() -> Block:
        return blocks[-1]

    def emit(text: str) -> None:
        text = text.strip("\n")
        if text.strip():
            current().parts.append(text)

    def walk(node: Tag) -> None:
        for child in node.children:
            if isinstance(child, NavigableString):
                if isinstance(child, (Comment, Doctype)):
                    continue
                s = WS.sub(" ", str(child)).strip()
                if s:  # stray text directly in a container
                    emit(s)
                continue
            if not isinstance(child, Tag):
                continue
            name = child.name
            if name in HEADING_TAGS:
                level = int(name[1])
                span = child.select_one(".mw-headline")
                anchor = (span.get("id") if span else None) or child.get("id")
                title = _inline(span or child).strip()
                blocks.append(Block(level, title, anchor))
            elif name == "div" and "mw-heading" in (child.get("class") or []):
                walk(child)  # MW >= 1.43 wraps headings in a div
            elif name == "p":
                emit(_inline(child))
            elif name in ("ul", "ol"):
                emit(_list(child, 0))
            elif name == "dl":
                emit(_dl(child))
            elif name == "pre":
                emit("```\n" + child.get_text() + "\n```")
            elif name == "table":
                emit(_table(child))
            elif name in ("div", "section", "blockquote", "center", "span", "figure", "details", "article"):
                if _is_inline_only(child):
                    emit(_inline(child))
                else:
                    walk(child)
            elif name in ("li", "dd", "dt"):
                emit(_inline(child))
            elif name in ("br", "hr", "img"):
                continue
            else:
                emit(_inline(child))

    walk(root)
    out = [b for b in blocks if (b.text or b.heading) and (b.heading or "").strip().lower() not in DROP_HEADINGS]
    return ParsedPage(out or [Block(None, None, None)], notices)


# ---------------------------------------------------------------- inline


def _is_inline_only(tag: Tag) -> bool:
    block_names = {"p", "ul", "ol", "dl", "table", "pre", "div", "section", "blockquote", *HEADING_TAGS}
    return not any(isinstance(c, Tag) and c.name in block_names for c in tag.descendants)


def _inline(tag: Tag) -> str:
    parts: list[str] = []

    def rec(n: Tag) -> None:
        for c in n.children:
            if isinstance(c, NavigableString):
                if isinstance(c, (Comment, Doctype)):
                    continue
                parts.append(str(c))
            elif isinstance(c, Tag):
                if c.name == "br":
                    parts.append("\n")
                elif c.name == "img":
                    alt = (c.get("alt") or "").strip()
                    if alt and not alt.lower().endswith((".jpg", ".png", ".gif")):
                        parts.append(f"[{alt}]")
                elif c.name in ("ul", "ol"):
                    parts.append("\n" + _list(c, 1) + "\n")
                else:
                    rec(c)

    rec(tag)
    text = "".join(parts).replace("\xa0", " ")
    lines = [WS.sub(" ", ln).strip() for ln in text.split("\n")]
    return "\n".join(ln for ln in lines if ln)


def _list(tag: Tag, depth: int) -> str:
    lines: list[str] = []
    ordered = tag.name == "ol"
    n = 0
    for li in tag.find_all("li", recursive=False):
        n += 1
        marker = f"{n}. " if ordered else "- "
        own = _own_text(li)
        lines.append("  " * depth + marker + own)
        for sub in li.find_all(["ul", "ol"], recursive=False):
            lines.append(_list(sub, depth + 1))
        for sub in li.find_all("dl", recursive=False):
            lines.append(_dl(sub, depth + 1))
    return "\n".join(ln for ln in lines if ln.strip())


def _own_text(li: Tag) -> str:
    """Text of a list item / definition excluding its nested lists."""
    clone = BeautifulSoup(str(li), "lxml").find(li.name)
    if clone is None:
        return _inline(li)
    for sub in clone.find_all(["ul", "ol", "dl"]):
        sub.decompose()
    return _inline(clone).replace("\n", " ")


def _dl(tag: Tag, depth: int = 0) -> str:
    lines: list[str] = []
    for c in tag.children:
        if not isinstance(c, Tag):
            continue
        if c.name == "dt":
            lines.append("  " * depth + _inline(c).replace("\n", " ") + ":")
        elif c.name == "dd":
            nested = c.find_all("dl", recursive=False)
            own = _own_text(c) if nested else _inline(c).replace("\n", " ")
            if own:
                lines.append("  " * depth + own)
            for sub in nested:
                lines.append(_dl(sub, depth + 1))
    return "\n".join(ln for ln in lines if ln.strip())


# ---------------------------------------------------------------- tables


@dataclass
class Cell:
    text: str
    header: bool
    bold_only: bool
    span: int = 1


def _cell_text(cell: Tag) -> str:
    return _inline(cell).replace("\n", "; ").replace("|", "\\|").strip(" ;")


def _span(cell: Tag, attr: str) -> int:
    """colspan/rowspan from hand-written wiki markup, which can be malformed (e.g. colspan="=2")."""
    m = re.search(r"\d+", str(cell.get(attr) or ""))
    return max(1, min(int(m.group()), 50)) if m else 1


def _is_bold_only(cell: Tag) -> bool:
    kids = [c for c in cell.children if not (isinstance(c, NavigableString) and not str(c).strip())]
    return len(kids) >= 1 and all(isinstance(c, Tag) and c.name in ("b", "strong") for c in kids)


def _unwrap_wrapper(table: Tag) -> str | None:
    """Layout tables that only wrap other tables (the creature infobox sits in a one-cell float table).

    Returns the rendered inner content, or None if this is not a wrapper."""
    trs = [tr for tr in table.find_all("tr") if tr.find_parent("table") is table]
    cells = [c for tr in trs for c in tr.find_all(["td", "th"], recursive=False)]
    if not cells or any(len(tr.find_all(["td", "th"], recursive=False)) != 1 for tr in trs):
        return None
    if not any(c.find("table") for c in cells):
        return None
    parts: list[str] = []
    for cell in cells:
        inner = [t for t in cell.find_all("table") if t.find_parent("table") is table]
        for t in inner:
            parts.append(_table(t))
        clone = BeautifulSoup(str(cell), "lxml")
        for t in clone.find_all("table"):
            t.decompose()
        stray = _inline(clone.body or clone)
        if stray.strip():
            parts.append(stray)
    return "\n\n".join(p for p in parts if p.strip())


def _table(table: Tag) -> str:
    wrapped = _unwrap_wrapper(table)
    if wrapped is not None:
        return wrapped
    rows = []
    for tr in table.find_all("tr"):
        if tr.find_parent("table") is not table:
            continue  # nested table rows are handled by their own table
        cells = [
            Cell(_cell_text(c), c.name == "th", _is_bold_only(c), _span(c, "colspan"))
            for c in tr.find_all(["td", "th"], recursive=False)
        ]
        rows.append((cells, tr))
    rows = [(cells, tr) for cells, tr in rows if cells]
    if not rows:
        return ""
    caption = table.find("caption")
    prefix = (_inline(caption).strip() + "\n") if caption else ""
    if all(len(cells) == 1 for cells, _ in rows):
        # One column: an infobox whose cells already read "Label: value", or a plain list. Lines beat a "Col 1" grid.
        return prefix + "\n".join(cells[0].text for cells, _ in rows if cells[0].text)
    if _is_kv_table(table, rows):
        return prefix + _render_kv(rows)
    return prefix + _render_grid(table)


def _is_kv_table(table: Tag, rows) -> bool:
    two = [cells for cells, _ in rows if len(cells) == 2 and not any(c.span > 1 for c in cells)
           and (cells[0].header or cells[0].bold_only)]
    if "infobox" in " ".join(table.get("class") or []):
        return len(two) >= 1
    if len(two) >= 3 and len(two) >= 0.6 * len(rows):
        return True
    # Plain label/value tables whose labels are ordinary cells (creature infoboxes): many short two-cell rows.
    short = [cells for cells, _ in rows if len(cells) == 2 and not any(c.span > 1 for c in cells)
             and 0 < len(cells[0].text) <= 40]
    return len(short) >= 5 and len(short) >= 0.6 * len(rows)


def _render_kv(rows) -> str:
    lines: list[str] = []
    for i, (cells, _) in enumerate(rows):
        if i == 0 and len(cells) == 2 and all(c.header for c in cells):
            continue  # a "Name | Value" header row, not a data pair
        if len(cells) == 2 and (cells[0].header or cells[0].bold_only or not cells[0].span > 1):
            key = cells[0].text.rstrip(":").strip()
            val = cells[1].text
            if key and val:
                lines.append(f"{key}: {val}")
            elif key and not val:
                continue  # label with no value (e.g. empty "Properties:")
        else:
            joined = " ".join(c.text for c in cells if c.text).strip()
            if joined:
                lines.append(joined)
    return "\n".join(lines)


def _render_grid(table: Tag) -> str:
    grid: list[list[str]] = []
    pending: dict[tuple[int, int], str] = {}
    header_row = None
    trs = [tr for tr in table.find_all("tr") if tr.find_parent("table") is table]
    for r, tr in enumerate(trs):
        row: list[str] = []
        c = 0
        cells = tr.find_all(["td", "th"], recursive=False)

        def fill_pending() -> None:
            nonlocal c
            while (r, c) in pending:
                row.append(pending.pop((r, c)))
                c += 1

        for cell in cells:
            fill_pending()
            text = _cell_text(cell)
            cs = _span(cell, "colspan")
            rs = _span(cell, "rowspan")
            for k in range(cs):
                row.append(text if k == 0 else "")
                for dr in range(1, rs):
                    pending[(r + dr, c)] = text if k == 0 else ""
                c += 1
        fill_pending()
        if any(x.strip() for x in row):
            grid.append(row)
            if header_row is None and cells and all(x.name == "th" for x in cells):
                header_row = len(grid) - 1
    if not grid:
        return ""
    width = max(len(r) for r in grid)
    grid = [r + [""] * (width - len(r)) for r in grid]
    if header_row is None:
        head, body = [f"Col {i + 1}" for i in range(width)], grid
    else:
        head, body = grid[header_row], grid[:header_row] + grid[header_row + 1:]
    lines = ["| " + " | ".join(head) + " |", "|" + "---|" * width]
    lines += ["| " + " | ".join(r) + " |" for r in body]
    return "\n".join(lines)
