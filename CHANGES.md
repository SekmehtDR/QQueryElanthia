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
- **Pasted text is understood, not mistaken for a wiki page.** A question with a Discord post or in-game message
  pasted along with it (before or after the question) is split into the question and the pasted material: the
  planner uses the material to name the people, places and events being asked about, the answer is told it is
  not a source and must not be cited or confused with a wiki page, and a later "what I sent you" or "that
  message" brings the paste back. Closing offers the model writes itself are removed (the program adds one),
  and old offers are no longer fed back to the model, which made it repeat them.
- **Closing offers are about the conversation.** They used to favour the most-linked pages, so a Dragon Priests
  chat got "want to hear about the Crossing?". Candidates are now ranked by how close they are to the question
  (and dropped when nothing fits), so the offers are things like the Black Spire or Suulnir.
- **"What is coming up next?" is answered by code, not guesswork.** For upcoming/next/current event questions the
  retrieved pages are sorted into still-ahead, over, and undated; rows of an event list whose dates are over are removed
  before the model sees them, and if everything found is over the reply is written in code ("I can't find anything
  scheduled after today; the most recent is X, ... already over"). The date reader understands the wiki's styles
  ("October 26, 2018", "Aug 29, 2024", "March 17th, 2018", "2026-11-02", "20270102") and ignores in-game dates.
- **Fennick knows what day and time it is.** "What time is it?" / "what's today's date?" are answered straight from the
  computer's clock (date, time and zone, with a pointer to the in-game TIME command), without searching; every other
  answer is told the date and time too.
- **Fennick knows what day it is.** "What festival is coming up next?" used to be answered from whichever dated page
  ranked first (a 2025 festival, offered as upcoming). The planner now gets the current year and adds it to
  questions about what is upcoming, next, current or latest, and each passage that contains real-world dates is
  tagged in code as over, upcoming, or mixed (`core/dates.py`), so the model does not do date arithmetic. The answer
  step is told today's date, to name the most recent event when everything listed is over, and to treat in-game years
  ("Festival 453", "457 AV") as not real dates.
- **Bulk from the mirror, deltas from Elanthipedia.** `[wiki] delta_source` names the source the delta update
  asks what changed (the real wiki, never behind), while crawls and full refreshes read the fast mirror. A mirror
  full refresh compares edit times against when each page was fetched (the same on every copy of a wiki, unlike
  revision ids, so mixing sources cannot cause re-fetch loops or overwrite a newer page with an older one), never
  deletes a page just because a lagging mirror lacks it, and winds the delta cursor back to just before the mirror's
  newest edit so the next delta picks up everything the mirror had not yet seen. A single-source setup still uses
  the revision-id guard.
- **Mirror first, Elanthipedia as fallback.** `[wiki] fallback_source` names a second source: the active one (a
  fast mirror) gets one quick retry, and when it is unreachable, overloaded, or does not have a page yet (a lagging
  mirror would otherwise make every new page look deleted) the request goes to Elanthipedia, which keeps its own
  rate limit. A mirror that fails three times in a row is skipped for five minutes. Set per machine in the
  git-ignored `epedia.local.toml`; links shown to people always point at elanthipedia.play.net.
- **Clear index** (`qqueryelanthia clear-index`, or the Index page: typed confirmation, refused while a job runs)
  empties the search index (chunks, vectors, keyword and title indexes) and marks every page unindexed, keeping the
  downloaded pages, item stats and conversations. Rebuild with Re-index (`index --reembed-all`): local work, no
  wiki traffic. The panel says so when the index is empty.
- **Full refresh explains itself.** When pages are read from a different source than the index was built from, Start
  full is disabled with the reason (it compares revision numbers, which only match within one wiki), the command
  fails with one readable line instead of a traceback, and the empty-index hint says Re-index (not Full refresh) is
  how a cleared index is rebuilt.
- **Re-index in the Index panel.** A whole-index re-chunk (`index --reembed-all`) now reports its progress, shown
  as a "Re-index" row (pages done, percent, when it started, or "stopped early" if it died), whether it was started
  from the command line or the new **Re-index** button. The start buttons are disabled while one runs, and the
  server refuses to start a second job on top of it. **Stop** also ends a re-index that was started from a terminal
  (found by its command line, then its whole process tree is stopped); it is shown as stopped at once.
- **Index quality fixes (a re-index applies them to existing pages):** "Incomplete Item" / "Outdated Item"
  banners (boilerplate in about 40,000 item chunks) are stripped and noted in one line like article banners;
  headerless two-column tables no longer get an invented "Col 1 | Col 2" header (about 5,000 chunks);
  `Post:` pages whose whole article sat in one table cell (one chunk was 6,000 tokens, more than the embedder
  reads) are split into normal-sized chunks; `[[category:...]]` typed into page text no longer leaks into chunks.
- **A more human voice.** Fennick no longer says "the chronicle" or "the passages": he is told to talk like a
  friendly, well-read person (contractions, varied openings), to name Elanthipedia or the page only when it
  matters, and the program-written lines (greeting, "couldn't find anything on that", closing offers) were
  reworded to match.
- **Conversations that stay on the rails.** Every page load starts a fresh chat (reloading no longer leaves
  Fennick remembering a conversation the screen has lost); earlier chats are listed under **Past chats**, where
  they can be resumed or deleted, and **New chat** closes the current one (it stays on disk). A **Talking about**
  chip shows what Fennick thinks the chat is about, and its x sets that topic aside without clearing the screen.
  Behind it: the last turns always roll into the next answer (as with a person, what was just said is shared
  context; a different subject is answered on its own), the planner gets a list of earlier questions as well as
  the last turns, pronoun-led messages ("what about them?") count as follow-ups, a question it had to rewrite counts as a follow-up, pages already cited or
  offered are not offered again, no re-greeting mid-chat, and a message with no subject at all ("how much does it
  cost?") gets a clarifying question (never twice in a row). New chat or opening a past chat cancels a running
  answer cleanly, each tab is its own conversation, and two requests on one chat are served in order.
  `qqueryelanthia eval --conversations` scores 17 scripted turns (17/17 now).
- **"View details" on each sync line** lists the pages that run touched (new, edited, deleted, re-rendered by a
  template edit) with links to the wiki. Kept per run in the new `sync_pages` table (up to 5,000 pages per run);
  runs from before this feature show only their counts.
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
- CLI output no longer crashes on characters a Windows console cannot print (`show` on a page with IPA text).
- A message only counts as leaning on earlier context ("what about them?") when it is short; advice that merely
  starts like an offer ("If you want to start forging, ...") is no longer stripped from the end of an answer.
- `qqueryelanthia version` no longer creates a database or leaks a connection on a fresh checkout; the version
  badge only reports a git commit for this project's own checkout.
- "This article is <b>incomplete</b>" banners (markup inside the phrase) were skipped by the stripper and left in
  about 1,400 chunks; "Who is <name>?" could be filed as small talk and answered with "nothing on that".
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
- 197 tests; lint clean.

## 0.1.0: first commit (2026-10-22)

Crawl, index, search and cited answers over the whole wiki, a CLI and a local web chat, sync, and the first
evaluation set. Pushed as the initial commit of the repository.

_Last updated: 1.0.0 (2026-10-22)_
