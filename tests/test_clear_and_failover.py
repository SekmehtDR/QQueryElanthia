import asyncio
import json
import struct
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from epedia.config import Settings
from epedia.db import connect
from epedia.frontends.web.app import create_app
from epedia.index.reset import clear_index, index_size
from epedia.wiki import client as wc
from epedia.wiki.client import FailoverClient, WikiClient, WikiError, open_client

LOCAL = {"X-QQE": "1", "Host": "localhost:8765"}


def seed(conn, pages=3):
    for i in range(1, pages + 1):
        conn.execute("INSERT INTO pages(page_id, ns, title, title_norm, updated_at, fetched_revid, latest_revid, indexed_revid) "
                     "VALUES (?, 0, ?, ?, '2026-01-01', 5, 5, 5)", (i, f"Page {i}", f"page {i}"))
        conn.execute("INSERT INTO chunks(chunk_id, page_id, revid, ord, kind, title, section_path, text, token_est, content_hash) "
                     "VALUES (?, ?, 5, 0, 'section', ?, '', 'forging hammer text', 3, ?)", (i, i, f"Page {i}", str(i)))
        conn.execute("INSERT INTO chunk_vec(chunk_id, embedding) VALUES (?, ?)", (i, struct.pack("768f", *([1.0] + [0.0] * 767))))
        conn.execute("INSERT INTO titles_fts(title_norm, page_id, is_redirect) VALUES (?, ?, 0)", (f"page {i}", i))


@pytest.fixture
def conn(tmp_path):
    s = Settings()
    s.db.path = str(tmp_path / "t.db")
    c = connect(s)
    yield c
    c.close()


def test_clearing_the_index_empties_search_but_keeps_pages_and_rebuilds_cleanly(conn):
    seed(conn)
    conn.execute("INSERT INTO sessions(session_id, frontend, created_at) VALUES ('s1', 'web', '2026-01-01')")
    assert index_size(conn) == {"chunks": 3, "vectors": 3, "titles": 3, "pages": 3}
    removed = clear_index(conn)
    assert removed["chunks"] == 3
    assert index_size(conn) == {"chunks": 0, "vectors": 0, "titles": 0, "pages": 3}  # pages stay
    assert conn.execute("SELECT count(*) FROM chunks_fts WHERE chunks_fts MATCH 'hammer'").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM pages WHERE indexed_revid IS NOT NULL").fetchone()[0] == 0  # all pending again
    assert conn.execute("SELECT count(*) FROM pages WHERE fetched_revid IS NOT NULL").fetchone()[0] == 3  # downloads kept
    assert conn.execute("SELECT count(*) FROM sessions").fetchone()[0] == 1  # conversations kept
    # the keyword-index triggers are back, so the rebuild feeds search again
    conn.execute("INSERT INTO chunks(chunk_id, page_id, revid, ord, kind, title, section_path, text, token_est, content_hash) "
                 "VALUES (99, 1, 5, 0, 'section', 'Page 1', '', 'anvil and tongs', 3, 'zz')")
    assert conn.execute("SELECT count(*) FROM chunks_fts WHERE chunks_fts MATCH 'anvil'").fetchone()[0] == 1
    assert clear_index(conn)["chunks"] == 1 and index_size(conn)["chunks"] == 0  # repeatable


def test_the_clear_endpoint_needs_the_header_the_word_and_an_idle_index(tmp_path):
    s = Settings()
    s.db.path = str(tmp_path / "t.db")
    c = connect(s)
    seed(c)
    c.close()
    with TestClient(create_app(s)) as client:
        body = {"confirm": "CLEAR"}
        assert client.post("/api/index/clear", json=body, headers={"Host": "localhost:8765"}).status_code == 403
        assert client.post("/api/index/clear", json={"confirm": "clear"}, headers=LOCAL).status_code == 400
        assert client.get("/api/index/info").json()["chunks"] == 3
        ok = client.post("/api/index/clear", json=body, headers=LOCAL)
        assert ok.status_code == 200 and ok.json()["chunks"] == 3
        info = client.get("/api/index/info").json()
        assert info["chunks"] == 0 and info["pending_index"] == 3  # the panel can say "index is empty, click Re-index"


def test_the_clear_endpoint_refuses_while_a_re_index_is_running(tmp_path):
    s = Settings()
    s.db.path = str(tmp_path / "t.db")
    c = connect(s)
    seed(c)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    c.execute("INSERT OR REPLACE INTO state VALUES ('reindex.progress', ?)",
              (json.dumps({"total": 3, "done": 1, "started_at": now, "updated_at": now, "finished_at": None}),))
    c.close()
    with TestClient(create_app(s)) as client:
        assert client.post("/api/index/clear", json={"confirm": "CLEAR"}, headers=LOCAL).status_code == 409
        assert client.get("/api/index/info").json()["chunks"] == 3  # untouched


# ----------------------------------------------------------------------------------------------- failover


def failover(primary_script, secondary_script):
    s = Settings()
    s.wiki.sources["mirror"] = s.wiki.sources["original"].model_copy(update={"api_url": "https://mirror.example/api.php"})
    s.wiki.source, s.wiki.fallback_source = "mirror", "original"
    fc = FailoverClient(s.wiki)
    calls = {"primary": 0, "secondary": 0}

    def make(name, script):
        async def get(params):
            calls[name] += 1
            if isinstance(script, Exception):
                raise script
            return script

        return get

    fc.primary.get, fc.secondary.get = make("primary", primary_script), make("secondary", secondary_script)
    return fc, calls


def test_a_healthy_preferred_source_is_used_and_the_fallback_is_untouched():
    fc, calls = failover({"from": "mirror"}, {"from": "original"})
    assert asyncio.run(fc.get({})) == {"from": "mirror"} and calls == {"primary": 1, "secondary": 0}


def test_an_unreachable_or_lagging_source_falls_back_to_the_original():
    fc, calls = failover(WikiError("transport", "boom"), {"from": "original"})
    assert asyncio.run(fc.get({})) == {"from": "original"} and calls["secondary"] == 1
    fc, calls = failover(WikiError("missingtitle", "no page"), {"from": "original"})  # a mirror that has not got it yet
    assert asyncio.run(fc.get({})) == {"from": "original"}
    fc, calls = failover(WikiError("missingtitle", "no page"), WikiError("missingtitle", "no page"))
    with pytest.raises(WikiError) as exc:  # missing on both: really gone
        asyncio.run(fc.get({}))
    assert exc.value.code == "missingtitle"


def test_other_api_errors_are_real_and_not_masked_by_the_fallback():
    fc, calls = failover(WikiError("badvalue", "bad"), {"from": "original"})
    with pytest.raises(WikiError):
        asyncio.run(fc.get({}))
    assert calls["secondary"] == 0


def test_a_dead_mirror_is_skipped_after_a_few_failures():
    fc, calls = failover(WikiError("transport", "down"), {"from": "original"})

    async def go():
        for _ in range(wc.BREAKER_FAILURES + 4):
            await fc.get({})

    asyncio.run(go())
    assert calls["primary"] == wc.BREAKER_FAILURES  # then it is not even tried
    assert calls["secondary"] == wc.BREAKER_FAILURES + 4


def test_open_client_adds_the_fallback_only_when_one_is_configured():
    s = Settings()
    assert isinstance(open_client(s.wiki), WikiClient)
    s.wiki.sources["mirror"] = s.wiki.sources["original"]
    s.wiki.source, s.wiki.fallback_source = "mirror", "original"
    assert isinstance(open_client(s.wiki), FailoverClient)
    s.wiki.source = "original"  # --source original: nothing to fall back to
    assert isinstance(open_client(s.wiki), WikiClient)
