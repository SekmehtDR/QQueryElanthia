"""What the version badge opens: each update told by Fennick, then plainly, out of character.

The text is `whatsnew.json`, written by hand for players: what they would notice, never how it was built (CHANGES.md is for
that). It is read on every request, so committing an update to it needs no restart, and `tools/hooks/pre-commit` refuses a commit
that does not touch it. Newest first; a test fails if the running version has no entry. Fennick's part follows his rules like any
other: first person for framing only, no adventures of his own, and it says plainly when something broke.
"""

from __future__ import annotations

import json
from pathlib import Path

PATH = Path(__file__).with_name("whatsnew.json")


def recent(limit: int = 6) -> list[dict]:
    return json.loads(PATH.read_text(encoding="utf-8"))[:limit]
