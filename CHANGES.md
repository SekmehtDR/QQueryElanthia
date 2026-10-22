# Changes

Latest version: **1.0.0** (2026-10-22). The version lives in `src/epedia/__init__.py`; add each release here,
newest first. Format: what a user would notice first, then fixes, then internals.

## 1.0.0: Fennick, a storyteller's guide (2026-10-22)

The first full release: the whole of Elanthipedia, answered by a persona.

### New

- **Fennick**, a Halfling Bard who keeps a satchel of notes copied from Elanthipedia. He frames places, people,
  gods and creatures in his own voice, keeps game mechanics plain and marked out of character, relays the wiki's
  own hedges (outdated, obsolete, incomplete, era, GM post versus player guide), and never claims experience of
  his own. Switch him off with `[persona] enabled = false`.
- **Portrait in the corner of every answer.** He sways while thinking and his mouth moves in time with the words
  as they arrive (two hand-drawn frames, `tools/make_fennick.py`).
- **"Fennick is thinking..." with animated dots** instead of an empty box, changing to "is leafing through his
  notes" once the search is done.
- **Version badge** next to his name (`v1.0.0 · <commit>`), with a tooltip listing the models, page count and last
  update. Read live from the server; `qqueryelanthia version` prints the same.
- **Index panel with controls** behind an "Index" button in the header: **Delta update** (only what changed) and
  **Full refresh** (re-walk and compare every page; asks to confirm), **Stop**, a live status line with elapsed
  time and a log tail, plus last delta, last full refresh, newest wiki edit seen, source and pending work. The
  panel is opened on demand and polls only while open or while a job runs. Jobs run as a child
  `qqueryelanthia sync` process, one at a time, and are stopped when the server shuts down.
- **Desktop and Mobile layouts.** A full-window desktop layout (default) and a phone-width one, switched from
  the header and remembered; phones choose mobile automatically.
- **Live formatting** of the answer while it streams (headings, lists, tables, bold, code) and **source badges**:
  solid `[n]` for the model's citations, dashed `[n*]` for lines matched to a passage automatically, with hover
  tooltips; "Sources used" is folded and lists only what the answer cites.
- **UI polish.** Auto-growing input, follow-the-stream scrolling with "Jump to latest", Copy answer, Try again on
  errors, one answer at a time, reduced-motion handling.
- **Kind-aware retrieval.** The planner labels each question (item, crafting, place, character, lore, creature,
  mechanics, event/news, meta) and search favours matching kinds of wiki page; role-play logs and obsolete pages
  are demoted. "How do I forge weapons" now reaches the Forging guide instead of stray item pages.
- **Page classification and a link graph** (`classify`): 28 page kinds from namespaces, categories and title
  patterns, and ~305k links used to suggest one related page after an answer.
- **Greetings and questions about the assistant** are answered in character without searching.
- **Structured item questions** ("heaviest heavy edged weapon") run a live Semantic MediaWiki query, with a local
  fallback when the wiki is unreachable.
- **Follow-up questions** are rewritten to name their subject and answered from the previous answer's pages.
- **Wiki sources.** The original wiki is the default and primary source; extra mirrors are configured in a
  git-ignored `epedia.local.toml` and chosen per command with `--source`.
- **Command renamed to `qqueryelanthia`** (`epedia` still works as an alias).
- **Evaluation set** of 38 questions with kinds; `eval` reports hit@k, MRR and planner accuracy; `eval --answers`
  prints a voice checklist for reviewing answers by hand.
- `ARCHITECTURE.md`, `CLAUDE.md` and this file.

### Fixed

- A long section's short closing paragraph could be silently dropped by the chunker and never searched.
- `EPEDIA_WIKI__RPS` (and per-source overrides) were ignored.
- `pipeline` skipped classification, leaving a fresh build without page kinds.
- `watch` leaked a log handler and a database connection on every loop.
- `eval --answers` polluted real chat history and was affected by it on later runs.
- Ctrl-C during a crawl waited out a whole batch and left its handler installed.
- Title lookups scanned the whole title table several times per question (now 1 to 3 ms).
- Stale redirect targets could break title matching after a page was deleted and recreated.
- The items tool could hang for two minutes when the wiki was unreachable, and compared numeric filters sent as
  text lexicographically.
- The User-Agent default would have sent a literal `{version}`.
- A closing "offer" written by the model sometimes named pages that did not exist; it is now added by code from
  real related pages (and never maps, scripts, logs or obsolete pages).
- The model sometimes appended "the chronicle has nothing on that" to a real answer (now softened in code).
- Source tagging never puts a badge on a line whose numbers differ from the passage's, and keeps citations written
  after a period with their sentence.
- `qqueryelanthia version` no longer creates a database or leaks a connection on a fresh checkout; the version
  badge only reports a git commit for this project's own checkout.
- The page handles failed requests, malformed stream events and dropped connections instead of freezing.
- Index controls are local-only: they need the `X-QQE` header, a Host of this machine (blocks DNS rebinding), a
  matching Origin, and a loopback client if the server is bound beyond loopback. Two clicks cannot start two jobs,
  Stop ends the whole process tree, and a job that cannot start is reported as failed.

### Internals

- Pages are identified by title, so the original wiki and a mirror can feed the same database; whole-wiki
  operations refuse to run against a non-primary source, and `adopt` keeps already-fetched pages whose stored
  copy is provably current.
- Change detection uses `allrevisions` and `logevents` rather than recent changes.
- Maintenance banners are stripped at index time and noted in one line; "Redirect to:" stubs are dropped;
  creature infoboxes and one-column tables come out as `Key: value` lines.
- 133 tests; lint clean.

## 0.1.0: first commit (2026-10-22)

Crawl, index, search and cited answers over the whole wiki, a CLI and a local web chat, sync, and the first
evaluation set. Pushed as the initial commit of the repository.

_Last updated: 1.0.0 (2026-10-22)_
