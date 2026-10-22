# Architecture

An overview of how QQueryElanthia fits together. It is meant to be read top to bottom and refreshed whenever
the architecture changes (ask: "please update my documentation"). For usage see `README.md`; for what changed
and when see `CHANGES.md`.

## In one paragraph

A one-off **build** turns the player-run wiki Elanthipedia into a local search index: pages are downloaded as
rendered HTML, converted to clean text, cut into section-sized chunks, labelled by kind, and stored in one SQLite
file with both keyword (FTS5) and meaning (vector) indexes. At question time a **planner** model labels and
rewrites the question, **hybrid search** finds the best chunks (favouring the right kind of page), and a local
language model writes a cited answer in the voice of **Fennick**. A periodic **sync** keeps the index current.

## The system at a glance

```
                          ┌────────────────────────────  YOU  ────────────────────────────┐
                          │   web chat (browser)        CLI: ask / chat          Discord* │   *planned
                          └───────┬───────────────────────────┬────────────────────────────┘
                                  │ SSE stream                │ async events
                          ┌───────▼───────────────────────────▼───────┐
                          │          core.Assistant  (Fennick)        │
                          │   plan → retrieve → answer → persist      │
                          └──┬──────────┬─────────────┬───────────────┘
                 planner /   │          │ hybrid      │ structured item
                 answer LLM  │          │ search      │ questions
                  ┌──────────▼───┐  ┌───▼─────────┐ ┌─▼───────────────┐
                  │   Ollama     │  │ SQLite file │ │ Semantic MW ask │
                  │ qwen3:8b     │  │ data/wiki.db│ │ (live; falls    │
                  │ nomic-embed  │  │ FTS5 + vec0 │ │  back to local) │
                  └──────────────┘  └───▲─────────┘ └─────────────────┘
                                        │ written by
        ┌───────────────────────────────┴──────────────────────────────────────────┐
        │ BUILD / SYNC   discover → crawl → classify → smw → index → titles        │
        └───────────────▲──────────────────────────────────────────────────────────┘
                        │ MediaWiki API (rate-limited, polite)
              ┌─────────┴──────────┐        ┌──────────────────────────┐
              │ elanthipedia.play  │        │ optional mirror          │
              │ .net  (primary)    │        │ (git-ignored local conf) │
              └────────────────────┘        └──────────────────────────┘
```

## Build pipeline

Each stage is **idempotent** and resumable. Pages carry one revision marker per stage, and a stage processes
only the rows where its own marker lags the one before it:

```
 wiki            fetched            classified            indexed              item stats
 latest_revid ─► fetched_revid ───► classified_revid ───► indexed_revid ─────► smw_revid
    ▲                │                    │                    │                   │
 discover         crawl               classify              index                smw
 (allpages +     (action=parse →     (kind, flags,         (chunk → FTS5        (browsebysubject →
  prop=info)      HTML, zlib)         link graph)           + vectors)           stats chunk)
```

What each stage does:

| stage      | input → output |
|------------|----------------|
| `discover` | `allpages` per namespace → `pages`, `redirects`; remembers where it started so sync misses nothing |
| `crawl`    | `action=parse` (templates expanded) → compressed HTML in `page_html`; 10 requests in flight, rate-limited |
| `classify` | namespace + categories + title patterns → `page_kind`, `is_log`, `is_obsolete`; links → `page_links`; soft redirects → `redirects` |
| `smw`      | Semantic MediaWiki properties (weight, damage, cost…) → `item_properties` and one `stats` chunk per item page |
| `index`    | HTML → text blocks → ~450-token chunks → FTS5 rows + 768-dim vectors (unchanged text reuses its vector by hash) |
| `titles`   | page and redirect titles → `titles_fts`, so "choke" finds "Choke command" |

### From HTML to chunks

```
 rendered HTML ──► html2md ──► blocks ──────────────► chunker ──────────► chunks
  (one page)       • drop nav, edit links, banners      • section path      "Barbarian > Berserks"
                   • tables → "Key: value" lines          from headings      ~450 tokens, never splits
                     or Markdown grids                  • merge tiny           a table row; header
                   • one-column table → lines             sections             repeated on each piece
                   • note banners: (Marked incomplete)  • split long ones
                   • "Redirect to: X" stub → dropped      with overlap
```

## One question's journey

```
 "which one is lighter?"            (turn 2 of a conversation about armor)
        │
        ▼
 ┌─ PLAN (qwen3:8b, JSON schema, temperature 0) ─────────────────────────────────────────┐
 │  standalone_question  "Which version of the aged leather combat armor … is lighter?"  │
 │  follow_up = true     kind = item        mode = search | items (+ items_query)        │
 └───────────────────────────────────────────────────────────────────────────────────────┘
        │ kind = meta ─────────► greeting / "who are you": answer from a truthful blurb,
        │                         no search at all
        ▼
 ┌─ RETRIEVE ────────────────────────────────────────────────────────────────────────────┐
 │  follow-up?  ► start with the previous answer's own passages                          │
 │  items mode? ► live Semantic MediaWiki query → a table passage (local SQL if offline) │
 │  hybrid search (below) → top chunks, max 3 per page, plus runner-ups for "related"    │
 └───────────────────────────────────────────────────────────────────────────────────────┘
        ▼
 ┌─ ANSWER (qwen3:8b, streamed) ─────────────────────────────────────────────────────────┐
 │  system = persona + kind-specific shape hint + numbered passages                      │
 │  history = last 4 turns (each capped)         user = the standalone question          │
 └───────────────────────────────────────────────────────────────────────────────────────┘
        ▼
 text tokens ──► soften_refusal ──► attribute ──► one closing offer appended BY CODE
 (the draft,     a tacked-on        lines the model    (a popular page linked from the top page,
  streamed)      "nothing on that"  left uncited get    never a map, script, log or obsolete page)
                 becomes an honest  a [n*] tag where
                 caveat             one passage clearly matches
        ▼
 `done` event carries the FINAL text (draft + fixes) → the page repaints it; the same text is saved
 persisted: messages(role, content, kind, mode, sources) → next turn's "previous answer"
```

### Source tagging: showing where each line came from

A small local model cites unreliably, so the program checks its work instead of trusting it:

```
 answer line ─► already has [n]? ──yes──► leave it (solid badge in the page)
                    │ no
                    ▼
        weighted word overlap with each numbered passage
        (numbers count double, rare words count more, plural forms fold together)
                    │
      best passage covers ≥ 50% of the line, beats the runner-up by a clear margin
      (or matches almost entirely, ties going to the higher-ranked passage)?
            yes │                                  │ no
                ▼                                  ▼
       append [n*]  (dashed badge,         leave the line alone: framing and
       tooltip says "matched               unsupported lines are never guessed at
       automatically")
```

## Hybrid search and the scoring formula

```
 question ──┬─► keyword search  (FTS5 BM25, AND then OR)      top 40 ─┐
            ├─► meaning search  (sqlite-vec nearest chunks)   top 40 ─┼─► rank fusion (RRF, k=60)
            └─► title match     exact: "stealth" → Stealth skill      │      rrf = Σ 1/(60+rank)
                                partial: all core title words in query │
                                                                       ▼
   score = rrf  ×  namespace  ×  disambiguation  ×  KIND(question, page)  ×  log  ×  obsolete
                   (Post .8)      (.8)              (0.6 … 1.4 matrix)      (.5)    (.75)
         + exact-title boost (.05)  or  partial-title boost (≤ .02)         ◄── added AFTER the
         + continuity boost (.012) for chunks the previous answer used          multipliers, so a page the
                                                                                user named always wins
```

### Question kinds steer page kinds

Ten question kinds (item, crafting, place, character, lore, creature, mechanics, event_news, meta, general) and
28 page kinds meet in a weight matrix (`kinds.py`, overridable in `epedia.toml`). A small slice:

```
                       guide  crafting  shop  place   npc   pc    lore  creature
  question: crafting    1.3     1.4     1.1   0.9    0.9   0.6   0.7    0.7
  question: place       0.9     0.9     1.2   1.4    1.1   0.6   0.9    1.0
  question: character   0.8     0.7     1.0   0.9    1.4   1.3   1.0    0.7
  question: lore        0.8     0.7     0.8   1.0    1.0   0.7   1.4    0.9
```

Page kinds come from `classify`: namespace first (Post, Category, Weapon/Armor/Shield/Item/Title), then
categories (`Player Characters` → pc, `Shops` → shop, `Bestiary` → creature…), then title patterns
(`… guide`, `… skill`, `Name/Logs/…`). Role-play logs and obsolete pages are demoted globally.

## Data model

One SQLite file (WAL mode), `data/wiki.db`:

```
 pages ─────────────┬──< page_html     (zlib HTML, section list)           identity = title (unique)
  page_id (internal)│
  title, ns         ├──< chunks ──┬── chunks_fts   (FTS5, porter stemming)
  page_kind, flags  │  kind: section│─ chunk_vec    (vec0, 768 floats)
  categories (JSON) │       | stats │
  *_revid markers   │
                    ├──< item_properties   (Semantic MediaWiki: prop, text, number)
                    └──< page_links ──►    (from → to; related-page suggestions)

 redirects (from_title → to_page_id)   titles_fts (page + redirect titles)   smw_properties (catalog)
 sessions ──< messages (role, content, standalone_q, mode, kind, sources JSON)
 state (key/value: cursors, primary_source, schema_version)   sync_log
```

## Wiki sources

```
   config:  [wiki] source = "original"            epedia.local.toml (git-ignored): extra sources
            [wiki.sources.original] api_url, rps, concurrency
                    │
   epedia --source <name> <cmd>   picks one for a single command
                    │
   pages are keyed by TITLE (ids and revision numbers differ between wikis), so either source can feed sync.
   Whole-wiki operations (discover, full scan) must use the PRIMARY source; `discover --force` switches it,
   and `adopt` keeps already-fetched pages whose stored copy is provably current (compares edit timestamps).
   Citations always link to the original site.
```

## Keeping it current

```
  every hour (Task Scheduler or `watch`)                     weekly
  ───────────────────────────────────────                    ──────
  allrevisions since cursor ──► edited / new pages           full scan: re-walk allpages, compare touched/
  logevents (delete, move)  ──► removed / renamed pages       lastrevid, drop vanished pages
  edited Template/Module    ──► embeddedin → pages that re-render without a new revision
          │
          ▼
  reconcile (prop=info) → crawl → classify → smw → index → titles          cursor advances only to
                                                                           what the source really has
```

## The web interface

Everything on the page lives in one `#app` container, so the header, chat and input share a width and gutter:

```
 DESKTOP (default): fills the window up to a wide column         MOBILE: a phone-width column
┌─────────────────────────────────────────────────────────────┐    ┌─────────────────────────┐
│ Fennick [v1.0.0] a traveler's…   [D|M] [Index] [New chat]   │    │ Fennick [v1.0.0]        │
├─────────────────────────────────────────────────────────────┤    │ [D|M] [Index] [New]     │
│ (folded until [Index]: pages crawled / searchable)          │    ├─────────────────────────┤
├─────────────────────────────────────────────────────────────┤    │       (portrait)        │
│                      (portrait)  Fennick                    │    │   Well met. I'm …       │
│   Well met. I'm Fennick …                                   │    │ ┌─────────────────────┐ │
│  [How do I start forging?] [Who is Hodierna?] …             │    │ │ How do I start…     │ │
│                                   you ┌───────────────────┐ │    │ └─────────────────────┘ │
│                                       │ Tell me about…    │ │    │ ┌─────────────────── ◉┐ │
│                                       └───────────────────┘ │    │ │ Fennick is … answer │ │
│ ┌─────────────────────────────────────────────────── ◉ ┐    │    │ │ with badges ② ③     │ │
│ │ Fennick is thinking ● ● ●  →  streamed answer         │   │    │ │ [Copy answer]       │ │
│ │ with live formatting and numbered badges ② ③          │   │    │ │ ▸ Sources used (2)  │ │
│ │ [Copy answer]    ▸ Sources used (2)   ← folded        │   │    │ └─────────────────────┘ │
│ │ Pull on a thread: [Therengia]                         │   │    ├─────────────────────────┤
│ └───────────────────────────────────────────────────────┘   │    │ [ Ask…       ]  [Ask]   │
│ [ Ask about DragonRealms…                       ]   [Ask]   │    └─────────────────────────┘
│ Enter to send · Shift+Enter for a new line                  │
└─────────────────────────────────────────────────────────────┘
```

- **Layout choice** is made in `<head>` before first paint: the saved choice, else mobile on a phone-sized
  screen, else desktop. `html[data-view="desktop|mobile"]` drives the CSS; the header toggle switches and remembers it.
- **Live formatting.** `static/render.js` (a pure function, also tested under node) turns the growing answer into
  HTML on every animation frame: headings, lists, tables, bold, italics, code, and citation badges. Half-typed
  markup renders sensibly (an unclosed `**` is already bold). The final text from `done` replaces the raw draft,
  so tags added by the program appear at the end of streaming.
- **Showing where each line came from.** Solid numbered badges are citations by the model; dashed ones were matched
  to their source automatically. Hovering a badge shows the source's title, section and an excerpt; clicking it
  opens "Sources used" and highlights the entry; hovering an entry lights up every badge that cites it.
  "Sources used" lists only what the answer cites; everything else searched is under "Also consulted".
- **Motion.** The thinking dots ripple through the portrait's colours; his portrait sways while he thinks and his
  mouth flaps with the words. With "reduce motion" the bounce and sway stop but the dots still fade and change colour.
- **Reading while it streams.** The page follows the answer only while you are at the bottom; scroll up and a
  "Jump to latest" button appears. One answer runs at a time, errors show a "Try again" button, and a dropped
  connection is reported as possibly incomplete.

- **Index panel.** Shows last delta, last full refresh, newest edit seen, source and pending work
  (`jobs.index_info`, read from `sync_log` and `state`), with **Delta update**, **Full refresh** and **Stop**.

### Index jobs

```
 browser ──POST /api/index/start {mode}──▶ guard() ──▶ JobManager.start()  (asyncio.Lock, one job at a time)
                                                         │ spawn: python -m epedia.cli --config … --source … sync [--full-scan]
 browser ◀── GET /api/index/info (1.5 s while running) ◀─┤ _pump(): reads stdout, strips colour codes, keeps last 40 lines
 browser ──POST /api/index/stop────────────────────────▶ _kill_tree()  (taskkill /T /F on Windows, killpg elsewhere)
```

The job is the same CLI a person would run, so the web page and the command line cannot drift apart; it uses the
server's own config file and wiki source. `guard()` protects start and stop (they cause real traffic to the
wiki): `X-QQE: 1` header (cannot be sent cross-site without a CORS preflight, which is never granted), a Host
header naming this machine (DNS rebinding), Origin equal to Host, and a loopback client when bound beyond
loopback. A job that exits, is stopped, or fails to spawn is always recorded as finished with an exit code; the
server stops any running job when it shuts down.

FastAPI serves one static page and a few JSON endpoints: `POST /api/chat` (Server-Sent Events: `sources`, `token`,
`done` with the final text and related pages, `error`), `/api/persona`, `/api/version`, `/api/status`,
`/api/sessions`, `GET /api/index/info`, `POST /api/index/start|stop`, and `/static/*` (portraits, `render.js`).

## Persona

Fennick is a **voice, not a source**. The persona prompt gives him a short backstory and rules: first person only
for framing; every fact cited from a passage; relay the wiki's hedges and source tier; mechanics out of character
and in CAPS commands; one closing offer. Because an 8B model follows factual rules far better than stylistic
ones, style directives sit beside the passages (`FRAMING`, `SHAPE_HINTS`) and anything that must be exact (the
offer, tagging uncited lines, softening a misplaced "nothing on that") is done in code. `persona.enabled = false`
restores plain answers.

## Module map

```
 src/epedia/
 ├─ cli.py ............. typer commands (init, discover, crawl, classify, smw, index, sync, ask, serve, eval …)
 ├─ config.py .......... pydantic settings: epedia.toml + epedia.local.toml + EPEDIA_* environment
 ├─ kinds.py ........... question kinds, page kinds, the weight matrix
 ├─ db.py .............. schema, migrations, transaction(), state helpers
 ├─ jobs.py ............ index job runner (child sync process, stop, status) and index_info for the Index panel
 ├─ buildinfo.py ....... version, git commit, models, index size (read live, cached for a minute)
 ├─ wiki/    client (rate limit, maxlag, retries) · discover · crawl · smw · sync
 ├─ text/    html2md (HTML → blocks) · chunker · stats (item stat sheets)
 ├─ index/   indexer (chunks, vectors, titles) · classify (kinds, links, soft redirects)
 ├─ retrieval/ search (hybrid, scoring) · titles (normalisation)
 ├─ llm/     ollama (embed, chat, JSON-schema chat) · prompts (planner, persona, shapes, passages)
 ├─ core/    assistant (plan → retrieve → answer) · attribution (tag uncited lines) · memory ·
 │           items_tool (SMW ask + local fallback)
 ├─ frontends/web/  app.py (FastAPI) · static/ (index.html, render.js, fennick.png, fennick_talk.png)
 └─ evalx/   runner (hit@k, MRR, kind/mode accuracy, voice checklist)
 tests/  pytest suite + saved action=parse fixtures       eval/questions.yaml  the evaluation set
 tools/  make_fennick.py (draws the portrait)
```

## Design decisions in brief

- **Retrieval, not training.** Updates show up within minutes of a sync, and every answer can cite its pages.
- **Rendered HTML, not wikitext.** The wiki leans on templates, transclusion and queries; only the rendered page
  has the real content.
- **One SQLite file.** Transactional state next to vectors; no server to run. Concurrent writers (web, sync)
  use `BEGIN IMMEDIATE` and WAL.
- **Hybrid search.** Game wikis are full of exact names (items, spells, places) that meaning-search alone misses.
- **Be a polite crawler.** Rate-limited, `maxlag`, identifying User-Agent, resumable; one source is primary.
- **Honest persona.** Grounded facts, cited, hedges relayed; nothing in the voice is allowed to be evidence.

_Last updated: 1.0.0 (2026-10-22)_
