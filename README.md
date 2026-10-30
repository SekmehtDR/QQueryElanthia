# QQueryElanthia

A local assistant that answers questions about the text MUD **DragonRealms** from
[Elanthipedia](https://elanthipedia.play.net), the player-run wiki. Every fact is cited to the wiki page it came from;
when the wiki has nothing on a question, it says so. It answers as **Fennick**, a Halfling Bard (the persona can be
turned off). Everything runs on your machine: SQLite (FTS5 + sqlite-vec) for search, [Ollama](https://ollama.com) for
the models.

The command is `qqueryelanthia`, alias `epedia`. (QQ is how the internet writes crying.)

## Requirements

- Windows or Linux, Python 3.14
- Ollama with `qwen3:8b` and `nomic-embed-text`, and a GPU with about 8 GB free
- About 2 GB of disk

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\pip install -e ".[dev]"
.\.venv\Scripts\epedia init        # creates data\wiki.db, checks Ollama, pulls both models
```

Put a contact (email or wiki user page) in `[wiki] user_agent` in `epedia.toml`.

Build the index once (several hours, mostly the rate-limited crawl of about 117,000 pages; interrupt and rerun to
resume):

```powershell
.\.venv\Scripts\epedia pipeline    # discover, crawl, classify, smw, index, titles
.\.venv\Scripts\epedia monitor     # progress, in another window
```

## Run

```powershell
.\.venv\Scripts\qqueryelanthia serve    # web chat at http://127.0.0.1:8765
```

To run it without a window: `Start-Process .\.venv\Scripts\qqueryelanthia.exe -ArgumentList serve -WindowStyle Hidden`.
To stop it, match its exact command line (never the word "index": the folder is `epediaindexer`):

```powershell
Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'qqueryelanthia\.exe" serve' } |
  ForEach-Object { taskkill /PID $_.ProcessId /T /F }
```

Stopping the server also stops any job started from its Index panel.

| Problem | Fix |
|---|---|
| Browser can't connect | Start the server. |
| Answers fail with a connection error | Start Ollama. |
| "Can't find anything" on every question | The index is empty: Index panel, **Re-index**. |
| "Address already in use" | It is already running, or use `serve --port 9000`. |

## The web page

- Answers stream with numbered citation badges: solid when cited by the model, dashed when matched by code, dotted
  with a "?" when the page does not clearly support the line.
- 👍 / 👎 under each answer; a thumbs-down asks what went wrong. Review them with `epedia feedback`.
- The start screen lists the last 20 questions asked. **Past chats** opens, and **Delete all chats** clears, every
  conversation on the page, visitors' included. **About** tells who Fennick is.
- **Index** (this computer only) runs updates and shows index health.

## Share it

A Cloudflare quick tunnel gives a temporary public link while your computer, Ollama and the server are running.

1. Set an access code (8+ characters) in the git-ignored `epedia.local.toml`, then restart the server. Without a code,
   tunnelled visitors are refused.

   ```toml
   [web]
   access_code = "a phrase only your friends know"
   ```

2. `winget install --id Cloudflare.cloudflared`, then `cloudflared tunnel --url http://localhost:8765` and share the
   printed link and the code.

Visitors enter the code once, see only their own chats, and get no Index panel or Past chats. They are limited to 40
questions an hour, and at most two answers are written at once. Their questions are stored in your database: tell
them. Keep `[web] host` at `127.0.0.1`.

## Commands

| Command | Does |
|---|---|
| `epedia ask "..."` / `epedia chat` | One question / a conversation in the terminal |
| `epedia sync` | Fetch only what changed (the web server does this hourly) |
| `epedia sync --full-scan` | Re-compare every page |
| `epedia fill` | Fetch missing pages from the configured source |
| `epedia feedback [turn]` | Thumbs-down answers, or one in its conversation |
| `epedia eval [--full]` | Retrieval quality; `--full` adds answers, refusals and conversations |
| `epedia doctor` | Index health check |
| `epedia show "Page"` | How a page was converted and chunked |
| `epedia search "..." --explain` | Retrieval only, with ranks and weights |

## Configuration

`epedia.toml`, with machine-local overrides in the git-ignored `epedia.local.toml` (or `EPEDIA_<SECTION>__<KEY>`
environment variables):

- `[wiki]` sources, namespaces (all of the wiki's by default), request rates
- `[retrieval]` result counts, boosts, namespace and kind weights
- `[chat]` history, temperature, `regenerate` (rewrite a draft that names things no page has; `"off"` to disable)
- `[persona]` `name`, `enabled = false` for plain answers
- `[web]` `auto_sync_minutes` (0 = off), `access_code`, `visitor_turns_per_hour`

**Sources.** `original` is elanthipedia.play.net; a `mirror` can be added for bulk reads. With a mirror, set in
`epedia.local.toml`:

```toml
[wiki]
source = "mirror"              # bulk reads
fallback_source = "original"   # when the mirror fails or lacks a page
delta_source = "original"      # "what changed?" always asks the real wiki
```

Citation links always point at elanthipedia.play.net. Ask a server's owner before raising its request rate.

## How it works

Pages are fetched rendered (`action=parse`), converted to text, classified (shop, creature, spell, talk page, ...),
chunked by section and embedded. A question is planned (rewritten to stand alone, labelled with a kind), searched
(BM25 + vectors, fused, weighted by kind and namespace) and answered by the model from the retrieved passages only.
Code then checks the answer against those passages: unsupported citations are marked, an invented name or a pretended
look-up triggers one rewrite, and questions about things the wiki never mentions are declined without the model.
Each stage is keyed by revision, so reruns and updates only touch what changed.

Source in `src/epedia/`, tests in `tests/`, evaluation sets in `eval/`. See `ARCHITECTURE.md` for detail.

## Limits and credits

Answers are only as good as the wiki and an 8B model; check the linked pages. All knowledge comes from the
community-written Elanthipedia. DragonRealms is a game by Simutronics; this is an unofficial fan tool, not affiliated
with either.
