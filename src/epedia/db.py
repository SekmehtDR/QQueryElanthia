"""SQLite storage: schema, connection setup (WAL, FTS5, sqlite-vec), state helpers."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import sqlite_vec

from .config import Settings

SCHEMA_VERSION = 5

SCHEMA = """
CREATE TABLE IF NOT EXISTS pages (
  page_id       INTEGER PRIMARY KEY,
  ns            INTEGER NOT NULL,
  title         TEXT NOT NULL UNIQUE,
  title_norm    TEXT NOT NULL,
  display_title TEXT,
  latest_revid  INTEGER,
  fetched_revid INTEGER,
  indexed_revid INTEGER,
  smw_revid     INTEGER,
  length        INTEGER,
  touched       TEXT,
  is_redirect   INTEGER NOT NULL DEFAULT 0,
  is_disambig   INTEGER NOT NULL DEFAULT 0,
  categories    TEXT,
  error_count   INTEGER NOT NULL DEFAULT 0,
  last_error    TEXT,
  updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_pages_ns ON pages(ns);
CREATE INDEX IF NOT EXISTS ix_pages_norm ON pages(title_norm);
CREATE INDEX IF NOT EXISTS ix_pages_fetch ON pages(ns, page_id)
  WHERE is_redirect=0 AND fetched_revid IS NOT latest_revid;
CREATE INDEX IF NOT EXISTS ix_pages_index ON pages(ns, page_id)
  WHERE is_redirect=0 AND indexed_revid IS NOT fetched_revid;

CREATE TABLE IF NOT EXISTS page_html (
  page_id    INTEGER PRIMARY KEY REFERENCES pages(page_id) ON DELETE CASCADE,
  revid      INTEGER NOT NULL,
  html       BLOB NOT NULL,
  sections   TEXT NOT NULL,
  fetched_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_html_fetched ON page_html(fetched_at);

CREATE TABLE IF NOT EXISTS redirects (
  from_title  TEXT PRIMARY KEY,
  from_norm   TEXT NOT NULL,
  to_title    TEXT NOT NULL,
  to_page_id  INTEGER,
  to_fragment TEXT
);
CREATE INDEX IF NOT EXISTS ix_redirects_norm ON redirects(from_norm);

CREATE TABLE IF NOT EXISTS chunks (
  chunk_id     INTEGER PRIMARY KEY AUTOINCREMENT,
  page_id      INTEGER NOT NULL REFERENCES pages(page_id) ON DELETE CASCADE,
  revid        INTEGER NOT NULL,
  ord          INTEGER NOT NULL,
  kind         TEXT NOT NULL,
  title        TEXT NOT NULL,
  section_path TEXT NOT NULL,
  anchor       TEXT,
  text         TEXT NOT NULL,
  token_est    INTEGER NOT NULL,
  content_hash TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_chunks_page ON chunks(page_id, ord);
CREATE INDEX IF NOT EXISTS ix_chunks_hash ON chunks(content_hash);

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
  title, section_path, text,
  content='chunks', content_rowid='chunk_id',
  tokenize='porter unicode61 remove_diacritics 2'
);
CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
  INSERT INTO chunks_fts(rowid, title, section_path, text)
    VALUES (new.chunk_id, new.title, new.section_path, new.text);
END;
CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
  INSERT INTO chunks_fts(chunks_fts, rowid, title, section_path, text)
    VALUES ('delete', old.chunk_id, old.title, old.section_path, old.text);
END;
CREATE TRIGGER IF NOT EXISTS chunks_au AFTER UPDATE ON chunks BEGIN
  INSERT INTO chunks_fts(chunks_fts, rowid, title, section_path, text)
    VALUES ('delete', old.chunk_id, old.title, old.section_path, old.text);
  INSERT INTO chunks_fts(rowid, title, section_path, text)
    VALUES (new.chunk_id, new.title, new.section_path, new.text);
END;

CREATE VIRTUAL TABLE IF NOT EXISTS titles_fts USING fts5(
  title_norm, page_id UNINDEXED, is_redirect UNINDEXED,
  tokenize='unicode61 remove_diacritics 2'
);

CREATE TABLE IF NOT EXISTS item_properties (
  page_id    INTEGER NOT NULL REFERENCES pages(page_id) ON DELETE CASCADE,
  prop       TEXT NOT NULL,
  value_text TEXT NOT NULL,
  value_num  REAL,
  value_type TEXT,
  PRIMARY KEY (page_id, prop, value_text)
);
CREATE INDEX IF NOT EXISTS ix_itemprops_prop ON item_properties(prop, value_num);

CREATE TABLE IF NOT EXISTS smw_properties (
  prop          TEXT PRIMARY KEY,
  value_type    TEXT,
  page_count    INTEGER,
  sample_values TEXT,
  label         TEXT,
  description   TEXT
);

CREATE TABLE IF NOT EXISTS state (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
  session_id TEXT PRIMARY KEY,
  frontend   TEXT,
  created_at TEXT NOT NULL,
  title      TEXT
);
CREATE TABLE IF NOT EXISTS messages (
  msg_id       INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id   TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
  role         TEXT NOT NULL,
  content      TEXT NOT NULL,
  standalone_q TEXT,
  mode         TEXT,
  sources      TEXT,
  created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_messages_session ON messages(session_id, msg_id);

CREATE TABLE IF NOT EXISTS sync_log (
  id      INTEGER PRIMARY KEY AUTOINCREMENT,
  run_at  TEXT NOT NULL,
  kind    TEXT NOT NULL,
  rc_from TEXT,
  rc_to   TEXT,
  changed INTEGER,
  deleted INTEGER,
  moved   INTEGER,
  errors  INTEGER,
  seconds REAL
);

-- which pages each sync run touched, for the "View details" list in the web page
CREATE TABLE IF NOT EXISTS sync_pages (
  sync_id INTEGER NOT NULL REFERENCES sync_log(id) ON DELETE CASCADE,
  title   TEXT NOT NULL,
  action  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_sync_pages ON sync_pages(sync_id);

-- Edit history (metadata only): who changed which page, when, by how much. Keyed by title like everything else.
CREATE TABLE IF NOT EXISTS revisions (
  revid    INTEGER PRIMARY KEY,
  title    TEXT NOT NULL,
  ts       TEXT NOT NULL,
  user     TEXT,
  comment  TEXT,
  size     INTEGER,
  parentid INTEGER,
  tags     TEXT
);
CREATE INDEX IF NOT EXISTS ix_revisions_title ON revisions(title, ts);

-- Guild/race/gender/status of each stored version of a character page, to see which fact changed and who changed it.
CREATE TABLE IF NOT EXISTS field_values (
  title TEXT NOT NULL,
  revid INTEGER NOT NULL,
  ts    TEXT NOT NULL,
  user  TEXT,
  field TEXT NOT NULL,
  value TEXT,
  PRIMARY KEY (revid, field)
);
CREATE INDEX IF NOT EXISTS ix_fieldvalues_title ON field_values(title, field, ts);
CREATE TABLE IF NOT EXISTS fields_done (
  title      TEXT PRIMARY KEY,
  upto_revid INTEGER,
  fetched_at TEXT
);

-- Typed facts read from tables and infoboxes by index/extract/*, so tools can rank and compare in code instead of
-- asking a small model to read a table. page_id is the page a fact came from (replaced together on a new revision).
CREATE TABLE IF NOT EXISTS facts (
  page_id    INTEGER NOT NULL REFERENCES pages(page_id) ON DELETE CASCADE,
  domain     TEXT NOT NULL,
  entity     TEXT NOT NULL,
  grp        TEXT NOT NULL,
  attribute  TEXT NOT NULL,
  value_num  REAL,
  value_text TEXT,
  section    TEXT,
  PRIMARY KEY (page_id, domain, entity, grp, attribute)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS ix_facts_domain ON facts(domain, grp, attribute);
CREATE INDEX IF NOT EXISTS ix_facts_entity ON facts(entity COLLATE NOCASE);

-- Topic graph derived from categories, subpages and links (index/topics.py): which pages belong to the family of a hub
-- page such as Crafting or Forging, and how strongly. No hand-made links.
CREATE TABLE IF NOT EXISTS topic_hubs (
  hub_page_id INTEGER PRIMARY KEY REFERENCES pages(page_id) ON DELETE CASCADE,
  title       TEXT NOT NULL,
  members     INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS page_topics (
  hub_page_id INTEGER NOT NULL REFERENCES pages(page_id) ON DELETE CASCADE,
  page_id     INTEGER NOT NULL REFERENCES pages(page_id) ON DELETE CASCADE,
  weight      REAL NOT NULL,
  via         TEXT NOT NULL,
  PRIMARY KEY (hub_page_id, page_id)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS ix_page_topics_page ON page_topics(page_id);
"""


def connect(settings: Settings, *, readonly: bool = False) -> sqlite3.Connection:
    path: Path = settings.db_path
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA temp_store=MEMORY")
    if not readonly:
        migrate(conn, settings)
    return conn


def migrate(conn: sqlite3.Connection, settings: Settings) -> None:
    conn.executescript(SCHEMA)
    # vec0 tables cannot use IF NOT EXISTS on every sqlite-vec version; check manually.
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='chunk_vec'"
    ).fetchone()
    if not exists:
        conn.execute(
            f"CREATE VIRTUAL TABLE chunk_vec USING vec0(chunk_id INTEGER PRIMARY KEY, "
            f"embedding FLOAT[{settings.ollama.embed_dim}])"
        )
    _migrate_v2(conn)
    _migrate_v3(conn)
    _migrate_v4(conn)
    _migrate_v5(conn)
    if get_state(conn, "schema_version") != str(SCHEMA_VERSION):
        set_state(conn, "schema_version", str(SCHEMA_VERSION))
    if get_state(conn, "embed_model") is None:
        set_state(conn, "embed_model", settings.ollama.embed_model)
        set_state(conn, "embed_dim", str(settings.ollama.embed_dim))


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    """ADD COLUMN if missing. Two processes (web, sync) may migrate at once, so a lost race is not an error."""
    if any(r[1] == column for r in conn.execute(f"PRAGMA table_info({table})")):
        return
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
    except sqlite3.OperationalError as exc:
        if "duplicate column" not in str(exc).lower():
            raise


def _migrate_v2(conn: sqlite3.Connection) -> None:
    """Page kinds (for kind-aware retrieval) and the internal link graph (for 'related pages')."""
    _ensure_column(conn, "pages", "page_kind", "TEXT")
    _ensure_column(conn, "pages", "is_obsolete", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "pages", "is_log", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "pages", "classified_revid", "INTEGER")
    _ensure_column(conn, "pages", "enriched_revid", "INTEGER")
    _ensure_column(conn, "messages", "kind", "TEXT")
    # conversation tracking: when a chat was last used, what it is about, and where the model's memory of it starts
    _ensure_column(conn, "sessions", "updated_at", "TEXT")
    _ensure_column(conn, "sessions", "topic", "TEXT")
    _ensure_column(conn, "sessions", "context_from", "INTEGER")
    _ensure_column(conn, "messages", "related", "TEXT")
    _ensure_column(conn, "sessions", "owner", "TEXT")  # 'local', or a per-browser id for visitors through a tunnel
    conn.executescript(
        """
        CREATE INDEX IF NOT EXISTS ix_sessions_updated ON sessions(updated_at);
        CREATE INDEX IF NOT EXISTS ix_pages_kind ON pages(page_kind);
        CREATE INDEX IF NOT EXISTS ix_pages_classify ON pages(page_id)
          WHERE is_redirect=0 AND classified_revid IS NOT fetched_revid;
        CREATE TABLE IF NOT EXISTS page_links (
          from_page_id INTEGER NOT NULL REFERENCES pages(page_id) ON DELETE CASCADE,
          to_page_id   INTEGER NOT NULL REFERENCES pages(page_id) ON DELETE CASCADE,
          PRIMARY KEY (from_page_id, to_page_id)
        ) WITHOUT ROWID;
        CREATE INDEX IF NOT EXISTS ix_links_to ON page_links(to_page_id);
        """
    )


def _migrate_v5(conn: sqlite3.Connection) -> None:
    """What a conversation is about right now (a page title), for "how much does it weigh?" several turns later."""
    _ensure_column(conn, "sessions", "anchor", "TEXT")


def _migrate_v4(conn: sqlite3.Connection) -> None:
    """Which revision of a page each extractor has read: two can read one page (a material page's box and what its name
    means), and the second must not be skipped because the first is current. Empty at first, so the next enrich re-reads."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS enriched (
          page_id INTEGER NOT NULL REFERENCES pages(page_id) ON DELETE CASCADE,
          domain  TEXT NOT NULL,
          revid   INTEGER,
          PRIMARY KEY (page_id, domain)
        ) WITHOUT ROWID;
        """
    )


def _migrate_v3(conn: sqlite3.Connection) -> None:
    """One row per answered (or failed) turn: timings, what the planner decided, how well the answer was grounded, and
    the reader's verdict. Written even when the turn fails, so nothing a person asked is lost from the record."""
    _ensure_column(conn, "messages", "cited", "TEXT")  # JSON list of the [n] the answer cites
    _ensure_column(conn, "messages", "turn_id", "INTEGER")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS turns (
          turn_id          INTEGER PRIMARY KEY AUTOINCREMENT,
          session_id       TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
          user_msg_id      INTEGER,
          assistant_msg_id INTEGER,
          started_at       TEXT NOT NULL,
          planner_ms       INTEGER,
          retrieve_ms      INTEGER,
          ttft_ms          INTEGER,
          total_ms         INTEGER,
          plan_json        TEXT,
          grounding_json   TEXT,
          verify_json      TEXT,
          regenerated      INTEGER NOT NULL DEFAULT 0,
          error            TEXT,
          frontend         TEXT,
          owner            TEXT,
          feedback         INTEGER,
          feedback_reason  TEXT,
          feedback_at      TEXT
        );
        CREATE INDEX IF NOT EXISTS ix_turns_session ON turns(session_id, turn_id);
        CREATE INDEX IF NOT EXISTS ix_turns_feedback ON turns(feedback) WHERE feedback IS NOT NULL;
        """
    )


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    # IMMEDIATE takes the write lock up front so busy_timeout applies; a deferred BEGIN that reads then
    # writes fails instantly with "database is locked" when another process committed in between (WAL).
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def get_state(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def set_state(conn: sqlite3.Connection, key: str, value: str | None) -> None:
    if value is None:
        conn.execute("DELETE FROM state WHERE key=?", (key,))
    else:
        conn.execute(
            "INSERT INTO state(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )


def delete_page(conn: sqlite3.Connection, page_id: int) -> None:
    """Remove a page and everything derived from it (vec rows are a virtual table, so explicit)."""
    conn.execute(
        "DELETE FROM chunk_vec WHERE chunk_id IN (SELECT chunk_id FROM chunks WHERE page_id=?)", (page_id,)
    )
    conn.execute("DELETE FROM chunks WHERE page_id=?", (page_id,))
    conn.execute("DELETE FROM page_links WHERE from_page_id=? OR to_page_id=?", (page_id, page_id))
    conn.execute("DELETE FROM item_properties WHERE page_id=?", (page_id,))
    conn.execute("DELETE FROM page_html WHERE page_id=?", (page_id,))
    conn.execute("DELETE FROM pages WHERE page_id=?", (page_id,))


def vec_version(conn: sqlite3.Connection) -> str:
    return conn.execute("SELECT vec_version()").fetchone()[0]
