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


def today_text() -> str:
    """Today's real-world date for the model, from this computer's clock ("Friday, October 23, 2026")."""
    from datetime import datetime

    now = datetime.now().astimezone()
    return f"{now:%A, %B} {now.day}, {now.year}"


def now_text() -> str:
    """The date, time and time zone on this computer's clock ("Friday, October 23, 2026, 7:45 PM Pacific Daylight Time")."""
    from datetime import datetime

    now = datetime.now().astimezone()
    hour = now.hour % 12 or 12
    return f"{now:%A, %B} {now.day}, {now.year}, {hour}:{now:%M} {'AM' if now.hour < 12 else 'PM'} {now.tzname()}"
