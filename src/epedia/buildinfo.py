"""What is running right now: version, source commit, models and the state of the index.

The git commit and the index counts are cached for a short time (they change rarely, and the web page asks on every
load); both are refreshed after the TTL, so a `git pull` or a sync shows up without restarting the server.
"""

from __future__ import annotations

import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Any

from . import __version__
from .config import Settings

REPO_ROOT = Path(__file__).resolve().parents[2]
TTL_SECONDS = 60.0

_commit_cache: dict[str, tuple[float, str | None]] = {}
_index_cache: dict[str, tuple[float, int, str | None]] = {}


def _is_project_checkout(root: Path) -> bool:
    """True only when `root` is this project's own git checkout: a non-editable install lives under
    site-packages, whose parents may belong to some unrelated repository that must not be reported as ours."""
    return (root / ".git").exists() and (root / "pyproject.toml").is_file() and (root / "src" / "epedia").is_dir()


def git_commit(root: Path | None = None) -> str | None:
    """Short hash of the checked-out commit, or None when not running from this project's git checkout."""
    root = root or REPO_ROOT
    key = str(root)
    cached = _commit_cache.get(key)
    if cached and time.monotonic() - cached[0] < TTL_SECONDS:
        return cached[1]
    commit: str | None = None
    if _is_project_checkout(root):
        try:
            out = subprocess.run(
                ["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
                capture_output=True, text=True, timeout=3, check=False,
            )
            if out.returncode == 0 and out.stdout.strip():
                commit = out.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            commit = None
    _commit_cache[key] = (time.monotonic(), commit)
    return commit


def _index_state(conn: sqlite3.Connection, key: str) -> tuple[int, str | None]:
    """(page count, last update), tolerating a database that has not been built yet."""
    cached = _index_cache.get(key)
    if cached and time.monotonic() - cached[0] < TTL_SECONDS:
        return cached[1], cached[2]
    try:
        pages = conn.execute("SELECT count(*) FROM pages WHERE is_redirect=0").fetchone()[0]
        last = conn.execute("SELECT value FROM state WHERE key='sync.last_run'").fetchone()
        if not last:
            last = conn.execute("SELECT max(fetched_at) FROM page_html").fetchone()
        last_sync = (last[0] or None) if last else None
    except sqlite3.OperationalError:  # no tables yet: a fresh checkout, before the first build
        pages, last_sync = 0, None
    _index_cache[key] = (time.monotonic(), pages, last_sync)
    return pages, last_sync


def build_info(settings: Settings, conn: sqlite3.Connection) -> dict[str, Any]:
    pages, last_sync = _index_state(conn, str(settings.db_path))
    return {
        "version": __version__,
        "commit": git_commit(),
        "chat_model": settings.ollama.chat_model,
        "embed_model": settings.ollama.embed_model,
        "pages": pages,
        "last_sync": last_sync,
        "source": settings.wiki.source,
    }
