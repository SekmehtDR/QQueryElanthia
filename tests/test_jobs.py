import asyncio
import os
import sqlite3
import subprocess
import sys
import time

import pytest
from fastapi.testclient import TestClient

from epedia.config import Settings
from epedia.frontends.web.app import _host_name, create_app
from epedia.jobs import MODES, JobBusy, JobManager, index_info

QUICK = [sys.executable, "-c", "print('stage one'); print('\\x1b[32mstage two\\x1b[0m'); print()"]
SLOW = [sys.executable, "-c", "import time; print('started', flush=True); time.sleep(60)"]
# a job that starts a grandchild (like a venv launcher does) and reports both process ids
TREE = [sys.executable, "-c",
        "import os, subprocess, sys, time\n"
        "g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "print('pids', os.getpid(), g.pid, flush=True)\n"
        "time.sleep(60)\n"]
LOCAL = {"X-QQE": "1", "Host": "localhost:8765"}


def manager(cmd):
    m = JobManager(Settings())
    m.command = lambda mode: cmd  # never run a real sync from a test
    return m


def pid_alive(pid: int) -> bool:
    if sys.platform == "win32":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_modes_map_to_the_commands_a_person_would_type():
    m = JobManager(Settings())
    assert m.command("delta")[-1] == "sync" and "--full-scan" not in m.command("delta")
    assert m.command("full")[-2:] == ["sync", "--full-scan"]
    assert m.command("delta")[:3] == [sys.executable, "-m", "epedia.cli"]
    with pytest.raises(ValueError):
        m.command("everything")
    assert set(MODES) == {"delta", "full", "reindex", "fill", "history"}
    assert JobManager(Settings()).command("history")[-1] == "history"
    assert JobManager(Settings()).command("fill")[-1] == "fill"
    assert JobManager(Settings()).command("reindex")[-2:] == ["index", "--reembed-all"]


def test_the_child_uses_the_servers_own_config_file_and_wiki_source(tmp_path):
    s = Settings()
    s.config_path = tmp_path / "other.toml"
    s.wiki.source = "original"
    cmd = JobManager(s).command("delta")
    assert cmd[3:7] == ["--config", str(tmp_path / "other.toml"), "--source", "original"]
    assert cmd[-1] == "sync"  # global options come before the subcommand


def test_a_job_runs_to_completion_with_a_clean_log_and_exit_code():
    async def go():
        m = manager(QUICK)
        snap = await m.start("delta")
        assert snap["running"] and snap["label"] == "Delta update"
        await m.wait()
        return m.snapshot()

    snap = asyncio.run(go())
    assert snap["running"] is False and snap["exit_code"] == 0 and snap["finished_at"]
    assert snap["log"] == ["stage one", "stage two"]  # colour codes and blank lines removed


def test_two_simultaneous_starts_launch_exactly_one_job():
    async def go():
        m = manager(SLOW)
        results = await asyncio.gather(m.start("delta"), m.start("full"), return_exceptions=True)
        busy = [r for r in results if isinstance(r, JobBusy)]
        started = [r for r in results if isinstance(r, dict)]
        await m.stop()
        return len(started), len(busy)

    assert asyncio.run(go()) == (1, 1)  # a double click cannot start two syncs on one database


def test_only_one_job_at_a_time_and_stop_ends_it_cleanly():
    async def go():
        m = manager(SLOW)
        await m.start("full")
        with pytest.raises(JobBusy):
            await m.start("delta")
        snap = await m.stop()
        return snap, m.running

    snap, running = asyncio.run(go())
    assert snap["stopped"] is True and snap["running"] is False and running is False
    assert snap["exit_code"] != 0 and snap["finished_at"]  # terminated, and recorded as finished


def test_stop_ends_the_whole_process_tree_not_just_the_launcher():
    async def go():
        m = manager(TREE)
        await m.start("full")
        for _ in range(100):  # wait for the child to report its pids
            if any(line.startswith("pids") for line in m.snapshot()["log"]):
                break
            await asyncio.sleep(0.1)
        line = next(x for x in m.snapshot()["log"] if x.startswith("pids"))
        child, grandchild = (int(x) for x in line.split()[1:])
        await m.stop()
        await asyncio.sleep(0.5)
        return child, grandchild

    child, grandchild = asyncio.run(go())
    assert not pid_alive(child) and not pid_alive(grandchild)  # no orphan keeps crawling after Stop


def test_stopping_when_nothing_runs_is_harmless():
    snap = asyncio.run(JobManager(Settings()).stop())
    assert snap["running"] is False and snap["stopped"] is False


def test_a_job_that_cannot_start_is_reported_as_failed_not_as_a_crash(tmp_path):
    async def go():
        m = JobManager(Settings())
        m.command = lambda mode: [str(tmp_path / "no-such-program")]
        return await m.start("delta")

    snap = asyncio.run(go())
    assert snap["running"] is False and snap["exit_code"] == -1 and snap["finished_at"]
    assert "could not start" in snap["log"][0]


def test_a_huge_line_with_no_newline_and_carriage_return_progress_do_not_hang_the_job():
    code = ("import sys; sys.stdout.write('x' * 300000); sys.stdout.flush(); "
            "sys.stdout.write('\\rprogress 10%\\rprogress 90%\\rdone\\n')")

    async def go():
        m = manager([sys.executable, "-c", code])
        await m.start("delta")
        await m.wait(timeout=20)
        return m.snapshot()

    snap = asyncio.run(go())
    assert snap["exit_code"] == 0 and snap["running"] is False
    assert snap["log"][-1] == "done" and "progress 90%" in snap["log"]
    assert all(len(line) <= 300 for line in snap["log"])


def test_running_state_never_disagrees_with_finished_state():
    async def go():
        m = manager(QUICK)
        await m.start("delta")
        await m.wait()
        first = m.snapshot()
        await m.start("delta")  # a new run replaces the old one entirely
        await m.wait()
        return first, m.snapshot()

    first, second = asyncio.run(go())
    for snap in (first, second):
        assert (snap["running"] is False) == (snap["finished_at"] is not None) and snap["exit_code"] == 0


def make_conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(
        "CREATE TABLE sync_log(id INTEGER PRIMARY KEY, run_at TEXT, kind TEXT, rc_from TEXT, rc_to TEXT, changed INT,"
        " deleted INT, moved INT, errors INT, seconds REAL);"
        "CREATE TABLE state(key TEXT PRIMARY KEY, value TEXT);"
        "CREATE TABLE pages(is_redirect INT, fetched_revid INT, latest_revid INT, indexed_revid INT);"
    )
    return c


def test_index_info_reports_last_delta_last_full_and_pending_work():
    c = make_conn()
    c.executemany(
        "INSERT INTO sync_log(run_at, kind, changed, deleted, moved, errors, seconds) VALUES (?, ?, ?, ?, 0, 0, ?)",
        [("2026-10-20T10:00:00Z", "rev:original", 5, 0, 40.0), ("2026-10-21T10:00:00Z", "rev:original", 12, 1, 55.5),
         ("2026-10-19T04:00:00Z", "full", 300, 7, 610.0)],
    )
    c.execute("INSERT INTO state VALUES ('sync.cursor', '2026-10-21T09:59:00Z'), ('primary_source', 'original')")
    c.executemany("INSERT INTO pages VALUES (?, ?, ?, ?)",
                  [(0, 1, 1, 1), (0, None, 7, None), (0, 2, 2, None), (1, None, None, None)])
    info = index_info(c, Settings())
    assert info["last_delta"]["at"] == "2026-10-21T10:00:00Z" and info["last_delta"]["changed"] == 12
    assert info["last_full"] == {"id": 3, "at": "2026-10-19T04:00:00Z", "changed": 300, "deleted": 7, "errors": 0, "seconds": 610.0}
    assert info["newest_edit_seen"] == "2026-10-21T09:59:00Z" and info["primary_source"] == "original"
    assert info["pending_crawl"] == 1 and info["pending_index"] == 1  # redirects are not counted


def test_index_info_on_an_unbuilt_database_says_never_instead_of_failing():
    info = index_info(sqlite3.connect(":memory:"), Settings())
    assert info["last_delta"] is None and info["last_full"] is None
    assert info["pending_crawl"] == 0 and info["pending_index"] == 0


def test_host_header_parsing():
    assert _host_name("localhost:8765") == "localhost" and _host_name("127.0.0.1:8765") == "127.0.0.1"
    assert _host_name("[::1]:8765") == "::1" and _host_name("evil.example") == "evil.example"


@pytest.fixture
def client(tmp_path):
    s = Settings()
    s.db.path = str(tmp_path / "t.db")
    app = create_app(s)
    app.state.jobs.command = lambda mode: QUICK
    with TestClient(app) as c:
        yield c


def test_starting_requires_the_custom_header_so_other_web_pages_cannot_trigger_work(client):
    r = client.post("/api/index/start", json={"mode": "delta"}, headers={"Host": "localhost:8765"})
    assert r.status_code == 403
    assert client.post("/api/index/stop", headers={"Host": "localhost:8765"}).status_code == 403


def test_a_cross_site_origin_is_refused_even_with_the_header(client):
    r = client.post("/api/index/start", json={"mode": "delta"}, headers={**LOCAL, "Origin": "https://evil.example"})
    assert r.status_code == 403


def test_dns_rebinding_is_refused_because_the_host_name_is_not_ours(client):
    # a hostile page whose name was re-pointed at 127.0.0.1: header and Origin agree, but the Host is not ours
    h = {"X-QQE": "1", "Host": "evil.example:8765", "Origin": "http://evil.example:8765"}
    assert client.post("/api/index/start", json={"mode": "delta"}, headers=h).status_code == 403
    assert client.post("/api/index/stop", headers=h).status_code == 403


def test_start_and_status_through_the_api(client):
    r = client.post("/api/index/start", json={"mode": "delta"}, headers=LOCAL)
    assert r.status_code == 200 and r.json()["label"] == "Delta update"
    info = client.get("/api/index/info").json()
    assert "job" in info and "last_delta" in info and info["pending_crawl"] == 0


def test_an_unknown_mode_is_a_400_and_a_second_start_while_running_is_a_409(tmp_path):
    s = Settings()
    s.db.path = str(tmp_path / "t.db")
    app = create_app(s)
    app.state.jobs.command = lambda mode: SLOW
    with TestClient(app) as c:
        assert c.post("/api/index/start", json={"mode": "full"}, headers=LOCAL).status_code == 200
        assert c.post("/api/index/start", json={"mode": "delta"}, headers=LOCAL).status_code == 409
        stopped = c.post("/api/index/stop", headers=LOCAL).json()
        assert stopped["stopped"] is True and stopped["running"] is False
    app2 = create_app(s)
    with TestClient(app2) as c2:
        assert c2.post("/api/index/start", json={"mode": "bogus"}, headers=LOCAL).status_code == 400


def test_a_server_bound_beyond_loopback_refuses_non_local_clients(tmp_path):
    s = Settings()
    s.db.path = str(tmp_path / "t.db")
    s.web.host = "0.0.0.0"
    app = create_app(s)
    app.state.jobs.command = lambda mode: QUICK
    with TestClient(app) as c:  # the test client's peer is not loopback
        assert c.post("/api/index/start", json={"mode": "delta"}, headers=LOCAL).status_code == 403


def test_closing_the_server_stops_a_running_job(tmp_path):
    s = Settings()
    s.db.path = str(tmp_path / "t.db")
    app = create_app(s)
    app.state.jobs.command = lambda mode: SLOW
    with TestClient(app) as c:
        assert c.post("/api/index/start", json={"mode": "full"}, headers=LOCAL).status_code == 200
        assert app.state.jobs.running
    time.sleep(0.3)
    assert not app.state.jobs.running  # shutting the server down never leaves a job running unwatched


def test_a_runs_page_list_is_kept_and_served_with_wiki_links(tmp_path):
    from epedia.db import connect
    from epedia.jobs import index_changes
    from epedia.wiki.sync import MAX_DETAIL_ROWS, record_pages

    s = Settings()
    s.db.path = str(tmp_path / "t.db")
    conn = connect(s)
    cur = conn.execute("INSERT INTO sync_log(run_at, kind, changed, deleted) VALUES ('2026-10-22T08:00:00Z', 'rev:original', 2, 1)")
    record_pages(conn, cur.lastrowid, [("Broadsword (1)", "edited"), ("New page", "new"), ("Old page", "deleted")])
    old = conn.execute("INSERT INTO sync_log(run_at, kind, changed, deleted) VALUES ('2026-10-01T08:00:00Z', 'rev:original', 5, 0)")

    got = index_changes(conn, s, cur.lastrowid)
    assert got["recorded"] and got["total"] == 3 and got["kind"] == "delta"
    by_title = {p["title"]: p for p in got["pages"]}
    assert by_title["Broadsword (1)"]["url"] == "https://elanthipedia.play.net/Broadsword_(1)"
    assert by_title["New page"]["action"] == "new" and by_title["Old page"]["action"] == "deleted"
    assert index_changes(conn, s, old.lastrowid)["recorded"] is False  # a run from before page lists were kept
    assert index_changes(conn, s, 99999) is None

    record_pages(conn, old.lastrowid, [(f"P{i}", "new") for i in range(MAX_DETAIL_ROWS + 50)])
    capped = index_changes(conn, s, old.lastrowid)
    assert capped["total"] == MAX_DETAIL_ROWS and capped["expected"] == 5  # capped; the UI says "N of expected"
    conn.close()


def test_changes_endpoint_and_run_ids_in_info(tmp_path):
    from epedia.db import connect
    from epedia.wiki.sync import record_pages

    s = Settings()
    s.db.path = str(tmp_path / "t.db")
    conn = connect(s)
    cur = conn.execute("INSERT INTO sync_log(run_at, kind, changed, deleted) VALUES ('2026-10-22T08:00:00Z', 'rev:original', 1, 0)")
    record_pages(conn, cur.lastrowid, [("Hodierna", "edited")])
    conn.close()
    with TestClient(create_app(s)) as c:
        info = c.get("/api/index/info").json()
        assert info["last_delta"]["id"] == cur.lastrowid
        ok = c.get(f"/api/index/changes/{cur.lastrowid}").json()
        assert [p["title"] for p in ok["pages"]] == ["Hodierna"]
        assert c.get("/api/index/changes/424242").status_code == 404


def test_reindex_progress_is_read_from_the_state_table_and_stale_runs_are_called_interrupted():
    import json
    from datetime import datetime, timedelta, timezone

    from epedia.jobs import reindex_status

    def put(c, **kw):
        c.execute("INSERT OR REPLACE INTO state VALUES ('reindex.progress', ?)", (json.dumps(kw),))

    now = datetime.now(timezone.utc)
    iso = lambda dt: dt.strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731
    c = make_conn()
    assert reindex_status(c) is None
    put(c, total=200, done=50, started_at=iso(now), updated_at=iso(now), finished_at=None)
    assert reindex_status(c) == {"state": "running", "done": 50, "total": 200, "pct": 25.0, "started_at": iso(now),
                                 "updated_at": iso(now), "finished_at": None}
    put(c, total=200, done=50, started_at=iso(now), updated_at=iso(now - timedelta(minutes=30)), finished_at=None)
    assert reindex_status(c)["state"] == "interrupted"  # nothing reported for half an hour: it died
    put(c, total=200, done=200, started_at=iso(now), updated_at=iso(now), finished_at=iso(now))
    assert reindex_status(c)["state"] == "finished"
    c.execute("INSERT OR REPLACE INTO state VALUES ('reindex.progress', 'not json')")
    assert reindex_status(c) is None  # garbage is ignored, never a crash


def test_a_reembed_run_leaves_a_finished_heartbeat(tmp_path):
    from epedia.db import connect
    from epedia.index.indexer import index_pages
    from epedia.jobs import reindex_status

    s = Settings()
    s.db.path = str(tmp_path / "t.db")
    conn = connect(s)
    asyncio.run(index_pages(s, conn, None, reembed_all=True, show_progress=False))  # an empty index: nothing to do
    got = reindex_status(conn)
    assert got["state"] == "finished" and got["total"] == 0 and got["pct"] == 100.0
    conn.close()


def test_the_page_refuses_to_start_a_job_while_a_re_index_runs_outside_it(tmp_path):
    import json
    from datetime import datetime, timezone

    from epedia.db import connect

    s = Settings()
    s.db.path = str(tmp_path / "t.db")
    conn = connect(s)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute("INSERT OR REPLACE INTO state VALUES ('reindex.progress', ?)",
                 (json.dumps({"total": 10, "done": 1, "started_at": now, "updated_at": now, "finished_at": None}),))
    conn.close()
    app = create_app(s)
    app.state.jobs.command = lambda mode: QUICK
    with TestClient(app) as c:
        assert c.get("/api/index/info").json()["reindex"]["state"] == "running"
        assert c.post("/api/index/start", json={"mode": "delta"}, headers=LOCAL).status_code == 409


def test_a_re_index_started_outside_the_page_can_be_stopped_from_it(tmp_path, monkeypatch):
    import json
    from datetime import datetime, timezone

    from epedia.db import connect
    from epedia.frontends.web import app as webapp

    s = Settings()
    s.db.path = str(tmp_path / "t.db")
    conn = connect(s)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute("INSERT OR REPLACE INTO state VALUES ('reindex.progress', ?)",
                 (json.dumps({"total": 10, "done": 4, "started_at": now, "updated_at": now, "finished_at": None}),))
    conn.close()
    killed = []
    monkeypatch.setattr(webapp, "find_reindex_pids", lambda: [4242])
    monkeypatch.setattr(webapp, "kill_pid_tree", killed.append)
    with TestClient(create_app(s)) as c:
        assert c.get("/api/index/info").json()["reindex"]["state"] == "running"
        assert c.post("/api/index/stop", headers={"Host": "localhost:8765"}).status_code == 403  # still guarded
        r = c.post("/api/index/stop", headers=LOCAL).json()
        assert r["external_stopped"] == 1 and killed == [4242]
        assert c.get("/api/index/info").json()["reindex"]["state"] == "interrupted"  # shown as stopped at once
        # nothing left running: another stop does nothing
        assert "external_stopped" not in c.post("/api/index/stop", headers=LOCAL).json()


def test_live_progress_counts_only_what_happened_since_the_job_started():
    from epedia.jobs import live_progress

    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE pages (page_id INTEGER PRIMARY KEY, title TEXT, fetched_revid INT, indexed_revid INT, updated_at TEXT)")
    c.execute("CREATE TABLE page_html (page_id INT, fetched_at TEXT)")
    rows = [(1, "Old", 5, 5, "2026-10-28T10:00:00Z"), (2, "New fetched", 7, None, "2026-10-28T12:01:00Z"),
            (3, "New indexed", 8, 8, "2026-10-28T12:02:00Z")]
    c.executemany("INSERT INTO pages VALUES (?,?,?,?,?)", rows)
    c.executemany("INSERT INTO page_html VALUES (?,?)", [(r[0], r[4]) for r in rows])
    # indexed long ago, only touched by another stage during this job: not this job's work
    c.execute("INSERT INTO pages VALUES (4, 'Touched', 3, 3, '2026-10-28T12:03:00Z')")
    c.execute("INSERT INTO page_html VALUES (4, '2026-10-28T09:00:00Z')")
    live = live_progress(c, "2026-10-28T12:00:00Z")
    assert (live["fetched"], live["indexed"]) == (2, 1)
    assert [p["title"] for p in live["latest"]] == ["New indexed", "New fetched"]
    assert live_progress(sqlite3.connect(":memory:"), "2026-10-28T12:00:00Z")["latest"] == []  # no tables yet
