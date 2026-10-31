"""The What's new window: what the version badge opens, told by Fennick and then plainly."""

import re
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from epedia import __version__
from epedia.config import Settings
from epedia.core import whatsnew
from epedia.frontends.web.app import STATIC, create_app

LOCAL = {"Host": "localhost:8765"}
HTML = STATIC / "index.html"


@pytest.fixture
def client(tmp_path):
    s = Settings()
    s.db.path = str(tmp_path / "t.db")
    with TestClient(create_app(s)) as c:
        yield c


def test_the_running_version_has_an_entry_and_the_newest_comes_first():
    releases = whatsnew.recent(99)
    assert releases[0]["version"] == __version__
    numbers = [tuple(int(x) for x in r["version"].split(".")) for r in releases]
    assert numbers == sorted(numbers, reverse=True) and len(set(numbers)) == len(numbers)


def test_every_entry_has_fennick_then_real_changes():
    for r in whatsnew.recent(99):
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", r["date"])
        assert len(r["fennick"]) > 40 and r["changes"] and all(c.strip() for c in r["changes"])


def test_it_keeps_to_what_players_would_notice():
    text = " ".join(r["fennick"] + " " + " ".join(r["changes"]) for r in whatsnew.recent(99)).casefold()
    # his voice's rules (llm/prompts.py), and nothing about how it is built or where it reads from
    for word in ("the chronicle", "the passages", "according to the notes", "i saw", "i visited", "i met", "mirror", "tunnel", "ollama",
                 "qwen", "sqlite", "database", "planner", "schema", "access code"):
        assert word not in text, word


def test_the_entries_reach_the_page_and_visitors_may_read_them(client):
    got = client.get("/api/whats-new", headers=LOCAL).json()["releases"]
    assert got[0]["version"] == __version__ and len(got) <= 6


def test_an_edit_shows_at_once_with_no_restart(client, tmp_path, monkeypatch):
    path = tmp_path / "whatsnew.json"
    path.write_text('[{"version": "9.9.9"}]', encoding="utf-8")
    monkeypatch.setattr(whatsnew, "PATH", path)
    assert client.get("/api/whats-new", headers=LOCAL).json()["releases"][0]["version"] == "9.9.9"


def test_a_commit_that_leaves_whats_new_alone_is_refused():
    hook = Path(__file__).parents[1] / "tools" / "hooks" / "pre-commit"
    text = hook.read_text(encoding="utf-8")
    assert text.startswith("#!/bin/sh") and "src/epedia/core/whatsnew.json" in text and "exit 1" in text


def test_the_badge_is_a_real_button_that_opens_a_dialog():
    html = HTML.read_text(encoding="utf-8")
    assert '<button type="button" class="ver" id="pver"' in html and '<dialog id="whatsnew"' in html
    assert "getElementById('pver').addEventListener('click', openWhatsNew)" in html


def test_the_about_window_keeps_its_look():
    html = HTML.read_text(encoding="utf-8")
    assert '<dialog id="about" class="paper"' in html and "dialog.paper" in html and "#about " not in html.split("</style>")[0]


@pytest.mark.skipif(not shutil.which("node"), reason="needs node")
def test_the_pages_code_really_runs():
    script = Path(__file__).parent / "js" / "run_whatsnew.js"
    out = subprocess.run(["node", str(script), str(HTML)], capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert out.stdout.count("ok ") == 6 and "BAD" not in out.stdout and "THREW" not in out.stdout, out.stdout + out.stderr
