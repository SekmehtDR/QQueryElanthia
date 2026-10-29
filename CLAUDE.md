# CLAUDE.md

Notes for Claude (and any other contributor) working in this repository. Keep this file current: it is the
first thing read at the start of a session.

## Working agreements (read these first)

1. **Never commit or push without the owner's explicit verbal approval, every time.** Approval for one commit or
   push does not carry over to the next. Do the work, run the tests, show what changed, then ask. Staging files
   or running `git init`/`git status`/`git diff` is fine; `git commit` and `git push` wait for a clear "yes".
2. **"Please update my documentation" means refresh these files together:** `ARCHITECTURE.md` (diagrams and the
   module map, when the architecture changed), `CHANGES.md` (add what is new, under the current version), this
   `CLAUDE.md` (commands, gotchas, conventions), and `README.md` if user-facing behaviour changed. Update
   "Last updated" lines and the version in `CHANGES.md` to match `src/epedia/__init__.py`. Updating docs does not
   include committing them (rule 1 still applies).
3. **Tell the owner the truth about results.** If tests fail, a step was skipped or something is unverified, say
   so plainly. Do not claim a UI change works unless it was actually exercised.
4. **Ask before anything outward-facing or hard to reverse:** pushing, registering Windows scheduled tasks,
   hitting the wiki harder than the configured rate limits, deleting data or backups.

## What this project is

**QQueryElanthia** (the name is a joke: QQ is how the internet writes crying) is a local assistant that answers
questions about the text MUD DragonRealms from **Elanthipedia**, the player-run wiki. It speaks as **Fennick**, a
Halfling Bard who keeps a satchel of notes copied from the wiki: curious and warm, strict about facts. Everything
runs locally: SQLite (FTS5 + sqlite-vec) for search, Ollama for the models (`qwen3:8b` chat and planner,
`nomic-embed-text` embeddings). Python package: `epedia` (internal name); commands: `qqueryelanthia`, alias
`epedia`. See `ARCHITECTURE.md` for how it fits together and `README.md` for user-facing usage.

## Commands you will use

```powershell
.\.venv\Scripts\python.exe -m pytest -q                # tests (all should pass)
.\.venv\Scripts\ruff.exe check src tests tools --fix   # lint (E, F, I; E501 ignored)
.\.venv\Scripts\qqueryelanthia.exe serve               # web chat at http://127.0.0.1:8765
.\.venv\Scripts\qqueryelanthia.exe ask "..." [--kind crafting]
.\.venv\Scripts\qqueryelanthia.exe eval                # retrieval quality (hit@k, MRR, kind/mode accuracy)
.\.venv\Scripts\qqueryelanthia.exe status | monitor    # index progress
.\.venv\Scripts\qqueryelanthia.exe show "Page title"   # how a page was converted/chunked/classified
.\.venv\Scripts\qqueryelanthia.exe search "..." --explain --kind place
.\.venv\Scripts\qqueryelanthia.exe sync [--full-scan | --dry-run]
.\.venv\Scripts\python.exe tools\make_fennick.py       # regenerate the two portrait PNGs
```

Full build order: `pipeline` (discover, crawl, classify, smw, index, titles). After changing chunking or text
conversion, run `index --reembed-all` (unchanged chunks reuse vectors by content hash). After changing
classification rules, run `classify --all`.

## Layout

```
src/epedia/   wiki/ (client, discover, crawl, smw, sync)   text/ (html2md, chunker, stats)
              index/ (indexer, classify)   retrieval/ (search, titles)   llm/ (ollama, prompts)
              core/ (assistant, memory, items_tool)   frontends/web/ (app.py, static/)   evalx/
              kinds.py  config.py  db.py  buildinfo.py  progress.py  cli.py
tests/        pytest suite; tests/fixtures/*.json are saved action=parse outputs
eval/         questions.yaml (the evaluation set; titles verified against the index)
tools/        make_fennick.py (draws the portrait with Pillow)
epedia.toml   committed config;  epedia.local.toml  git-ignored, machine-local overrides
data/         wiki.db and logs (git-ignored)
```

## Conventions

- **One version, one place:** `src/epedia/__init__.py` (`__version__`). pyproject, CLI, web badge and the
  User-Agent all read it. Bump it there and add an entry to `CHANGES.md`.
- **Pages are identified by title**, never by the wiki's page id (ids and revision numbers differ between the
  original wiki and any mirror). `pages.page_id` is purely internal.
- **Every pipeline stage is idempotent and keyed by revision:** `latest_revid -> fetched_revid -> indexed_revid ->
  smw_revid -> classified_revid`. A stage processes rows where its own revision lags.
- **Citations and answers:** facts come only from retrieved passages, cited `[n]`. The closing "related page"
  offer is appended by code (`prompts.offer_line`), never written by the model.
- **SQL touching the database from more than one process** uses `transaction()` (BEGIN IMMEDIATE); schema
  changes go through `_ensure_column` in `db._migrate_*` so web and sync can migrate concurrently.
- Match the surrounding code's style: type hints, short docstrings that say *why*, comments only where the
  reason is not obvious.

## Gotchas learned the hard way

- **Never match processes by the word "index"** (the folder is called `epediaindexer`). Stop the web server with
  an exact match on `epedia.exe serve` / `qqueryelanthia.exe serve`.
- **Never commit `epedia.local.toml`, `data/`, `.claude/` or any wiki mirror URL.** A friend's mirror is kept
  only in `epedia.local.toml`; its API responses are uncached (every request renders on their server).
- **The generic Item namespace (118, ~66k pages) is excluded from structured-stat extraction** on purpose (one
  request per page). `epedia smw --ns 118` opts in.
- **The 8B model follows factual instructions far better than style ones.** Keep style directives next to the
  passages (`prompts.FRAMING`, `SHAPE_HINTS`) and anything that must be exact (offers, citations) in code.
- **Pydantic skips validators on defaults:** use `validate_default=True` where a default needs processing
  (see `WikiCfg.user_agent`).
- **Pillow's `getbbox()` on RGBA only looks at alpha.** Compare `.convert("RGB")` when diffing images.
- **`pip install -e .` fails to overwrite `epedia.exe` while the web server is running** on Windows: stop it first.
- The wiki's recent-changes list is unreliable on a mirror; sync uses `allrevisions` and `logevents`.
- **Index jobs are child processes.** On Windows the venv `python.exe` is a launcher, so stopping a job must kill
  the whole tree (`taskkill /T /F`); never rely on `proc.terminate()`. Global CLI options (`--config`, `--source`)
  go before the subcommand. Test jobs by replacing `JobManager.command`, never by running a real sync.
- **Web guards need a Host header in tests:** `TestClient` sends `Host: testserver`, which `guard()` refuses; pass
  `Host: localhost:8765` and `X-QQE: 1`.
- **Sources:** `wiki.source` is where pages are read, `wiki.fallback_source` (via `wiki.client.open_client`) is tried
  when it fails or lacks a page; always construct clients with `open_client`, never `WikiClient` directly. Citation
  URLs come from `wiki.base_url` (Elanthipedia), never from the source that was read. `wiki.delta_source` is
  what `sync` (deltas) reads, and a delta fetches only the pages it found changed (`crawl(titles=...)`); the backlog is
  `fill`'s, read from the mirror. With a mirror setup (delta_source or fallback_source set) full scans compare edit
  times, not revision ids, and `check_primary_source` stands down; with a single source it still protects revision ids.
- **Who is asking** (`frontends/web/access.py`): a request is local only if it comes from loopback AND has none of the
  tunnel's forwarding headers (`cf-connecting-ip`, `x-forwarded-for`, ...); a tunnel always connects from loopback, so
  the peer address proves nothing. Anything admin (Index, jobs, status, clear) must call `guard()`/`local_only()`; chats
  are scoped by `sessions.owner` (`local` or a per-browser cookie id). Never bind `web.host` beyond 127.0.0.1 to share.
- **Edit history** (`wiki/history.py`, `wiki/timeline.py`): `revisions` holds metadata only; `field_values` holds the
  infobox facts of old character versions. The trust rules live in `timeline.judge` and are the thing to change if
  Fennick trusts too much or too little. `epedia history` is the one bulk read (about 1,400 + ~3,900 requests to the
  real wiki, resumable): ask the owner before running it. Never fetch history while answering a question.
- **Typed facts** (`index/extract/`, `index/enrich.py`, table `facts`): structured data is read from indexed chunks by
  small registered extractors, never by asking the model. The chunker folds tiny sections into a neighbour, so an
  extractor must also read the heading line above a table (`extract.tables`). Answers that compare things (materials,
  the hunting ladder) are written by code from facts; the rules they apply name the page that states them and
  `epedia doctor` checks the wording is still there. `epedia doctor` should stay clean after any index change.
- **Names and kinds** (`retrieval/resolve.py`, `kinds.py`): the kind rules live in one declarative table; extend
  `INTENT_WORDS`/`KIND_GROUP` there rather than adding word lists elsewhere. Resolution is skipped for stop words and the
  game's own name (`titles.STOP_NAMES`); `Title:` and `Post:` pages are never names.
- **Edit tool traps:** this harness corrupts a backslash-b or backslash-n typed inside Python heredoc strings (backspace,
  literal newline). After scripted edits scan for control characters; build such regexes with `chr(92)` or use the Edit tool.
- Pass large JSON to child processes on stdin (Windows command lines are limited to ~32k characters).
- A non-breaking space does not glue text to an inline badge; wrap the pair in a `nowrap` span (`render.js`).
- Check the page's script with `node --check` after editing `static/index.html` (a duplicate `const` once broke
  the whole page); `tests/test_web.py` does this.

## Definition of done for a change

Tests pass, lint is clean, new behaviour has a test, the UI change was actually loaded or its script
syntax-checked, and `CHANGES.md` has an entry. Then ask before committing.

_Last updated: 1.0.0 (2026-10-22)_
