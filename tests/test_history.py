import sqlite3

import pytest
from fastapi.testclient import TestClient

from epedia.config import Settings
from epedia.frontends.web.app import create_app
from epedia.wiki import history

LOCAL = {"X-QQE": "1", "Host": "localhost:8765"}


def conn_with(rows):
    c = sqlite3.connect(":memory:", isolation_level=None)  # autocommit, as db.connect does
    c.row_factory = sqlite3.Row
    c.executescript(
        "CREATE TABLE revisions (revid INTEGER PRIMARY KEY, title TEXT, ts TEXT, user TEXT, comment TEXT, size INT,"
        " parentid INT, tags TEXT); CREATE TABLE state (key TEXT PRIMARY KEY, value TEXT);")
    c.executemany("INSERT INTO revisions VALUES (?,?,?,?,?,?,?,?)", rows)
    return c


def rev(i, day, user, size, comment="", tags=""):
    return (i, "Binu", f"2026-01-{day:02d}T10:00:00Z", user, comment, size, i - 1, tags)


def test_rows_from_and_store_are_idempotent():
    page = {"title": "Binu", "revisions": [{"revid": 5, "timestamp": "2026-01-01T00:00:00Z", "user": "A", "size": 10,
                                              "parentid": 4, "tags": ["mw-reverted"]}]}
    c = conn_with([])
    assert history.store(c, history.rows_from(page)) == 1
    history.store(c, history.rows_from(page))
    assert c.execute("SELECT count(*), tags FROM revisions").fetchone()[:] == (1, "mw-reverted")


def test_recent_edits_measure_each_edit_against_the_one_before_it():
    c = conn_with([rev(1, 1, "A", 1000), rev(2, 2, "B", 1100), rev(3, 3, "B", 1090)])
    edits = history.recent_edits(c, "Binu", 2)
    assert [e.delta for e in edits] == [-10, 100]  # newest first; the oldest shown is measured against the hidden one


def test_steady_page_built_up_by_small_additions():
    c = conn_with([rev(i, i, "A" if i % 2 else "B", 1000 + 40 * i) for i in range(1, 8)])
    verdict, reasons = history.assess(history.recent_edits(c, "Binu", 6))
    assert verdict == "steady" and any("small additions" in r for r in reasons)


def test_a_rewrite_is_mixed_and_a_rewrite_that_is_reverted_is_contested():
    c = conn_with([rev(1, 1, "A", 1000), rev(2, 2, "B", 300, "blanked"), rev(3, 3, "A", 1000, "Undo revision 2")])
    verdict, reasons = history.assess(history.recent_edits(c, "Binu", 3))
    assert verdict == "contested" and any("large rewrite" in r for r in reasons)
    c = conn_with([rev(1, 1, "A", 1000), rev(2, 2, "B", 2000)])
    assert history.assess(history.recent_edits(c, "Binu", 2))[0] == "mixed"


def test_anonymous_editors_and_a_single_owner_are_noticed():
    c = conn_with([rev(1, 1, "A", 1000), rev(2, 2, "203.0.113.9", 1001)])
    verdict, reasons = history.assess(history.recent_edits(c, "Binu", 2))
    assert verdict == "mixed" and any("anonymous" in r for r in reasons)
    c = conn_with([rev(1, 1, "Binu", 1000), rev(2, 2, "Binu", 1010)])
    assert any("all of these edits are by Binu" in r for r in history.assess(history.recent_edits(c, "Binu", 2))[1])


def test_passage_lists_edits_with_the_reading_and_respects_the_setting():
    c = conn_with([rev(1, 1, "A", 1000), rev(2, 2, "B", 300, "blanked"), rev(3, 3, "A", 1000, "Undo revision 2")])
    p = history.passage(c, "Binu", 5, "https://elanthipedia.play.net")
    assert p and p["text"].startswith("Reading of the history: contested.") and "[revert]" in p["text"]
    assert p["url"] == "https://elanthipedia.play.net/index.php?title=Binu&action=history"
    assert history.passage(c, "Binu", 0, "https://x") is None
    assert history.passage(c, "Nobody", 5, "https://x") is None


def test_setting_is_clamped_and_defaults_when_unset():
    c = conn_with([])
    assert history.edits_setting(c, 10) == 10
    assert history.set_edits_setting(c, 500) == history.MAX_EDITS
    assert history.set_edits_setting(c, -3) == 0 and history.edits_setting(c, 10) == 0


@pytest.fixture
def client(tmp_path):
    s = Settings()
    s.db.path = str(tmp_path / "t.db")
    with TestClient(create_app(s)) as c:
        yield c


def test_the_setting_is_changed_from_the_page_only_by_the_owner(client):
    assert client.post("/api/settings/history", json={"history_edits": 3}, headers={"Host": "localhost:8765"}).status_code == 403
    tunnel = {**LOCAL, "cf-connecting-ip": "203.0.113.9"}
    assert client.post("/api/settings/history", json={"history_edits": 3}, headers=tunnel).status_code in (401, 403)
    r = client.post("/api/settings/history", json={"history_edits": 3}, headers=LOCAL)
    assert r.status_code == 200 and r.json()["history_edits"] == 3
    assert client.get("/api/index/info", headers={"Host": "localhost:8765"}).json()["history_edits"] == 3
