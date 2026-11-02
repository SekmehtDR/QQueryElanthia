"""What the version badge opens: each update told by Fennick, then plainly, out of character.

The text is `whatsnew.json`, written by hand for players: what they would notice, never how it was built (CHANGES.md is for
that). It is read on every request, so committing an update to it needs no restart, and `tools/hooks/pre-commit` refuses a commit
that does not touch it. Newest first; a test fails if the running version has no entry. Fennick's part follows his rules like any
other: first person for framing only, no adventures of his own, and it says plainly when something broke. An entry newer than the running
version is held back: this file is live the moment it is saved, and the server may still be running the version before.
"""

from __future__ import annotations

import json
from pathlib import Path

from .. import __version__

PATH = Path(__file__).with_name("whatsnew.json")


def _order(version: str) -> tuple[int, ...]:
    return tuple(int(n) for n in version.split("."))


def recent(limit: int = 6) -> list[dict]:
    running = _order(__version__)
    return [e for e in json.loads(PATH.read_text(encoding="utf-8")) if _order(e["version"]) <= running][:limit]
