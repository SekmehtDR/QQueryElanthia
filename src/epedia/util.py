"""Small shared helpers."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from urllib.parse import quote

log = logging.getLogger("epedia")


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def token_estimate(text: str) -> int:
    """Cheap token count estimate (~4 chars per token for English prose)."""
    return max(1, len(text) // 4)


def page_url(base_url: str, title: str, anchor: str | None = None) -> str:
    path = quote(title.replace(" ", "_"), safe=":/()'!,;=")
    url = base_url.rstrip("/") + "/" + path
    if anchor:
        url += "#" + quote(anchor, safe=":/()'!,;=.")
    return url


def parse_ns_option(value: str | None, default: list[int]) -> list[int]:
    if not value:
        return list(default)
    return [int(x) for x in value.replace(";", ",").split(",") if x.strip()]
