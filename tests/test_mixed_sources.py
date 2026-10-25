import asyncio

import pytest

from epedia.config import Settings
from epedia.db import connect, get_state, set_state
from epedia.wiki import sync as syncmod

MIRROR_NEWEST = "2026-10-23T02:48:21Z"


class FakeClient:
    """Lists a fixed set of pages the way allpages+info does."""

    def __init__(self, pages):
        self.pages = pages

    async def paginate(self, params):
        yield {"query": {"pages": self.pages}}


def make(tmp_path, delta_source):
    s = Settings()
    s.db.path = str(tmp_path / "t.db")
    s.wiki.namespaces = [0]
    s.wiki.sources["mirror"] = s.wiki.sources["original"].model_copy(update={"api_url": "https://mirror.example/api.php"})
    s.wiki.source, s.wiki.fallback_source, s.wiki.delta_source = "mirror", "original", delta_source
    conn = connect(s)
    for pid, title, fetched in [(1, "Fresh", "2026-10-23T09:00:00Z"), (2, "Edited", "2026-10-23T01:00:00Z"),
                                (3, "Only here", "2026-10-23T09:00:00Z")]:
        conn.execute("INSERT INTO pages(page_id, ns, title, title_norm, updated_at, fetched_revid, latest_revid, touched) "
                     "VALUES (?, 0, ?, ?, '2026-01-01', 7, 7, '2026-10-01T00:00:00Z')", (pid, title, title.lower()))
        conn.execute("INSERT INTO page_html(page_id, revid, html, sections, fetched_at) VALUES (?, 7, x'00', '[]', ?)", (pid, fetched))
    set_state(conn, "sync.cursor", "2026-10-23T15:00:00Z")
    return s, conn


def run_scan(s, conn, monkeypatch):
    async def nothing(*a, **k):
        return {}

    async def newest(client):
        return MIRROR_NEWEST

    monkeypatch.setattr(syncmod, "discover", nothing)
    monkeypatch.setattr(syncmod, "run_pipeline_for_changes", nothing)
    monkeypatch.setattr(syncmod, "newest_change_timestamp", newest)
    # the mirror has: Fresh (touched before we fetched it), Edited (touched after), and a page we have not seen
    pages = [
        {"title": "Fresh", "ns": 0, "touched": "2026-10-23T02:00:00Z", "lastrevid": 900, "length": 100},
        {"title": "Edited", "ns": 0, "touched": "2026-10-23T02:30:00Z", "lastrevid": 901, "length": 100},
        {"title": "Brand new", "ns": 0, "touched": "2026-10-23T02:40:00Z", "lastrevid": 902, "length": 100},
    ]
    return asyncio.run(syncmod.full_scan(s, conn, FakeClient(pages), None))


def test_a_mirror_full_scan_compares_edit_times_and_never_deletes_what_the_mirror_lacks(tmp_path, monkeypatch):
    s, conn = make(tmp_path, delta_source="original")
    stats = run_scan(s, conn, monkeypatch)
    assert stats == {"new": 1, "stale": 1, "removed": 0}  # "Fresh" is current whatever revision ids say
    titles = {r[0] for r in conn.execute("SELECT title FROM pages")}
    assert "Only here" in titles and "Brand new" in titles  # absent on a lagging mirror is not deleted
    stale = {r[0] for r in conn.execute("SELECT title FROM pages WHERE fetched_revid IS NULL")}
    assert "Edited" in stale and "Fresh" not in stale
    # the delta cursor goes back to just before the mirror's newest edit, so the next delta covers what it lacks
    assert get_state(conn, "sync.cursor") == "2026-10-23T01:48:21Z"


def test_a_full_scan_of_the_authoritative_wiki_still_removes_deleted_pages_and_leaves_the_cursor(tmp_path, monkeypatch):
    s, conn = make(tmp_path, delta_source=None)
    s.wiki.source, s.wiki.fallback_source = "original", None
    stats = run_scan(s, conn, monkeypatch)
    assert stats["removed"] == 1 and conn.execute("SELECT count(*) FROM pages WHERE title='Only here'").fetchone()[0] == 0
    assert get_state(conn, "sync.cursor") == "2026-10-23T15:00:00Z"


def test_the_cursor_is_only_ever_wound_back_never_forward(tmp_path, monkeypatch):
    s, conn = make(tmp_path, delta_source="original")
    set_state(conn, "sync.cursor", "2026-10-22T00:00:00Z")  # already earlier than the mirror's newest edit
    run_scan(s, conn, monkeypatch)
    assert get_state(conn, "sync.cursor") == "2026-10-22T00:00:00Z"


def test_mixed_sources_replace_the_primary_source_guard(tmp_path):
    from epedia.wiki.discover import SourceMismatch, check_primary_source

    s, conn = make(tmp_path, delta_source="original")
    set_state(conn, "primary_source", "original")
    check_primary_source(conn, s.wiki)  # mirror active, original behind it: fine, no error
    s.wiki.fallback_source = s.wiki.delta_source = None
    with pytest.raises(SourceMismatch):
        check_primary_source(conn, s.wiki)  # a single-source setup still protects revision ids


def test_sync_reads_deltas_from_the_delta_source(monkeypatch):

    from epedia import cli

    seen = {}

    async def fake_sync(settings, conn, client, ollama, dry_run=False):
        seen["source"] = settings.wiki.source
        return {}

    s = Settings()
    s.wiki.sources["mirror"] = s.wiki.sources["original"]
    s.wiki.source, s.wiki.fallback_source, s.wiki.delta_source = "mirror", "original", "original"
    monkeypatch.setattr(cli, "settings", lambda: s)
    monkeypatch.setattr(syncmod, "sync", fake_sync)
    monkeypatch.setattr(cli.db, "connect", lambda *a, **k: type("C", (), {"close": lambda self: None})())
    cli._sync_runner("rc", True, None)
    assert seen["source"] == "original"  # the delta asked the real wiki even though pages are normally read elsewhere


def test_a_crawl_limited_to_named_pages_leaves_the_rest_of_the_backlog_alone(tmp_path):
    from epedia.wiki.crawl import crawl

    s, conn = make(tmp_path, delta_source="original")
    conn.execute("UPDATE pages SET fetched_revid=NULL")  # all three pages are waiting to be fetched
    fetched = []

    class Client:
        async def get(self, params):
            fetched.append(params["page"])
            return {"parse": {"text": "<p>x</p>", "revid": 11, "sections": [], "categories": [], "displaytitle": params["page"]}}

    stats = asyncio.run(crawl(conn, Client(), s.wiki, show_progress=False, titles={"Edited"}))
    assert stats["fetched"] == 1 and fetched == ["Edited"]  # a delta fetches only what it found changed
    assert conn.execute("SELECT count(*) FROM pages WHERE fetched_revid IS NULL").fetchone()[0] == 2  # the backlog waits for Fill
    fetched.clear()
    asyncio.run(crawl(conn, Client(), s.wiki, show_progress=False))  # no titles: everything pending (what Fill does)
    assert sorted(fetched) == ["Fresh", "Only here"]
    assert conn.execute("SELECT count(*) FROM sqlite_temp_master WHERE name='crawl_scope'").fetchone()[0] == 0  # scope table is gone


def test_the_delta_hands_only_its_changed_pages_to_the_pipeline(tmp_path, monkeypatch):
    s, conn = make(tmp_path, delta_source="original")
    seen = {}

    async def fake_changes(client, settings, since):
        return syncmod.Changes(titles={"Edited"}, template_titles=set(), newest="2026-10-23T16:00:00Z")

    async def fake_reconcile(conn, client, settings, ch, stats, details=None):
        details.append(("Edited", "edited"))
        details.append(("Gone", "deleted"))

    async def fake_pipeline(settings, conn, client, ollama, titles=None):
        seen["titles"] = titles

    monkeypatch.setattr(syncmod, "collect_changes", fake_changes)
    monkeypatch.setattr(syncmod, "reconcile", fake_reconcile)
    monkeypatch.setattr(syncmod, "run_pipeline_for_changes", fake_pipeline)
    asyncio.run(syncmod.sync(s, conn, None, None))
    assert seen["titles"] == {"Edited"}  # deleted pages are not fetched, and the backlog is not touched
