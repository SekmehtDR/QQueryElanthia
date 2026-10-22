# QQueryElanthia: Fennick, a storyteller's guide to DragonRealms

A local assistant that answers questions about the text MUD **DragonRealms** from **Elanthipedia**, the
player-run wiki, and cites the pages it used. Ask it in plain language; it answers as *Fennick*, a Halfling Bard
who keeps a satchel of notes copied from the wiki: curious and warm, but strict about facts. Everything runs on
your own machine (SQLite with FTS5 + sqlite-vec for search, [Ollama](https://ollama.com) for the models) and a
sync job re-indexes only the pages that changed.

```
You  > How do I start forging?
(crafting) searching: How do I start forging?
Fennick  To start forging you'll want the Forging guide's walkthrough ... [2]
         (OOC) The steps, in order: ...
         If you like, I can tell you about Weaponsmithing discipline.
Sources: Forging guide — Forging Walkthrough > Forging Items, Forging skill — Training, ...
```

The name is a joke: *QQuery* because QQ is how the internet writes crying, and anyone who has hunted for a
half-documented DragonRealms mechanic knows the feeling. The command is `qqueryelanthia`, with `epedia` as a
short alias; the examples below use `epedia`.

## What it does

- **Grounded answers with citations.** Every fact comes from retrieved wiki passages and is cited `[n]`, linking
  to the page and section. If the wiki has nothing relevant, Fennick says so instead of guessing.
- **Knows what you are asking.** A planner labels each question (item, crafting, place, character, lore,
  creature, mechanics, event/news, or a greeting) and retrieval favours the matching kinds of wiki page:
  crafting questions reach the guides and disciplines, "who is Hodierna" reaches the Immortals, shop questions
  reach shop pages, and role-play logs and obsolete pages are demoted.
- **Stat questions use live structured data.** "What is the heaviest heavy edged weapon?" becomes a Semantic
  MediaWiki query against the wiki's own item data.
- **Remembers the conversation.** Follow-ups ("which one is lighter?") are rewritten to name their subject and
  are answered from the previous answer's source pages.
- **Stays current.** `epedia sync` re-fetches only edited, moved, deleted and template-affected pages.
- **Honest about being a program.** Fennick is a voice, not a source: the persona never claims experience of
  its own, keeps game mechanics plain and out of character, and relays the wiki's own hedges (outdated,
  obsolete, incomplete, era, GM post vs. player guide).

## Requirements

Windows or Linux, Python 3.14, [Ollama](https://ollama.com) running locally, and a GPU with about 8 GB free
(developed on an RTX 3060 12 GB). Models: `qwen3:8b` (chat and planner) and `nomic-embed-text` (embeddings).
Roughly 3 GB of disk for the database.

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\pip install -e ".[dev]"
.\.venv\Scripts\epedia init          # creates data\wiki.db, checks Ollama, pulls both models
```

Edit `epedia.toml` and put a contact (email or wiki user page) in `[wiki] user_agent` so the wiki admins can
reach you. Optional Ollama settings (user environment variables, then restart Ollama):
`OLLAMA_FLASH_ATTENTION=1`, `OLLAMA_KV_CACHE_TYPE=q8_0`.

## Build the index (once, resumable)

```powershell
.\.venv\Scripts\epedia pipeline      # discover -> crawl -> classify -> smw -> index -> titles
```

Expect several hours the first time, mostly the polite, rate-limited crawl of ~109k pages. Or run the stages
yourself, in two terminals so embedding overlaps the network-bound crawl:

```powershell
epedia discover ; epedia crawl       # terminal 1
epedia index --follow                # terminal 2
epedia classify ; epedia smw ; epedia index --titles   # after the crawl
```

Interrupt any stage with Ctrl-C and rerun it; it continues where it stopped. Watch progress with
`epedia monitor` (live console view) or `epedia status`; the web page shows a status bar too.

## Use it

```powershell
epedia chat                          # conversation with memory (/new, /quit)
epedia ask "What do Barbarian roars do?"
epedia serve                         # web chat at http://127.0.0.1:8765
```

The web page greets you as Fennick, offers starter questions, shows what kind of question it detected, and
suggests related pages to pull on after each answer. Answers are formatted as they arrive and carry numbered
badges showing which page each line came from (solid: cited by the model; dashed: matched automatically). Use
**Desktop / Mobile** in the header to switch layouts.

The **Index** button opens the index panel: when the wiki was last checked, how many pages are waiting, and
buttons for a **Delta update** (only what changed), a **Full refresh** (re-compares every page; slow, asks first)
and **Stop**. Each run has a **View details** link listing the pages it changed. These only work from the computer running the server.

Debugging helpers:

| Command | Shows |
|---|---|
| `epedia show "Musk hog"` | the page's kind, stripped banners, and exactly how it was converted and chunked |
| `epedia search "forge weapons" --kind crafting --explain` | retrieval only, with per-signal ranks, page kind and weight |
| `epedia ask "..." --kind place` | force a question kind |
| `epedia items "heaviest heavy edged weapon"` | the planner plus the structured item query |
| `epedia classify --stats` | how many pages of each kind the wiki has |
| `epedia eval` / `epedia eval --answers` | retrieval quality (hit@k, MRR, planner accuracy) / answers to review by hand |

## Configuration

Everything is in `epedia.toml` (override any key with `EPEDIA_<SECTION>__<KEY>`):

- `[persona]` `name` and `enabled`. Set `enabled = false` for plain, neutral answers.
- `[retrieval]` result counts, boosts, `log_weight`, `obsolete_weight`, `offer_related`, and optional
  `[retrieval.kind_weights.<kind>]` overrides of the built-in question-kind x page-kind matrix
  (`src/epedia/kinds.py`).
- `[wiki]` `source`, `namespaces`, `item_namespaces`, request rates.
- `[ollama]` models and context size.

## Wiki sources

`epedia.toml` lists two sources, `original` (elanthipedia.play.net) and `mirror` (a community copy). Content is
read from `[wiki] source`, **`original` by default**: always current, and an hourly `sync` is only a few
requests. A mirror is faster for a full rebuild but lags the original by hours and does not record edits in its
recent-changes list.

```powershell
epedia sources                     # reachable? how far is each source behind?
epedia --source mirror crawl       # use another source for one command
epedia --source mirror discover --force   # make it the primary source (re-labels pages, does not re-download)
```

Pages are identified by title, so either source can feed incremental `sync`. Revision numbers differ between
sites, so whole-wiki operations (`discover`, `sync --full-scan`) refuse to run against a source other than the
primary one. `adopt` keeps already-fetched pages whose stored copy is provably current when the primary changes.
Answers always cite the original site's URLs. Be considerate of any server you crawl: check with its owner
before raising `rps`, and note that API requests are generally not cached.

## Keep it current

```powershell
epedia sync                          # edits/deletions/moves since the last run (crawl, classify, smw, index)
epedia sync --full-scan              # weekly safety net (catches silent re-renders)
epedia refresh --older-than 90 --limit 1000
epedia watch --every 30              # or schedule it:
```

```powershell
$exe = "<repo>\.venv\Scripts\epedia.exe"
$wd  = "<repo>"
Register-ScheduledTask -TaskName "Epedia Sync" -Action (New-ScheduledTaskAction -Execute $exe -Argument "sync --log data\sync.log" -WorkingDirectory $wd) -Trigger (New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Hours 1)) -Settings (New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Hours 3) -MultipleInstances IgnoreNew)
Register-ScheduledTask -TaskName "Epedia Full Scan" -Action (New-ScheduledTaskAction -Execute $exe -Argument "sync --full-scan --log data\sync.log" -WorkingDirectory $wd) -Trigger (New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday -At 4am)
```

The generic Item namespace (~66k pages) is left out of structured-stat extraction because it costs one request per
page; run `epedia smw --ns 118` to add it.

## How it works

| Stage | What happens |
|---|---|
| discover | `allpages` + `prop=info` per namespace, redirects resolved; ~1,000 requests |
| crawl | `action=parse` -> rendered HTML (templates expanded), stored compressed |
| text | HTML -> sections; key/value and one-column tables become `Key: value` lines; maintenance banners are stripped and noted; "Redirect to:" stubs are dropped |
| classify | each page gets a kind (shop, npc, pc, creature, guide, spell, lore, ...) from namespace, categories and title patterns, plus a link graph; no network or model needed |
| index | ~450-token chunks per section, FTS5 + 768-dim vectors; unchanged text is never re-embedded |
| smw | `browsebysubject` item properties -> `stats` chunk + `item_properties` table |
| plan | the planner rewrites follow-ups, labels the question kind, and routes stat questions to a live Semantic MediaWiki `ask` query |
| answer | hybrid search (BM25 + vectors, rank fusion) with title boosts and kind weights, then a grounded, cited, streamed answer in Fennick's voice with one closing offer chosen from real related pages |

Pages carry `latest_revid -> fetched_revid -> indexed_revid -> smw_revid -> classified_revid`; every stage
processes rows where its revision lags, which is what makes reruns and updates cheap.

Layout: `src/epedia/{wiki,text,index,retrieval,llm,core,frontends,evalx}`, tests in `tests/`, the evaluation set
in `eval/questions.yaml`. A Discord bot only needs to call
`epedia.core.assistant.Assistant.answer_stream(session_id, message)`.

## Limits

Answers are only as good as the wiki and an 8B local model: the model can still add small flourishes the pages
do not support, and many wiki pages are incomplete or outdated (Fennick will say when a page is marked so). The
links under every answer are there so you can check.

## Credits

All knowledge comes from the community-written [Elanthipedia](https://elanthipedia.play.net). DragonRealms is
a game by Simutronics; this project is an unofficial fan tool and is not affiliated with either.
