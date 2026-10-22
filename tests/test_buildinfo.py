import sqlite3

from epedia import __version__, buildinfo
from epedia.config import Settings


def fresh_settings(tmp_path):
    s = Settings()
    s.db.path = str(tmp_path / "x.db")
    return s


def test_build_info_on_a_database_with_no_tables_reports_zero_pages_instead_of_crashing(tmp_path):
    buildinfo._index_cache.clear()
    info = buildinfo.build_info(fresh_settings(tmp_path), sqlite3.connect(":memory:"))
    assert info["version"] == __version__ and info["pages"] == 0 and info["last_sync"] is None


def test_index_counts_are_cached_briefly_then_refreshed(tmp_path, monkeypatch):
    buildinfo._index_cache.clear()
    s = fresh_settings(tmp_path)
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        "CREATE TABLE pages(is_redirect INT); CREATE TABLE state(key TEXT, value TEXT);"
        "CREATE TABLE page_html(fetched_at TEXT); INSERT INTO pages VALUES (0), (0), (1);"
    )
    assert buildinfo.build_info(s, conn)["pages"] == 2
    conn.execute("INSERT INTO pages VALUES (0)")
    assert buildinfo.build_info(s, conn)["pages"] == 2  # within the TTL: served from the cache
    now = buildinfo.time.monotonic()
    monkeypatch.setattr(buildinfo.time, "monotonic", lambda: now + buildinfo.TTL_SECONDS + 1)
    assert buildinfo.build_info(s, conn)["pages"] == 3  # after the TTL: re-read


def test_commit_is_only_reported_for_this_projects_own_checkout(tmp_path):
    buildinfo._commit_cache.clear()
    # an unrelated directory (e.g. site-packages inside someone else's repository) is never reported
    (tmp_path / ".git").mkdir()
    assert buildinfo.git_commit(tmp_path) is None
    assert not buildinfo._is_project_checkout(tmp_path)


def test_the_real_checkout_is_recognised():
    assert buildinfo._is_project_checkout(buildinfo.REPO_ROOT)
